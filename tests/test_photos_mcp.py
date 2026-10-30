"""Read-only Photos MCP contracts, privacy boundaries and bounded searches."""

import base64
from datetime import datetime, timezone
from io import BytesIO
import plistlib
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from mcp.server.fastmcp.exceptions import ToolError
from PIL import Image
import pytest
from requests import RequestException

from pyicloud import photos_mcp as m
from pyicloud.services.photos import (
    AlbumContainer,
    PhotoAlbum,
    PhotoAlbumFolder,
    PhotoLibrary,
    SmartPhotoAlbum,
)


def asset(photo_id: str = "photo-1", **fields: Any) -> Any:
    """Build a metadata-only asset without network or disk access."""
    return SimpleNamespace(
        id=photo_id,
        filename=f"{photo_id}.jpg",
        asset_date=datetime(2026, 8, 15, tzinfo=timezone.utc),
        added_date=datetime(2026, 8, 16, tzinfo=timezone.utc),
        item_type="image",
        dimensions=(100, 100),
        size=500,
        asset_record={
            "fields": {key: {"value": value} for key, value in fields.items()}
        },
        resources={},
    )


@pytest.fixture(name="reader")
def reader_fixture() -> Any:
    """Create an account double without connecting to Apple."""
    result = m.PhotosReader()
    library = MagicMock(spec=PhotoLibrary)
    library.all = MagicMock()
    library.all.__iter__.side_effect = lambda: iter([asset(), asset("second")])
    api = MagicMock()
    api.get_auth_status.return_value = {
        "authenticated": True,
        "requires_2fa": False,
        "requires_2sa": False,
    }
    api.photos.libraries = {"root": library, "shared": object()}
    result.api = api
    with patch.dict(
        "os.environ",
        {
            "ICLOUD_USERNAME": "account@example.com",
            "ICLOUD_SESSION_DIR": "/private/session",
        },
    ):
        yield result


def search(reader: Any, **options: Any) -> dict[str, Any]:
    """Call the bounded reader with explicit default options."""
    args: dict[str, Any] = {
        "library": "root",
        "album": None,
        "from_date": None,
        "to_date": None,
        "text": None,
        "bounds": None,
        "favorites_only": False,
        "offset": 0,
        "limit": 30,
        "scan_limit": 500,
    }
    args.update(options)
    return cast(dict[str, Any], reader.search(**args))


def test_dates() -> None:
    """Bare dates, aware timestamps, invalid input and inclusive end dates."""
    assert m.parse_date(None) is None
    assert str(m.parse_date("2026-08-15")) == "2026-08-15 00:00:00+00:00"
    assert str(m.parse_date("2026-08-15", end=True)) == "2026-08-16 00:00:00+00:00"
    assert (
        str(m.parse_date("2026-08-15T12:00:00Z", end=True))
        == "2026-08-15 12:00:00+00:00"
    )
    with pytest.raises(m.RequestError, match="ISO"):
        m.parse_date("not-a-date")


@pytest.mark.parametrize(
    "bounds,location,expected",
    [
        ([30, 20, 40, 30], {"latitude": 35, "longitude": 25}, True),
        ([30, 20, 40, 30], {"latitude": 35, "longitude": 31}, False),
        ([-10, 170, 10, -170], {"latitude": 0, "longitude": -179}, True),
        ([-10, 170, 10, -170], {"latitude": 0, "longitude": 0}, False),
        ([30, 20, 40, 30], None, False),
        ([30, 20, 40, 30], {"latitude": 35, "longitude": None}, False),
    ],
)
def test_bounds(bounds: Any, location: Any, expected: bool) -> None:
    """Handle missing GPS and the date line without guessing places."""
    assert m.in_bounds(location, bounds) is expected


def test_summary_redacts_records_and_urls() -> None:
    """Only explicitly selected fields leave the service."""
    photo = asset(isFavorite=1)
    with patch.object(
        m,
        "build_xmp_metadata",
        return_value=SimpleNamespace(
            gps_latitude=35.0,
            gps_longitude=25.0,
            title="Crete",
            description="Holiday",
            keywords=["beach"],
        ),
    ):
        result = m.photo_summary(photo)
    assert result["location"]["latitude"] == 35
    assert result["favorite"] is True
    assert "asset_record" not in result
    assert "versions" not in result
    with patch.object(m, "build_xmp_metadata", return_value=None):
        assert m.photo_summary(photo)["title"] is None


