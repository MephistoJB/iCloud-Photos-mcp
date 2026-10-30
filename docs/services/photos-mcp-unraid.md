# Unraid installation

The repository includes a Dockerfile, a stateless token-free HTTP entry point,
and a server-specific Unraid template at `deploy/unraid/iCloud-Photos-mcp.xml`.
This is a custom Docker application, not a Community Applications store listing.

## Build

Build the wheel from the checked-out Git revision first. Its package version is
derived from the Git tags by setuptools-scm, not manually assigned:

```sh
uv build --wheel --out-dir dist
docker build -t icloud-photos-mcp:local .
```

Keep exactly one wheel in `dist` when building. The minimal Docker build context
contains only that wheel and the Dockerfile; account sessions, `.env`, Git data,
and development environments are excluded. The image installs the CLI and MCP
extras, runs as UID 99/GID 100, and uses `/config` for persistent data.

## Configuration

Create `/mnt/user/appdata/icloud-photos-mcp` and its `session` subdirectory with
owner `99:100` and mode `700`. No MCP token is required.

Import the XML as a user template under
`/boot/config/plugins/dockerMan/templates-user/my-iCloud-Photos-mcp.xml`.
Set `ICLOUD_USERNAME` to the Apple Account identifier. The private session is
persisted at `/config/session`. The centralserver template binds port 8301 on
`192.168.1.16` (LAN) and `100.97.50.7` (Tailscale); edit the publish addresses,
`ICLOUD_PHOTOS_ALLOWED_NETWORKS` and explicit allowed-host list for a different server. Do not publish this service to the
public Internet. Use the Tailscale address for an encrypted network path.

The installed instance uses:

- Container: `iCloud-Photos-mcp`
- Image: `icloud-photos-mcp:local`
- Status page: `http://100.97.50.7:8301/`
- MCP on LAN: `http://192.168.1.16:8301/mcp`
- MCP on Tailscale: `http://100.97.50.7:8301/mcp`
- Session directory: `/config/session`

All HTTP requests require a source address in `ICLOUD_PHOTOS_ALLOWED_NETWORKS`.
The centralserver template permits loopback, LAN `192.168.1.0/24` and Tailscale
ranges. The application default permits only loopback and Tailscale; explicitly
configure your actual LAN range when deploying elsewhere. Invalid or empty ranges
prevent startup. Forwarded headers are not trusted. No Authorization header or MCP
token is needed. The container port is published on both server addresses.
Devices on the configured LAN or permitted by your Tailscale access policy can
call the MCP.
Explicit Host validation and Origin checks protect the MCP endpoint against DNS
rebinding. Health checks indicate service liveness, not a successful Apple login.

The template uses a read-only container filesystem, a private temporary volume,
no extra Linux capabilities, no new privileges, resource limits and rotated logs.
Writes and deletes both default to disabled. Existing tool dry-run defaults
still apply after opting in; see the [MCP guide](photos-mcp.md).

## Apple login

After installation, open the container console in Unraid and run:

```sh
icloud auth login --username "$ICLOUD_USERNAME" --session-dir /config/session
```

Use the normal Apple Account password and complete Apple's two-factor and
trusted-device prompts in that console. An app-specific Mail/Contacts password
cannot authenticate iCloud Photos web access. The MCP does not accept passwords
or verification codes through tool calls. If session creation requires account
terms acceptance, review and accept those yourself. With Advanced Data
Protection, trusted-device web authorization may be required. The service never
disables that protection automatically.

Restart the container after login, then call `doctor` with an MCP
client. Re-run login when Apple expires the session. Back up `/config` privately;
session cookies are credentials. A healthy container with a
missing/expired Apple session returns an actionable tool error.

## Updates

Rebuild the wheel and image from the desired Git revision and recreate the
container with the saved Unraid template. Preserve the `/config` volume. This
local image is not downloaded from a registry; Unraid's registry update button
is not a substitute for rebuilding it. No automatic account changes are run on
startup. The current installation does not require a local Apple Photos library.
