"""Explicit, bounded editing operations for the Photos MCP server."""

from __future__ import annotations

import base64
from collections.abc import Callable, Sequence
import os
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from requests import RequestException

from pyicloud import PyiCloudService
from pyicloud.common.cloudkit import (
    CKModifyOperation,
    CKRecord,
    CKTombstoneRecord,
    CKWriteRecord,
    CKZoneIDReq,
)
from pyicloud.exceptions import PyiCloudException
from pyicloud.photos_mcp_common import RequestError, album_summary, parse_date
from pyicloud.services.photos import (
    AlbumContainer,
    PhotoAlbum,
    PhotoAlbumFolder,
    PhotoAsset,
    PhotoLibrary,
    SmartPhotoAlbum,
)
from pyicloud.services.photos_cloudkit.mappers import record_field_value
from pyicloud.services.photos_cloudkit.materialize import build_xmp_metadata

EDIT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)


class EditingReader(Protocol):
    """The minimal account interface consumed by editing tools."""

    api: PyiCloudService | None

    def write_library(self, key: str, dry_run: bool) -> PhotoLibrary:
        """Return the gated private library."""
        ...

    def photo(self, library: str, photo_id: str) -> PhotoAsset:
        """Return an explicitly selected visible photo."""
        ...


class PhotosEditor:
    """Prepare exact targets before writes and preserve uncertain outcomes."""

    def __init__(self, reader: EditingReader) -> None:
        self.reader = reader

    def library(
        self, library: str, dry_run: bool, *, deleting: bool = False
    ) -> PhotoLibrary:
        """Destructive container/library operations need a separate opt-in."""
        if (
            deleting
            and not dry_run
            and os.environ.get("ICLOUD_PHOTOS_ENABLE_DELETES") != "1"
        ):
            raise RequestError(
                "Deletes disabled. Set ICLOUD_PHOTOS_ENABLE_DELETES=1 "
                "in addition to the write gate."
            )
        return self.reader.write_library(library, dry_run)

    @staticmethod
    def container(albums: AlbumContainer, item_id: str) -> PhotoAlbum:
        """Require an exact regular album or folder ID, never a name or smart album."""
        item = albums.get(item_id)
        if not isinstance(item, PhotoAlbum) or isinstance(item, SmartPhotoAlbum):
            raise RequestError("Provide an existing regular album or folder ID.")
        return item

    def prepare(
        self,
        library: PhotoLibrary,
        item_id: str,
        kind: str,
        fields: dict[str, Any],
        *,
        relation: tuple[str, str] | None = None,
    ) -> CKModifyOperation:
        """Read a fresh change tag and constrain the exact CloudKit record type."""
        records = library.service.private_client.lookup(
            record_names=[item_id],
            zone_id=CKZoneIDReq(**library.zone_id),
        ).records
        record = next(
            (
                item
                for item in records
                if isinstance(item, CKRecord)
                and item.recordName == item_id
                and item.recordType == kind
            ),
            None,
        )
        if record is None or not record.recordChangeTag:
            raise RequestError(
                "Fresh record/change tag unavailable; no change submitted."
            )
        if relation and (
            record_field_value(record, "itemId") != relation[0]
            or record_field_value(record, "containerId") != relation[1]
        ):
            raise RequestError(
                "Album relation identity did not match; no change submitted."
            )
        return CKModifyOperation(
            operationType="delete" if relation else "update",
            record=CKWriteRecord(
                recordName=item_id,
                recordType=kind,
                recordChangeTag=record.recordChangeTag,
                fields=fields,
            ),
        )

    @staticmethod
    def apply(library: PhotoLibrary, operation: CKModifyOperation) -> str:
        """Only confirm exact echoed values or a matching relation tombstone."""
        try:
            response = library.service.private_client.modify(
                operations=[operation],
                zone_id=CKZoneIDReq(**library.zone_id),
                atomic=True,
            )
        # Do not lose earlier confirmed batch results when iCloud fails.
        except (PyiCloudException, RequestException, OSError, ValueError):  # pylint: disable=broad-exception-caught
            return "uncertain"
        for record in response.records:
            if record.recordName != operation.record.recordName:
                continue
            if operation.operationType == "delete":
                if isinstance(record, CKTombstoneRecord) and record.deleted:
                    return "confirmed"
            elif (
                isinstance(record, CKRecord)
                and record.recordType == operation.record.recordType
                and all(
                    record_field_value(record, name) == field.value
                    for name, field in operation.record.fields.items()
                )
            ):
                return "confirmed"
        return "unconfirmed"

    def edit_container(
        self,
        item_id: str,
        name: str | None,
        parent_id: str | None,
        moving: bool,
        library: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Rename or move a container without creating hierarchy cycles/collisions."""
        if not moving and (
            name is None
            or not name.strip()
            or len(name) > 255
            or "/" in name
            or any(ord(char) < 32 for char in name)
        ):
            raise RequestError(
                "Use a name of 1..255 characters without slashes or control characters."
            )
        selected = self.library(library, dry_run)
        albums = selected.refresh_albums()
        target = self.container(albums, item_id)
        destination = parent_id if moving else target.parent_id
        if destination is not None:
            if not isinstance(albums.get(destination), PhotoAlbumFolder):
                raise RequestError(
                    "Destination must be an existing folder ID or null for root."
                )
            visited = {target.id}
            ancestor: str | None = destination
            while ancestor is not None:
                if ancestor in visited:
                    raise RequestError(
                        "Moving would create or traverse a folder cycle."
                    )
                visited.add(ancestor)
                ancestor = self.container(albums, ancestor).parent_id
        new_name = target.name if moving else name
        if any(
            isinstance(item, PhotoAlbum)
            and item.id != target.id
            and item.name == new_name
            and item.parent_id == destination
            for item in albums
        ):
            raise RequestError("A sibling with this name already exists.")
        before = album_summary(target)
        after = {"name": new_name, "parent_id": destination}
        if new_name == target.name and destination == target.parent_id:
            return {
                "status": "unchanged",
                "dry_run": dry_run,
                "before": before,
                "after": after,
            }
        fields = (
            {"parentId": {"type": "STRING", "value": destination}}
            if moving
            else {
                "albumNameEnc": {
                    "type": "ENCRYPTED_BYTES",
                    "value": base64.b64encode(str(new_name).encode()).decode(),
                }
            }
        )
        operation = self.prepare(selected, target.id, "CPLAlbum", fields)
        status = "would_update" if dry_run else self.apply(selected, operation)
        self.reader.api = None if not dry_run else self.reader.api
        return {"status": status, "dry_run": dry_run, "before": before, "after": after}

    def metadata(
        self,
        photo_id: str,
        title: str | None,
        description: str | None,
        favorite: bool | None,
        taken_at: str | None,
        library: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Update supported library metadata, leaving original files untouched."""
        fields: dict[str, Any] = {}
        for key, value in (("captionEnc", title), ("extendedDescEnc", description)):
            if value is not None:
                if len(value) > 8192:
                    raise RequestError("Metadata text must not exceed 8192 characters.")
                fields[key] = {
                    "type": "ENCRYPTED_BYTES",
                    "value": base64.b64encode(value.encode()).decode(),
                }
        if favorite is not None:
            fields["isFavorite"] = {"type": "INT64", "value": int(favorite)}
        date = parse_date(taken_at)
        if date is not None:
            fields["assetDate"] = {
                "type": "TIMESTAMP",
                "value": int(date.timestamp() * 1000),
            }
        if not fields:
            raise RequestError("Specify at least one metadata change.")
        selected = self.library(library, dry_run)
        photo = self.reader.photo(library, photo_id)
        metadata = build_xmp_metadata(photo.asset_record)
        summary = {
            "title": metadata.title if metadata else None,
            "description": metadata.description if metadata else None,
            "favorite": record_field_value(photo.asset_record, "isFavorite") == 1,
            "taken_at": photo.asset_date.isoformat(),
        }
        changes = {
            key: value
            for key, value in (
                ("title", title),
                ("description", description),
                ("favorite", favorite),
                ("taken_at", taken_at),
            )
            if value is not None
        }
        operation = self.prepare(selected, photo.id, "CPLAsset", fields)
        status = "would_update" if dry_run else self.apply(selected, operation)
        if not dry_run:
            self.reader.api = None
        return {
            "photo_id": photo.id,
            "status": status,
            "dry_run": dry_run,
            "fields": list(fields),
            "before": {key: summary[key] for key in changes},
            "after": changes,
            "note": "Library metadata only; original EXIF unchanged.",
        }

    def delete_containers(
        self,
        item_id: str,
        recursive: bool,
        library: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Delete explicit containers, descendants first; never delete their photos."""
        selected = self.library(library, dry_run, deleting=True)
        albums = selected.refresh_albums()
        target = self.container(albums, item_id)
        ordered: list[PhotoAlbum] = []
        visiting: set[str] = set()

        def visit(item: PhotoAlbum) -> None:
            if item.id in visiting:
                raise RequestError("Folder hierarchy contains a cycle.")
            if len(visiting) >= 100:
                raise RequestError("At most 100 containers may be deleted per call.")
            visiting.add(item.id)
            children = [
                child
                for child in albums
                if isinstance(child, PhotoAlbum) and child.parent_id == item.id
            ]
            if children and not recursive:
                raise RequestError(
                    "Folder is not empty. Preview with recursive=true "
                    "to include descendants."
                )
            for child in children:
                visit(child)
            ordered.append(item)

        visit(target)
        operations = [
            self.prepare(
                selected,
                item.id,
                "CPLAlbum",
                {
                    "isDeleted": {"type": "INT64", "value": 1},
                },
            )
            for item in ordered
        ]
        return self.batch(
            selected,
            [item.id for item in ordered],
            operations,
            dry_run,
            effect="delete_containers_keep_photos",
        )

    def batch(
        self,
        library: PhotoLibrary,
        ids: list[str],
        operations: Sequence[CKModifyOperation | None],
        dry_run: bool,
        *,
        effect: str,
    ) -> dict[str, Any]:
        """Stop after an uncertain write and retain per-target progress."""
        results = []
        complete = True
        for item_id, operation in zip(ids, operations, strict=True):
            if not complete:
                status = "not_attempted"
            elif operation is None:
                status = "already_absent"
            elif dry_run:
                status = "would_change"
            else:
                status = PhotosEditor.apply(library, operation)
                complete = status == "confirmed"
            results.append({"id": item_id, "status": status})
        if not dry_run:
            self.reader.api = None
        return {
            "dry_run": dry_run,
            "complete": complete,
            "effect": effect,
            "results": results,
            "note": "Inspect uncertain results before retrying; no automatic rollback.",
        }

    def photos(self, library: str, ids: list[str]) -> list[PhotoAsset]:
        """Validate a whole batch before any mutation."""
        if not 1 <= len(ids) <= 100 or any(
            not value or len(value) > 512 for value in ids
        ):
            raise RequestError("Provide between 1 and 100 explicit photo IDs.")
        return [self.reader.photo(library, value) for value in dict.fromkeys(ids)]

    def delete_photos(
        self, ids: list[str], library: str, dry_run: bool
    ) -> dict[str, Any]:
        """Mark photos deleted; never expunge/purge library assets."""
        selected = self.library(library, dry_run, deleting=True)
        photos = self.photos(library, ids)
        operations = [
            self.prepare(
                selected,
                photo.id,
                "CPLAsset",
                {
                    "isDeleted": {"type": "INT64", "value": 1},
                },
            )
            for photo in photos
        ]
        return self.batch(
            selected,
            [photo.id for photo in photos],
            operations,
            dry_run,
            effect="mark_library_photos_deleted_no_purge",
        )

    def memberships(
        self,
        source_id: str,
        ids: list[str],
        destination_id: str | None,
        library: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Remove album relations, or add-and-verify a destination before removing."""
        if destination_id == source_id:
            raise RequestError("Source and destination albums must differ.")
        selected = self.library(library, dry_run)
        albums = selected.refresh_albums()
        source = self.container(albums, source_id)
        destination = self.container(albums, destination_id) if destination_id else None
        if isinstance(source, PhotoAlbumFolder) or isinstance(
            destination, PhotoAlbumFolder
        ):
            raise RequestError("Photo membership targets must be regular albums.")
        photos = self.photos(library, ids)
        operations: list[CKModifyOperation | None] = []
        present: set[str] = set()
        for photo in photos:
            if destination and destination.get(photo.id) is not None:
                present.add(photo.id)
            if source.get(photo.id) is None:
                operations.append(None)
            else:
                relation_id = f"{photo.asset_id}-IN-{source.id}"
                operations.append(
                    self.prepare(
                        selected,
                        relation_id,
                        "CPLContainerRelation",
                        {},
                        relation=(photo.asset_id, source.id),
                    )
                )
        if destination is None or dry_run:
            result = self.batch(
                selected,
                [photo.id for photo in photos],
                operations,
                dry_run,
                effect="move_album_membership"
                if destination
                else "remove_album_membership",
            )
            if destination:
                result["destination_album_id"] = destination.id
                result["would_add"] = [
                    photo.id for photo in photos if photo.id not in present
                ]
            return result
        results = []
        complete = True
        for photo, operation in zip(photos, operations, strict=True):
            phase = "not_attempted"
            status = "not_attempted"
            if complete:
                phase = "destination"
                try:
                    if photo.id not in present:
                        destination.add_photo(photo)
                    if destination.get(photo.id) is None:
                        raise RequestError("Destination membership not confirmed.")
                    phase = "source"
                    status = (
                        "already_absent"
                        if operation is None
                        else self.apply(selected, operation)
                    )
                except (  # pylint: disable=broad-exception-caught
                    PyiCloudException,
                    RequestException,
                    OSError,
                    ValueError,
                    KeyError,
                ):
                    status = "uncertain"
                complete = status in ("confirmed", "already_absent")
            results.append({"id": photo.id, "status": status, "phase": phase})
        self.reader.api = None
        return {
            "dry_run": False,
            "complete": complete,
            "effect": "move_album_membership",
            "results": results,
            "note": "Destination is confirmed before source removal; "
            "partial moves are retained.",
        }


def register_edit_tools(
    server: FastMCP,
    reader: EditingReader,
    protect: Callable[[Callable[..., Any]], Callable[..., Any]],
) -> None:
    """Expose edits separately from additive organizing tools."""
    editor = PhotosEditor(reader)

    def register(function: Callable[..., Any]) -> None:
        server.tool(annotations=EDIT_ANNOTATIONS)(protect(function))

    @register
    def rename_container(
        container_id: str, name: str, library: str = "root", dry_run: bool = True
    ) -> dict[str, Any]:
        """Rename by exact ID; preview by default and reject name collisions."""
        return editor.edit_container(container_id, name, None, False, library, dry_run)

    @register
    def move_container(
        container_id: str,
        parent_id: str | None = None,
        library: str = "root",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Move an album/folder into a folder, or root with null. Reject cycles."""
        return editor.edit_container(
            container_id, None, parent_id, True, library, dry_run
        )

    @register
    def set_photo_metadata(
        photo_id: str,
        title: str | None = None,
        description: str | None = None,
        favorite: bool | None = None,
        taken_at: str | None = None,
        library: str = "root",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Edit title, description, favorite or ISO capture date.

        None means unchanged; empty text clears. Original EXIF stays unchanged.
        """
        return editor.metadata(
            photo_id, title, description, favorite, taken_at, library, dry_run
        )

    @register
    def remove_photos_from_album(
        album_id: str, photo_ids: list[str], library: str = "root", dry_run: bool = True
    ) -> dict[str, Any]:
        """Remove only album memberships; keep photos in iCloud and other albums."""
        return editor.memberships(album_id, photo_ids, None, library, dry_run)

    @register
    def move_photos_between_albums(
        source_album_id: str,
        destination_album_id: str,
        photo_ids: list[str],
        library: str = "root",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Verify target membership before source removal; report partial results."""
        return editor.memberships(
            source_album_id, photo_ids, destination_album_id, library, dry_run
        )

    @register
    def delete_container(
        container_id: str,
        recursive: bool = False,
        library: str = "root",
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Delete containers, keeping photos. Nonempty folders need recursive=true.

        Actual changes require both the write and delete configuration gates.
        """
        return editor.delete_containers(container_id, recursive, library, dry_run)

    @register
    def delete_photos(
        photo_ids: list[str], library: str = "root", dry_run: bool = True
    ) -> dict[str, Any]:
        """Mark up to 100 photos deleted from iCloud and synced devices.

        No permanent purge. Requires both write and delete configuration gates.
        """
        return editor.delete_photos(photo_ids, library, dry_run)
