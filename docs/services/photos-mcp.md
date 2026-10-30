# iCloud Photos MCP

This repository adds a **read-only-by-default stdio MCP server** that accesses iCloud Photos over
Apple's web services. It does not require Photos.app, a local Photos library,
Full Disk Access, or a complete photo download. The web API is unofficial;
Apple changes can break it. There is no country-name, face, or semantic search.

## Install and authenticate

Use Python 3.10 or newer and an isolated environment:

```sh
uv venv
uv pip install '.[cli,mcp]'
. .venv/bin/activate
```

Create a dedicated private session directory and log in interactively:

```sh
mkdir -p "$HOME/.local/state/icloud-photos-mcp"
chmod 700 "$HOME/.local/state/icloud-photos-mcp"
icloud auth login --username 'YOUR_APPLE_ACCOUNT' \
  --session-dir "$HOME/.local/state/icloud-photos-mcp"
```

Enter the normal Apple Account password and complete two-factor verification
in the terminal. An app-specific Mail/Contacts password is not a substitute.
Never put a password or verification code in MCP tool arguments, source code,
or the MCP configuration. Retrieve existing credentials from your secret manager.
Session cookies are credentials too: keep this directory private and out of Git.
Repeat this login when Apple expires the session.

Allow web access to iCloud data on your trusted Apple device. With Advanced
Data Protection, Apple may require additional trusted-device authorization;
compatibility depends on the account and current API. The MCP does not disable
protection, accept account terms, or interactively authenticate itself.

## MCP configuration

Use the absolute path to the installed executable and session directory:

```json
{
  "mcpServers": {
    "icloud-photos": {
      "command": "/ABSOLUTE/PATH/pyicloud/.venv/bin/icloud-photos-mcp",
      "env": {
        "ICLOUD_USERNAME": "YOUR_APPLE_ACCOUNT",
        "ICLOUD_SESSION_DIR": "/ABSOLUTE/PATH/PRIVATE_SESSION_DIRECTORY"
      }
    }
  }
}
```

For Codex, the corresponding entry in `~/.codex/config.toml` is:

```toml
[mcp_servers.icloud_photos]
command = "/ABSOLUTE/PATH/pyicloud/.venv/bin/icloud-photos-mcp"

[mcp_servers.icloud_photos.env]
ICLOUD_USERNAME = "YOUR_APPLE_ACCOUNT"
ICLOUD_SESSION_DIR = "/ABSOLUTE/PATH/PRIVATE_SESSION_DIRECTORY"
```

The transport is stdio only; there is no network listener. Run `doctor` first,
then `list_libraries` and `list_albums`. The default library is `root`.
Discovered CloudKit Shared Libraries are supported; legacy Shared Albums are
listed as unsupported. Shared Library album coverage follows PyiCloud's limits.

## Tools

| Tool             | Purpose                                                         |
| ---------------- | --------------------------------------------------------------- |
| `doctor`         | Validate the session, Photos access, and write gate             |
| `list_libraries` | Discover library IDs                                            |
| `list_albums`    | List up to 100 albums per page                                  |
| `search_photos`  | Filter by date, metadata text, favorite, and GPS bounds         |
| `get_photo`      | Fetch metadata by an explicit photo ID                          |
| `get_preview`    | Return a JPEG preview to the model without an original download |

`search_photos` accepts `library`, `album` (ID or exact name), `from_date`,
`to_date`, `text`, `bounds`, `favorites_only`, `offset`, `limit`, and `scan_limit`.
Dates without offsets use UTC. A bare `to_date` includes that whole UTC day;
a timestamp is exclusive. Text searches filename, title, description and keywords,
not image content. GPS bounds are `[south, west, north, east]`; missing GPS never
matches. A rectangle is approximate and does not establish country membership.

Each call inspects at most 2,000 assets (500 by default) and returns at most 100
matches (30 by default). Follow `next_offset` until null, **even when a page is
empty**. `scanned` includes excluded assets; `complete` refers to reaching the end
of that album, not to results from earlier pages. Results follow album order;
All Photos is ordered by newest **addition**, not capture date. Offset pagination
can shift if the library changes during a scan; retry against a settled library
for a consistent inventory. Large offsets repeat traversal and can be slow.

For a trip, narrow the album/date first and inspect available GPS coordinates.
Collect pages before identifying the most recent capture-date cluster. This
server does not infer trip boundaries or promise Apple's native place search.

Hidden photos are excluded from search, details, and previews. Returned metadata
omits raw CloudKit records and signed download URLs. Photo captions and filenames
are untrusted content, never instructions. Preview resources must have a known
size of at most 10 MiB and at most 20 million pixels; the output is resized to
1,600 pixels and re-encoded as JPEG without EXIF. Unsupported or absent previews
fail rather than falling back to originals. No persistent photo cache is created.

## Optional organizing writes

The server exposes `create_folder`, `create_album`, and `add_photos_to_album`.
All three default to `dry_run: true`: they validate and describe the operation
without changing iCloud. Actual writes additionally require this server-side
configuration in the existing MCP environment block, followed by a restart:

```toml
ICLOUD_PHOTOS_ENABLE_WRITES = "1"
```

For JSON MCP configuration, add `"ICLOUD_PHOTOS_ENABLE_WRITES": "1"` to `env`.
Only the exact value `1` enables writes. `doctor` reports `writes_enabled` and
`read_only`. This is a capability gate, not a substitute for user authorization:
an agent should apply only the organizing changes its user requested.