def test_session_factory_and_reuse(reader: Any) -> None:
    """Never request a password, device discovery or interactive login."""
    reader.api = None
    with patch.object(m, "PyiCloudService") as factory:
        factory.return_value.get_auth_status.return_value = {
            "authenticated": True,
            "requires_2fa": False,
            "requires_2sa": False,
        }
        assert reader.connection() is factory.return_value
        reader.connection()
    assert factory.call_count == 1
    assert factory.call_args.kwargs["authenticate"] is False
    assert factory.call_args.kwargs["refresh_interval"] is None
    assert factory.call_args.kwargs["accept_terms"] is False


@pytest.mark.parametrize(
    "status",
    [
        {"authenticated": False},
        {"authenticated": True, "requires_2fa": True},
        {"authenticated": True, "requires_2fa": False, "requires_2sa": True},
    ],
)
def test_expired_session(reader: Any, status: Any) -> None:
    """Authentication failures clear the session for a later retry."""
    reader.api.get_auth_status.return_value = status
    with pytest.raises(m.RequestError, match="Session"):
        reader.connection()
    assert reader.api is None


def test_missing_configuration() -> None:
    """Missing configuration fails before service construction."""
    with patch.dict("os.environ", {}, clear=True), pytest.raises(m.RequestError):
        m.PhotosReader().connection()


def test_libraries_and_albums(reader: Any) -> None:
    """Shared streams are disclosed as unsupported and pages stay bounded."""
    assert reader.libraries()["libraries"][1]["supported"] is False
    with pytest.raises(m.RequestError):
        reader.library("shared")
    with pytest.raises(m.RequestError):
        reader.library("missing")
    library = reader.library("root")
    library.albums = [SimpleNamespace(id=str(i), name=str(i)) for i in range(3)]
    assert reader.albums("root", 0, 2)["next_offset"] == 2
    assert reader.albums("root", 2, 2)["next_offset"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 101},
        {"offset": -1},
        {"scan_limit": 0},
        {"from_date": "2026-09-01", "to_date": "2026-08-01"},
        {"bounds": [1]},
        {"bounds": [40, 20, 30, 30]},
        {"bounds": [30, -200, 40, 30]},
        {"bounds": [30, 20, 40, float("nan")]},
    ],
)
def test_invalid_search_before_auth(reader: Any, options: Any) -> None:
    """Reject invalid filters before touching the account."""
    reader.api.get_auth_status.reset_mock()
    with pytest.raises(m.RequestError):
        search(reader, **options)
    reader.api.get_auth_status.assert_not_called()


def test_pagination_is_based_on_scanned_items(reader: Any) -> None:
    """Empty pages must retain their cursor until the source is exhausted."""
    page = search(reader, text="absent", scan_limit=1)
    assert page["photos"] == []
    assert page["next_offset"] == 1
    assert page["complete"] is False
    assert search(reader, offset=2)["complete"] is True
    page = search(reader, limit=1)
    assert page["next_offset"] == 1
    assert len(page["photos"]) == 1
    assert len(search(reader, offset=1)["photos"]) == 1


def test_filters_and_album(reader: Any) -> None:
    """Date, hidden, favorites, GPS and text filters compose."""
    library = reader.library("root")
    library.albums = {
        "trip": [asset(isHidden=1), asset("other"), asset("MATCH", isFavorite=1)]
    }
    assert (
        len(search(reader, album="trip", favorites_only=True, text="match")["photos"])
        == 1
    )
    assert not search(reader, from_date="2027-01-01")["photos"]
    assert not search(reader, to_date="2025-01-01")["photos"]
    assert not search(reader, bounds=[30, 20, 40, 30])["photos"]
    photo = asset()
    photo.asset_date = datetime(2026, 8, 15)
    library.albums = {"trip": [photo]}
    assert search(reader, album="trip", to_date="2026-08-15")["complete"]
    with patch.object(m, "in_bounds", return_value=True):
        assert search(reader, bounds=[30, 20, 40, 30])["photos"]


def test_explicit_photo_and_hidden(reader: Any) -> None:
    """Details and previews cannot retrieve hidden or missing photos."""
    library = reader.library("root")
    library.all.get.return_value = asset()
    assert reader.photo("root", "photo-1").id == "photo-1"
    for value in (None, asset(isHidden=1)):
        library.all.get.return_value = value
        with pytest.raises(m.RequestError):
            reader.photo("root", "photo-1")
    with pytest.raises(m.RequestError):
        reader.photo("root", "")


