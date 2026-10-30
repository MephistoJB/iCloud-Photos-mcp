"""Editing contracts: exact targets, dry runs, conflicts and partial outcomes."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from requests import RequestException

from pyicloud.common.cloudkit import (
    CKModifyOperation,
    CKRecord,
    CKTombstoneRecord,
    CKWriteRecord,
    CKZoneIDReq,
)
from pyicloud.common.cloudkit.client import CloudKitContainerClient
from pyicloud.photos_mcp import PhotosReader, RequestError, create_server
from pyicloud.photos_mcp_edits import PhotosEditor
from pyicloud.services.photos import AlbumContainer, PhotoLibrary, SmartPhotoAlbum
from tests.test_photos_mcp import asset, container


@pytest.fixture(name="state")
def edit_state() -> Any:
    """A fully local account double with record-aware CloudKit responses."""
    reader = PhotosReader()
    api = MagicMock()
    api.get_auth_status.return_value = {
        "authenticated": True,
        "requires_2fa": False,
        "requires_2sa": False,
    }
    library = MagicMock(spec=PhotoLibrary)
    library.zone_id = {"zoneName": "PrimarySync"}
    library.refresh_albums.return_value = AlbumContainer([container()])
    library.all.get.side_effect = lambda value: SimpleNamespace(**{
        **vars(asset(value)),
        "asset_id": value,
    })
    library.service = api.photos
    api.photos.libraries = {"root": library}
    reader.api = api
    records: dict[str, CKRecord] = {}
    client = library.service.private_client
    client.lookup.side_effect = lambda **kwargs: SimpleNamespace(
        records=[records[value] for value in kwargs["record_names"] if value in records]
    )

    def echo(**kwargs: Any) -> Any:
        operation = kwargs["operations"][0]
        if operation.operationType == "delete":
            item: Any = CKTombstoneRecord(
                recordName=operation.record.recordName, deleted=True
            )
        else:
            item = CKRecord(
                recordName=operation.record.recordName,
                recordType=operation.record.recordType,
                fields=operation.record.fields,
                recordChangeTag="new",
            )
        return SimpleNamespace(records=[item])

    client.modify.side_effect = echo
    with (
        patch.object(reader, "connection", return_value=api),
        patch.dict(
            "os.environ",
            {
                "ICLOUD_USERNAME": "test@example.com",
                "ICLOUD_SESSION_DIR": "/session",
                "ICLOUD_PHOTOS_ENABLE_WRITES": "1",
                "ICLOUD_PHOTOS_ENABLE_DELETES": "1",
            },
        ),
    ):
        yield SimpleNamespace(
            api=api,
            reader=reader,
            editor=PhotosEditor(reader),
            library=library,
            client=client,
            records=records,
        )


def record(
    state: Any, item_id: str = "album-1", kind: str = "CPLAlbum", **values: Any
) -> CKRecord:
    """Give an exact record a real change tag for optimistic concurrency."""
    item = CKRecord.model_validate({
        "recordName": item_id,
        "recordType": kind,
        "recordChangeTag": "old",
        "fields": {
            key: {"type": "STRING", "value": value} for key, value in values.items()
        },
    })
    state.records[item_id] = item
    return item


def test_delete_gate_and_write_gate(state: Any) -> None:
    """Both gates are required for actual deletes, before any account access."""
    with (
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_DELETES": "0"}),
        pytest.raises(RequestError, match="Deletes disabled"),
    ):
        state.editor.delete_photos(["p"], "root", False)
    with (
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "0"}),
        pytest.raises(RequestError, match="Writes disabled"),
    ):
        state.editor.delete_photos(["p"], "root", False)
    state.client.modify.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "wrong_type", "tag"])
def test_prepare_requires_fresh_matching_record(state: Any, kind: str) -> None:
    """A wrong or absent identity/change tag cannot cause a write."""
    if kind != "missing":
        item = record(state, kind="CPLAsset" if kind == "wrong_type" else "CPLAlbum")
        if kind == "tag":
            item.recordChangeTag = None
    with pytest.raises(RequestError, match="Fresh record"):
        state.editor.prepare(state.library, "album-1", "CPLAlbum", {})
    state.client.modify.assert_not_called()


def test_relation_identity_and_errors(state: Any) -> None:
    """The record must actually join the requested photo and album."""
    record(state, "p-IN-a", "CPLContainerRelation", itemId="other", containerId="a")
    with pytest.raises(RequestError, match="identity"):
        state.editor.prepare(
            state.library, "p-IN-a", "CPLContainerRelation", {}, relation=("p", "a")
        )
    item = record(state)
    operation = state.editor.prepare(state.library, item.recordName, "CPLAlbum", {})
    state.client.modify.side_effect = RequestException("secret-token")
    assert state.editor.apply(state.library, operation) == "uncertain"
    state.client.modify.side_effect = None
    state.client.modify.return_value = SimpleNamespace(
        records=[record(state, "different")]
    )
    assert state.editor.apply(state.library, operation) == "unconfirmed"
    state.client.modify.return_value = SimpleNamespace(
        records=[CKTombstoneRecord(recordName="album-1", deleted=True)]
    )
    assert state.editor.apply(state.library, operation) == "unconfirmed"


def test_rename_preview_apply_and_unchanged(state: Any) -> None:
    """No mutation on preview or no-op; actual update carries the old tag."""
    record(state)
    assert (
        state.editor.edit_container("album-1", "Trip", None, False, "root", False)[
            "status"
        ]
        == "unchanged"
    )
    result = state.editor.edit_container(
        "album-1", "Holiday", None, False, "root", True
    )
    assert result["after"]["name"] == "Holiday"
    state.client.modify.assert_not_called()
    result = state.editor.edit_container(
        "album-1", "Holiday", None, False, "root", False
    )
    assert result["status"] == "confirmed"
    operation = state.client.modify.call_args.kwargs["operations"][0]
    assert operation.record.recordChangeTag == "old"
    assert operation.operationType == "update"
    assert state.reader.api is None


@pytest.mark.parametrize("name", [None, "", "a/b", "a\n", "x" * 256])
def test_rename_validation(state: Any, name: Any) -> None:
    """Reject invalid names before any mutation."""
    with pytest.raises(RequestError):
        state.editor.edit_container("album-1", name, None, False, "root", True)


def test_container_identity_and_collision(state: Any) -> None:
    """Names are not IDs; smart albums and ambiguous destinations are rejected."""
    for item_id in ("missing", "Trip"):
        with pytest.raises(RequestError):
            state.editor.edit_container(item_id, "New", None, False, "root", True)
    smart = MagicMock(spec=SmartPhotoAlbum)
    smart.id = "smart"
    state.library.refresh_albums.return_value = AlbumContainer([smart])
    with pytest.raises(RequestError):
        state.editor.edit_container("smart", "New", None, False, "root", True)
    state.library.refresh_albums.return_value = AlbumContainer([
        container(),
        container("New", "other"),
    ])
    with pytest.raises(RequestError, match="sibling"):
        state.editor.edit_container("album-1", "New", None, False, "root", True)


def test_move_container_cycles_root_and_parent(state: Any) -> None:
    """Validate ancestry before any field update and allow an explicit root move."""
    folder = container("Travel", "folder", folder=True)
    child = container("Child", "child", folder=True, parent_id="folder")
    album = container(parent_id="folder")
    state.library.refresh_albums.return_value = AlbumContainer([folder, child, album])
    record(state)
    result = state.editor.edit_container("album-1", None, None, True, "root", True)
    assert result["after"]["parent_id"] is None
    operation = state.editor.prepare(
        state.library,
        "album-1",
        "CPLAlbum",
        {"parentId": {"type": "STRING", "value": None}},
    )
    assert operation.record.fields["parentId"].value is None
    result = state.editor.edit_container("album-1", None, "child", True, "root", False)
    assert result["status"] == "confirmed"
    state.reader.api = state.api
    for destination in ("missing", "album-1", "child"):
        with pytest.raises(RequestError):
            state.editor.edit_container("folder", None, destination, True, "root", True)


def test_metadata_plan_and_update(state: Any) -> None:
    """Supported metadata is visible in plans and does not modify the master."""
    record(state, "p", "CPLAsset")
    result = state.editor.metadata(
        "p", "Title", "", False, "2026-08-15T12:00:00Z", "root", True
    )
    assert result["after"] == {
        "title": "Title",
        "description": "",
        "favorite": False,
        "taken_at": "2026-08-15T12:00:00Z",
    }
    state.client.modify.assert_not_called()
    result = state.editor.metadata("p", "Title", None, None, None, "root", False)
    assert result["status"] == "confirmed"
    assert set(state.client.modify.call_args.kwargs["operations"][0].record.fields) == {
        "captionEnc"
    }
    for title in (None, "x" * 8193):
        with pytest.raises(RequestError):
            state.editor.metadata("p", title, None, None, None, "root", True)


def test_delete_folder_descendants_and_limits(state: Any) -> None:
    """Nonempty folders require recursion; planned child-first deletes retain photos."""
    folder = container("Travel", "folder", folder=True)
    album = container(parent_id="folder")
    state.library.refresh_albums.return_value = AlbumContainer([folder, album])
    record(state, "folder")
    record(state)
    with pytest.raises(RequestError, match="not empty"):
        state.editor.delete_containers("folder", False, "root", True)
    result = state.editor.delete_containers("folder", True, "root", True)
    assert [item["id"] for item in result["results"]] == ["album-1", "folder"]
    state.client.modify.assert_not_called()
    assert state.editor.delete_containers("folder", True, "root", False)["complete"]
    for call in state.client.modify.call_args_list:
        operation = call.kwargs["operations"][0]
        assert operation.record.recordType == "CPLAlbum"
        assert set(operation.record.fields) == {"isDeleted"}
    folder.parent_id = "album-1"
    with pytest.raises(RequestError, match="cycle"):
        state.editor.delete_containers("folder", True, "root", True)
    chain = [
        container(item_id=str(i), folder=True, parent_id=str(i - 1) if i else None)
        for i in range(101)
    ]
    state.library.refresh_albums.return_value = AlbumContainer(chain)
    with pytest.raises(RequestError, match="100"):
        state.editor.delete_containers("0", True, "root", True)


def test_delete_photos_batch_progress_and_validation(state: Any) -> None:
    """Whole-batch validation precedes soft deletion; no purge is exposed."""
    for value in ("one", "two", "three"):
        record(state, value, "CPLAsset")
    plan = state.editor.delete_photos(["one", "one"], "root", True)
    assert len(plan["results"]) == 1
    state.client.modify.assert_not_called()
    with patch.object(PhotosEditor, "apply", side_effect=["confirmed", "uncertain"]):
        result = state.editor.delete_photos(["one", "two", "three"], "root", False)
    assert [item["status"] for item in result["results"]] == [
        "confirmed",
        "uncertain",
        "not_attempted",
    ]
    for ids in ([], [""], ["x" * 513], ["x"] * 101):
        with pytest.raises(RequestError):
            state.editor.delete_photos(ids, "root", True)


def setup_membership(state: Any) -> tuple[Any, Any]:
    """A source relation with no destination membership yet."""
    source, destination = container(item_id="source"), container(item_id="destination")
    source.get.return_value = asset("p")
    state.library.refresh_albums.return_value = AlbumContainer([source, destination])
    record(
        state, "p-IN-source", "CPLContainerRelation", itemId="p", containerId="source"
    )
    return source, destination


def test_remove_membership_and_absent(state: Any) -> None:
    """Remove only the matching relation, with a fresh tag instead of force delete."""
    source, _ = setup_membership(state)
    assert state.editor.memberships("source", ["p"], None, "root", True)["complete"]
    state.client.modify.assert_not_called()
    result = state.editor.memberships("source", ["p"], None, "root", False)
    assert result["results"][0]["status"] == "confirmed"
    operation = state.client.modify.call_args.kwargs["operations"][0]
    assert operation.record.recordType == "CPLContainerRelation"
    assert operation.operationType == "delete"
    assert operation.record.recordChangeTag == "old"
    source.get.return_value = None
    assert (
        state.editor.memberships("source", ["p"], None, "root", False)["results"][0][
            "status"
        ]
        == "already_absent"
    )
    state.client.modify.return_value = SimpleNamespace(records=[])
    state.client.modify.side_effect = None
    assert state.editor.apply(state.library, operation) == "unconfirmed"


def test_move_membership_add_verify_remove(state: Any) -> None:
    """The destination is verified before the source relation is removed."""
    source, destination = setup_membership(state)
    preview = state.editor.memberships("source", ["p"], "destination", "root", True)
    assert preview["would_add"] == ["p"]
    destination.add_photo.assert_not_called()
    events = []
    destination.add_photo.side_effect = lambda _: events.append("add")
    destination.get.side_effect = [None, asset("p")]
    original_apply = PhotosEditor.apply

    def remove(library: Any, operation: Any) -> str:
        events.append("remove")
        return original_apply(library, operation)

    with patch.object(PhotosEditor, "apply", side_effect=remove):
        assert state.editor.memberships("source", ["p"], "destination", "root", False)[
            "complete"
        ]
    assert events == ["add", "remove"]
    # An already completed move is a no-op.
    source.get.return_value = None
    destination.get.side_effect = None
    destination.get.return_value = asset("p")
    destination.add_photo.reset_mock()
    assert state.editor.memberships("source", ["p"], "destination", "root", False)[
        "complete"
    ]
    destination.add_photo.assert_not_called()


def test_move_partial_and_bad_destinations(state: Any) -> None:
    """Never remove source membership without a confirmed destination."""
    source, destination = setup_membership(state)
    for invalid in ("source", "missing"):
        with pytest.raises(RequestError):
            state.editor.memberships("source", ["p"], invalid, "root", True)
    folder = container(item_id="folder", folder=True)
    state.library.refresh_albums.return_value = AlbumContainer([source, folder])
    with pytest.raises(RequestError):
        state.editor.memberships("source", ["p"], "folder", "root", True)
    source, destination = setup_membership(state)
    record(
        state, "q-IN-source", "CPLContainerRelation", itemId="q", containerId="source"
    )
    result = state.editor.memberships(
        "source", ["p", "q"], "destination", "root", False
    )
    assert [item["status"] for item in result["results"]] == [
        "uncertain",
        "not_attempted",
    ]
    assert result["results"][0]["phase"] == "destination"
    state.client.modify.assert_not_called()
    destination.add_photo.side_effect = RequestException("secret-url")
    assert not state.editor.memberships("source", ["p"], "destination", "root", False)[
        "complete"
    ]
    destination.add_photo.side_effect = None
    destination.get.side_effect = [None, asset("p")]
    state.client.modify.side_effect = RequestException("secret-url")
    result = state.editor.memberships("source", ["p"], "destination", "root", False)
    assert result["results"][0]["phase"] == "source"
    assert "secret-url" not in str(result)


def test_cloudkit_explicit_null_is_preserved() -> None:
    """A move to root must send JSON null, not silently omit the field value."""

    session = MagicMock()
    session.post.return_value.json.return_value = {"records": []}
    session.post.return_value.status_code = 200
    client = CloudKitContainerClient("https://example.com", session, {})
    operation = CKModifyOperation(
        operationType="update",
        record=CKWriteRecord(
            recordName="album",
            recordType="CPLAlbum",
            recordChangeTag="old",
            fields={"parentId": {"type": "STRING", "value": None}},
        ),
    )
    client.modify(operations=[operation], zone_id=CKZoneIDReq(zoneName="PrimarySync"))
    assert (
        session.post.call_args.kwargs["json"]["operations"][0]["record"]["fields"][
            "parentId"
        ]["value"]
        is None
    )


@patch(
    "pydantic_settings.sources.providers.dotenv.DotEnvSettingsSource._read_env_files",
    return_value={},
)
async def test_edit_tool_contracts(_dotenv: Any, state: Any) -> None:
    """All edit tools are marked destructive, expose dry runs, and call their editor."""
    server = create_server(state.reader)
    calls: dict[str, dict[str, Any]] = {
        "rename_container": {"container_id": "album-1", "name": "New"},
        "move_container": {"container_id": "album-1"},
        "set_photo_metadata": {"photo_id": "p", "favorite": True},
        "remove_photos_from_album": {"album_id": "source", "photo_ids": ["p"]},
        "move_photos_between_albums": {
            "source_album_id": "source",
            "destination_album_id": "destination",
            "photo_ids": ["p"],
        },
        "delete_container": {"container_id": "album-1"},
        "delete_photos": {"photo_ids": ["p"]},
    }
    with (
        patch.object(PhotosEditor, "edit_container", return_value={}),
        patch.object(PhotosEditor, "metadata", return_value={}),
        patch.object(PhotosEditor, "memberships", return_value={}),
        patch.object(PhotosEditor, "delete_containers", return_value={}),
        patch.object(PhotosEditor, "delete_photos", return_value={}),
    ):
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert len(tools) == 16
        for name, arguments in calls.items():
            annotation = tools[name].annotations
            assert annotation and annotation.destructiveHint
            assert tools[name].inputSchema["properties"]["dry_run"]["default"] is True
            assert await server.call_tool(name, arguments)