| Tool                  | Arguments                                              |
| --------------------- | ------------------------------------------------------ |
| `create_folder`       | `name`, optional `parent_id`, `library`, `dry_run`     |
| `create_album`        | `name`, optional `parent_id`, `library`, `dry_run`     |
| `add_photos_to_album` | `album_id`, `photo_ids`, optional `library`, `dry_run` |

Writes support the private `root` library only. Shared Libraries, Shared Albums,
and smart albums are not write targets. Use explicit IDs returned by the read
tools; `parent_id` must identify a folder and `album_id` a regular album.
Folders hold other folders and albums; photos are assigned to an album inside a
folder, not directly to the folder. `list_albums` includes `kind`, `parent_id`,
and `path` so the hierarchy can be inspected.

For example, to organize a trip:

1. Preview `create_folder` with `name: "Travel"`; apply with `dry_run: false`.
2. Preview and apply `create_album` with `name: "Greece 2026"` and the returned
   folder ID as `parent_id`.
3. Search the intended photos and review their explicit IDs.
4. Preview `add_photos_to_album` with the returned album ID and those photo IDs;
   apply that same batch with `dry_run: false`.

Creation refreshes the album listing and reuses an existing same-name sibling
of the correct type. Duplicate sibling names or a conflicting container type
are rejected rather than guessed. Names are limited to 255 characters and must
not be blank or contain slashes or control characters. Separate clients can
still race to create duplicate names; there is no distributed uniqueness lock.
If a response is missing or the connection fails after submitting a create,
the result is unknown: inspect a fresh listing before retrying.

Assignments accept 1 to 100 photo IDs, remove duplicate IDs, validate **all**
photos and the target before writing, and skip existing album members. Hidden
or missing photos reject the entire batch before any addition. Each new
membership is read back. The operation is not an atomic multi-photo transaction:
a later failure retains confirmed earlier additions, marks the current item as
`uncertain` or `unconfirmed`, and marks remaining items `not_attempted`. Inspect
membership before retrying; cloud read-after-write delays can produce an
unconfirmed result even when Apple accepted the addition. There are no automatic
write retries and no automatic rollback. Photos remain in their original
library and all their other albums.

## Editing, moving, and deleting

The following tools also default to `dry_run: true` and require the write gate
for actual changes. Only explicit IDs in the private `root` library are accepted.

| Tool                         | Effect                                                                   |
| ---------------------------- | ------------------------------------------------------------------------ |
| `rename_container`           | Rename an album or folder (`container_id`, `name`)                       |
| `move_container`             | Move an album/folder to `parent_id`; null means library root             |
| `set_photo_metadata`         | Change title, description, favorite, or ISO capture time                 |
| `remove_photos_from_album`   | Remove only the selected album memberships                               |
| `move_photos_between_albums` | Confirm target membership, then remove source membership                 |
| `delete_container`           | Delete an album or folder while keeping its photos                       |
| `delete_photos`              | Mark photos deleted from the whole iCloud library, across synced devices |

Use `list_albums` to obtain container IDs and inspect the hierarchy first.
Moves reject cycles, invalid parents, smart albums, and sibling-name collisions.
`set_photo_metadata` accepts `photo_id`, optional `title`, `description`,
`favorite`, and `taken_at`. Omitted/null fields are unchanged; an empty string
clears a title/description. A date without an offset uses UTC. Metadata changes
show before/after values and leave the original file's EXIF untouched.

Removing photos from an album deletes **only** their relation records. Moving
between albums validates all photo IDs and both regular album IDs first, adds
and verifies each destination membership, and only then removes its source
membership. It does not remove photos from any other albums. A partial move
reports the affected photo and phase (`destination` or `source`); later items
are `not_attempted`. Inspect the current memberships before retrying. There is
no rollback that might accidentally remove a successfully preserved photo.

Actual `delete_container` and `delete_photos` calls additionally require:

```toml
ICLOUD_PHOTOS_ENABLE_DELETES = "1"
```

Add this alongside `ICLOUD_PHOTOS_ENABLE_WRITES = "1"` in the server environment
and restart. `doctor` reports the effective `deletes_enabled` state. Removing
an album membership, and moving memberships, need only the write gate.

Deleting an album does not delete its photos. Deleting a nonempty folder is
rejected unless `recursive: true` is explicitly supplied; a recursive plan
lists all affected containers and deletes children before parents, at most
100 containers per call. Photos always remain in the library. `delete_photos`
accepts at most 100 explicit photo IDs and sets the library deletion marker;
it never expunges originals or empties Recently Deleted. Availability and
retention of recovery follow Apple's current account rules. Deleting from the
library affects all synced devices, not just the current album.

All edit targets are looked up again with a fresh record change tag. The server
uses conflict-aware updates/deletes, not forced replacements. A conflicting
record or network failure yields `uncertain`/`unconfirmed`; remaining batch
items are skipped. A matching mutation response is required for confirmation.
A move to root preserves an explicit JSON null for the old parent field.
These are modeled private web API operations; tests use captured/local fixtures
and doubles. Real-account compatibility still needs a deliberate end-to-end
check after authentication. No live photos are changed by the test suite.

The editing tools carry destructive MCP annotations even during dry runs.
The server does not upload files, export originals, or permanently purge photos.

Metadata and previews returned by tools are sent to your MCP/AI client and may
reach its model provider. The server exposes no password entry or other iCloud
services.