def preview_photo(reader: Any, *, movie: bool = False) -> Any:
    """Create a preview-only resource and a small image in memory."""
    data = BytesIO()
    Image.new("RGB", (32, 16)).save(data, format="PNG")
    photo = asset()
    photo.item_type = "movie" if movie else "image"
    key = "thumb_image" if movie else "thumb"
    photo.resources = {key: SimpleNamespace(size=len(data.getvalue()))}
    photo.download = MagicMock(return_value=data.getvalue())
    reader.library("root").all.get.return_value = photo
    return photo


@pytest.mark.parametrize("movie", [True, False])
def test_preview_never_downloads_original(reader: Any, movie: bool) -> None:
    """Image and movie previews are JPEG blocks with stripped metadata."""
    photo = preview_photo(reader, movie=movie)
    result = reader.preview("root", "photo-1")
    photo.download.assert_called_once_with("thumb_image" if movie else "thumb")
    assert result.to_image_content().mimeType == "image/jpeg"
    with Image.open(BytesIO(result.data)) as image:
        assert not image.getexif()
        assert image.size == (32, 16)


@pytest.mark.parametrize("size", [None, 0, m.MAX_PREVIEW_BYTES + 1])
def test_preview_bad_declared_size(reader: Any, size: Any) -> None:
    """Oversized or unknown previews fail before downloading."""
    photo = preview_photo(reader)
    photo.resources["thumb"].size = size
    with pytest.raises(m.RequestError):
        reader.preview("root", "photo-1")
    photo.download.assert_not_called()


def test_preview_absent_and_bad_bytes(reader: Any) -> None:
    """Missing, empty, oversized and invalid previews do not fall back."""
    photo = preview_photo(reader)
    photo.resources = {}
    with pytest.raises(m.RequestError):
        reader.preview("root", "photo-1")
    photo = preview_photo(reader)
    for data in (None, b"x" * (m.MAX_PREVIEW_BYTES + 1)):
        photo.download.return_value = data
        with pytest.raises(m.RequestError):
            reader.preview("root", "photo-1")
    photo.download.return_value = b"invalid"
    with pytest.raises(OSError):
        reader.preview("root", "photo-1")
    with patch.object(Image, "open") as opened:
        opened.return_value.__enter__.return_value.width = 30_000
        opened.return_value.__enter__.return_value.height = 30_000
        with pytest.raises(m.RequestError, match="dimensions"):
            reader.preview("root", "photo-1")


@pytest.mark.parametrize(
    "error",
    [
        RequestException("secret-url"),
        OSError("secret-path"),
        ValueError("secret-token"),
        KeyError("secret-id"),
    ],
)
def test_remote_errors_are_redacted(reader: Any, error: Exception) -> None:
    """Only locally constructed messages may leave the exception boundary."""

    def fail() -> None:
        raise error

    with pytest.raises(ToolError) as raised:
        m.guarded(reader, fail)()
    assert "secret" not in str(raised.value)
    assert reader.api is None
    with pytest.raises(ToolError, match="safe"):
        m.guarded(reader, lambda: (_ for _ in ()).throw(m.RequestError("safe")))()


@patch(
    "pydantic_settings.sources.providers.dotenv.DotEnvSettingsSource._read_env_files",
    return_value={},
)
async def test_mcp_contract_and_calls(_dotenv: Any, reader: Any) -> None:
    """Verify schemas, read annotations, JSON results and image content."""
    server = m.create_server(reader)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == {
        "doctor",
        "list_libraries",
        "list_albums",
        "search_photos",
        "get_photo",
        "get_preview",
        "create_folder",
        "create_album",
        "add_photos_to_album",
    }
    assert all(
        tool.annotations
        and tool.annotations.readOnlyHint
        == (tool.name not in {"create_folder", "create_album", "add_photos_to_album"})
        for tool in tools
    )
    reader.library("root").albums = []
    reader.library("root").all.get.return_value = asset()
    for name, args in (
        ("doctor", {}),
        ("list_libraries", {}),
        ("list_albums", {}),
        ("search_photos", {}),
        ("get_photo", {"photo_id": "photo-1"}),
    ):
        assert await server.call_tool(name, args)
    preview_photo(reader)
    assert await server.call_tool("get_preview", {"photo_id": "photo-1"})
    assert m.create_server() is not None


