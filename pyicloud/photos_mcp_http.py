"""Private-network HTTP transport for the iCloud Photos MCP container."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
import logging
import os

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
import uvicorn

from pyicloud.photos_mcp import create_server


class NetworkAccess:
    """Accept configured private network clients without a bearer token."""

    def __init__(self, application: ASGIApp) -> None:
        self.application = application
        self.networks = tuple(
            ip_network(value.strip())
            for value in os.environ.get(
                "ICLOUD_PHOTOS_ALLOWED_NETWORKS",
                "127.0.0.0/8,::1/128,100.64.0.0/10,fd7a:115c:a1e0::/48",
            ).split(",")
            if value.strip()
        )
        if not self.networks:
            raise ValueError("Configure at least one trusted source network.")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            client = scope.get("client")
            try:
                address = ip_address(client[0] if client else "")
            except ValueError:
                address = None
            if address is None or not any(
                address in network for network in self.networks
            ):
                response = JSONResponse(
                    {"error": "private network required"}, status_code=403
                )
                await response(scope, receive, send)
                return
        await self.application(scope, receive, send)


async def health(_request: Request) -> JSONResponse:
    """Liveness does not contact Apple or expose account/session details."""
    return JSONResponse({"service": "iCloud-Photos-mcp", "status": "running"})


async def dashboard(_request: Request) -> HTMLResponse:
    """A small Unraid WebUI landing page without credentials or account data."""
    return HTMLResponse("""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iCloud Photos MCP</title><style>
body{font:17px system-ui;background:#111827;color:#e5e7eb;max-width:700px;margin:12vh auto;padding:24px}
h1{color:#fff}a{color:#93c5fd}code{background:#1f2937;padding:3px 7px;border-radius:5px}
</style><h1>iCloud Photos MCP</h1><p>The MCP service is running.</p>
<p>The <code>/mcp</code> endpoint is available without a token through the configured private networks.
Sign in to your Apple Account separately. This status page does not confirm an Apple login.</p>
<p><a href="https://github.com/MephistoJB/iCloud-Photos-mcp/blob/main/docs/services/photos-mcp-unraid.md">Setup and login</a></p>
<p><a href="/health">Service status</a></p></html>""")


def create_http_app() -> NetworkAccess:
    """Build stateless MCP with explicit trusted hosts and private network access."""
    hosts = [
        value.strip()
        for value in os.environ.get(
            "ICLOUD_PHOTOS_ALLOWED_HOSTS", "localhost:*,127.0.0.1:*"
        ).split(",")
        if value.strip()
    ]
    if not hosts or any("*" in host.removesuffix(":*") for host in hosts):
        raise ValueError(
            "Configure explicit trusted hosts; wildcard hostnames are not allowed."
        )
    server = create_server()
    server.settings.stateless_http = True
    server.settings.json_response = True
    server.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[],
    )
    server.custom_route("/health", methods=["GET"])(health)
    server.custom_route("/", methods=["GET"])(dashboard)
    return NetworkAccess(server.streamable_http_app())


def main() -> None:
    """Start the container service without logging requests or Apple internals."""
    logging.getLogger("pyicloud").setLevel(logging.CRITICAL)
    uvicorn.run(
        create_http_app(),
        host="0.0.0.0",
        port=8080,
        access_log=False,
        proxy_headers=False,
    )
