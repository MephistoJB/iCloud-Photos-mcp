# iCloud Photos MCP

This fork adds a **read-only stdio MCP server** that accesses iCloud Photos over
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

| Tool             | Purpose                                                               |
| ---------------- | --------------------------------------------------------------------- |
| `doctor`         | Validate the saved session and Photos discovery without login prompts |
| `list_libraries` | Discover library IDs                                                  |
| `list_albums`    | List up to 100 albums per page                                        |
| `search_photos`  | Filter by date, metadata text, favorite, and GPS bounds               |
| `get_photo`      | Fetch metadata by an explicit photo ID                                |
| `get_preview`    | Return a JPEG preview to the model without an original download       |

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

Metadata and previews returned by tools are sent to your MCP/AI client and may
reach its model provider. The server exposes no upload, deletion, album changes,
original-file export, password entry, or other iCloud services.