def test_main() -> None:
    """The executable starts only a stdio transport."""
    with patch.object(m, "create_server") as factory:
        m.main()
    factory.return_value.run.assert_called_once_with(transport="stdio")


def test_cloudkit_location_decoding() -> None:
    """Real CloudKit encoded GPS is decoded without leaking record fields."""
    location = base64.b64encode(plistlib.dumps({"lat": 35.2, "lon": 25.1})).decode()
    result = m.photo_summary(asset(locationEnc=location))
    assert result["location"] == {"latitude": 35.2, "longitude": 25.1}


def container(
    name: str = "Trip",
    item_id: str = "album-1",
    *,
    folder: bool = False,
    parent_id: str | None = None,
) -> Any:
    """An explicit container double with the same class boundary as production."""
    item = MagicMock(spec=PhotoAlbumFolder if folder else PhotoAlbum)
    item.id = item_id
    item.name = name
    item.parent_id = parent_id
    item.fullname = name
    item.get.return_value = None
    return item


def prepare_writes(reader: Any, items: list[Any]) -> Any:
    """Populate refreshed albums and enable the root-library test fixture."""
    selected = reader.library("root")
    selected.refresh_albums.return_value = AlbumContainer(items)
    return selected


@pytest.mark.parametrize("library,dry_run", [("shared:zone", True), ("root", False)])
def test_write_gate_before_account(reader: Any, library: str, dry_run: bool) -> None:
    """A shared library or disabled gate must never trigger authentication."""
    reader.api.get_auth_status.reset_mock()
    with (
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "0"}),
        pytest.raises(m.RequestError),
    ):
        reader.create_container("Trip", False, None, library, dry_run)
    reader.api.get_auth_status.assert_not_called()


@pytest.mark.parametrize("name", ["", "  ", "a/b", "a\n", "a" * 256])
def test_container_invalid_name(reader: Any, name: str) -> None:
    """Names are validated before account access."""
    with pytest.raises(m.RequestError):
        reader.create_container(name, False, None, "root", True)


@pytest.mark.parametrize("folder", [False, True])
def test_create_preview_existing_and_nested(reader: Any, folder: bool) -> None:
    """Folder and album plans are non-writing and parent-aware."""
    parent = container("Travel", "parent", folder=True)
    selected = prepare_writes(reader, [parent])
    plan = reader.create_container("Trip", folder, "parent", "root", True)
    assert plan["status"] == "would_create"
    assert plan["container"]["parent_id"] == "parent"
    selected.create_album.assert_not_called()
    created = container(parent_id="parent", folder=folder)
    selected.create_album.return_value = created
    with patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "1"}):
        result = reader.create_container("Trip", folder, "parent", "root", False)
    assert result["status"] == "created"
    assert selected.create_album.call_args.kwargs["parent_id"] == "parent"
    selected.refresh_albums.return_value = AlbumContainer([parent, created])
    selected.create_album.reset_mock()
    assert (
        reader.create_container("Trip", folder, "parent", "root", True)["status"]
        == "already_exists"
    )
    selected.create_album.assert_not_called()
    # Names may repeat in different parent folders without being ambiguous.
    assert (
        reader.create_container("Trip", folder, None, "root", True)["status"]
        == "would_create"
    )


@pytest.mark.parametrize(
    "items,parent_id",
    [
        ([], "missing"),
        ([container()], "album-1"),
        ([container(), container(item_id="duplicate")], None),
        ([container(folder=True)], None),
    ],
)
def test_create_invalid_parent_or_name_collision(
    reader: Any, items: Any, parent_id: Any
) -> None:
    """No mutation for an invalid parent, ambiguous name or wrong sibling type."""
    selected = prepare_writes(reader, items)
    with pytest.raises(m.RequestError):
        reader.create_container("Trip", False, parent_id, "root", True)
    selected.create_album.assert_not_called()


@pytest.mark.parametrize("failure", [None, "exception", "wrong-parent"])
def test_create_uncertain_outcome(reader: Any, failure: Any) -> None:
    """Never report a successful creation without a matching response."""
    selected = prepare_writes(reader, [])
    selected.create_album.return_value = (
        container(parent_id="unexpected") if failure == "wrong-parent" else None
    )
    if failure == "exception":
        selected.create_album.side_effect = RequestException("secret-token")
    with (
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "1"}),
        pytest.raises(m.RequestError, match="before retrying") as raised,
    ):
        reader.create_container("Trip", False, None, "root", False)
    assert "secret" not in str(raised.value)
    assert reader.api is None


