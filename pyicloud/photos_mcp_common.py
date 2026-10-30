"""Shared validation and album metadata for the Photos MCP tools."""

from datetime import datetime, timedelta, timezone
from typing import Any

from pyicloud.services.photos import (
    BasePhotoAlbum,
    PhotoAlbum,
    PhotoAlbumFolder,
    SmartPhotoAlbum,
)


def album_summary(album: BasePhotoAlbum) -> dict[str, Any]:
    """Describe hierarchy without returning raw records."""
    return {
        "id": album.id,
        "name": album.name,
        "kind": "folder"
        if isinstance(album, PhotoAlbumFolder)
        else ("smart_album" if isinstance(album, SmartPhotoAlbum) else "album"),
        "parent_id": album.parent_id if isinstance(album, PhotoAlbum) else None,
        "path": album.fullname if isinstance(album, PhotoAlbum) else album.name,
    }


class RequestError(ValueError):
    """A safe, locally constructed message intended for the MCP client."""


def parse_date(value: str | None, *, end: bool = False) -> datetime | None:
    """Interpret a bare date as a UTC day, with an exclusive upper bound."""
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RequestError("Dates must use ISO 8601 format.") from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    if end and len(value) == 10:
        result += timedelta(days=1)
    return result