@pytest.mark.parametrize(
    "target", [None, container(folder=True), MagicMock(spec=SmartPhotoAlbum)]
)
def test_assign_invalid_target(reader: Any, target: Any) -> None:
    """Only a regular album is a valid destination."""
    selected = prepare_writes(reader, [])
    selected.refresh_albums.return_value.get = MagicMock(return_value=target)
    with pytest.raises(m.RequestError):
        reader.assign_photos("album-1", ["photo-1"], "root", True)


@pytest.mark.parametrize(
    "album_id,ids",
    [("", ["x"]), ("a", []), ("a", [""]), ("a", ["x"] * 101), ("a", ["x" * 513])],
)
def test_assign_invalid_batch(reader: Any, album_id: str, ids: list[str]) -> None:
    """Validate batch bounds without sending any writes."""
    with pytest.raises(m.RequestError):
        reader.assign_photos(album_id, ids, "root", True)


def test_assign_validates_whole_batch(reader: Any) -> None:
    """An unknown or hidden photo prevents every write in the batch."""
    target = container()
    selected = prepare_writes(reader, [target])
    selected.all.get.side_effect = [asset(), None]
    with (
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "1"}),
        pytest.raises(m.RequestError),
    ):
        reader.assign_photos(target.id, ["photo-1", "missing"], "root", False)
    target.add_photo.assert_not_called()


def test_assignment_plan_deduplicates_and_skips_members(reader: Any) -> None:
    """Duplicate IDs and existing memberships never cause redundant mutations."""
    target = container()
    selected = prepare_writes(reader, [target])
    selected.all.get.side_effect = asset
    target.get.side_effect = lambda value: asset(value) if value == "present" else None
    plan = reader.assign_photos(target.id, ["present", "new", "new"], "root", True)
    assert [item["status"] for item in plan["results"]] == [
        "already_present",
        "would_add",
    ]
    target.add_photo.assert_not_called()
    target.get.side_effect = [asset("present"), None, asset("new")]
    target.add_photo.return_value = True
    with patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "1"}):
        result = reader.assign_photos(
            target.id, ["present", "new", "new"], "root", False
        )
    assert [item["status"] for item in result["results"]] == [
        "already_present",
        "added",
    ]
    assert result["complete"] is True
    target.add_photo.assert_called_once()


@pytest.mark.parametrize("failure", ["exception", "false", "missing"])
def test_assignment_partial_failure_preserves_progress(
    reader: Any, failure: str
) -> None:
    """Stop after uncertain outcomes and preserve confirmed earlier additions."""
    target = container()
    selected = prepare_writes(reader, [target])
    selected.all.get.side_effect = asset
    target.get.side_effect = [None, None, None, asset("one"), None]
    target.add_photo.side_effect = [
        True,
        RequestException("secret-url")
        if failure == "exception"
        else failure != "false",
    ]
    with patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "1"}):
        result = reader.assign_photos(target.id, ["one", "two", "three"], "root", False)
    assert result["complete"] is False
    assert result["results"][0]["status"] == "added"
    assert result["results"][1]["status"] == (
        "uncertain" if failure == "exception" else "unconfirmed"
    )
    assert result["results"][2]["status"] == "not_attempted"
    assert "secret-url" not in str(result)
    assert target.add_photo.call_count == 2


@patch(
    "pydantic_settings.sources.providers.dotenv.DotEnvSettingsSource._read_env_files",
    return_value={},
)
async def test_mcp_write_tools_and_doctor(_dotenv: Any, reader: Any) -> None:
    """MCP defaults to dry runs and reports server-side write permission."""
    target = container()
    prepare_writes(reader, [target])
    reader.api.photos.libraries["root"].all.get.return_value = asset()
    server = m.create_server(reader)
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ("create_album", "create_folder", "add_photos_to_album"):
        assert tools[name].inputSchema["properties"]["dry_run"]["default"] is True
        assert await server.call_tool(
            name,
            {"album_id": target.id, "photo_ids": ["photo-1"]}
            if name == "add_photos_to_album"
            else {"name": "New"},
        )
    target.add_photo.assert_not_called()
    with patch.dict("os.environ", {"ICLOUD_PHOTOS_ENABLE_WRITES": "1"}):
        result = await server.call_tool("doctor", {})
    assert "writes_enabled" in str(result)
