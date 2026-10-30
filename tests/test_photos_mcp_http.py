"""Container HTTP network isolation and MCP transport without Apple/network access."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient
from starlette.types import Scope
import uvicorn

from pyicloud import photos_mcp_http as http


@pytest.fixture(name="client")
def http_client() -> Any:
    """Run the ASGI lifespan in memory with a private synthetic source address."""
    with (
        patch(
            "pydantic_settings.sources.providers.dotenv."
            "DotEnvSettingsSource._read_env_files",
            return_value={},
        ),
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ALLOWED_HOSTS": "testserver"}),
        TestClient(http.create_http_app(), client=("100.90.1.2", 12345)) as client,
    ):
        yield client


def test_public_status_does_not_expose_account(client: Any) -> None:
    """Liveness is available inside trusted networks, but has no Apple details."""
    assert client.get("/health").json() == {
        "service": "iCloud-Photos-mcp",
        "status": "running",
    }
    assert "Apple login" in client.get("/").text
    assert "session" not in client.get("/health").text


@pytest.mark.parametrize("source", ["192.168.1.20", "203.0.113.1", "invalid", None])
def test_rejects_untrusted_sources(source: Any) -> None:
    """LAN/public clients cannot bypass isolation with forwarded headers."""
    application = AsyncMock()
    app = http.NetworkAccess(application)
    scope: Scope = {
        "type": "http",
        "client": (source, 12345) if source else None,
        "headers": [(b"x-forwarded-for", b"100.90.1.2")],
    }
    send = AsyncMock()
    asyncio.run(app(scope, AsyncMock(), send))
    assert send.call_args_list[0].args[0]["status"] == 403
    application.assert_not_awaited()


def test_token_free_mcp_and_host_checks(client: Any) -> None:
    """A real initialization/tool-list exchange succeeds through the wrapper."""
    headers = {
        "Accept": "application/json, text/event-stream",
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    assert client.post("/mcp", json=request, headers=headers).status_code == 200
    response = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        },
    )
    assert response.status_code == 200
    assert len(response.json()["result"]["tools"]) == 16
    assert (
        client.post(
            "/mcp", json=request, headers={**headers, "Host": "evil.example"}
        ).status_code
        == 421
    )
    assert (
        client.post(
            "/mcp", json=request, headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )


@pytest.mark.parametrize("hosts", ["", "*", "*.example.com:8080"])
def test_invalid_host_configuration(hosts: str) -> None:
    """Do not silently allow arbitrary Host headers on a network service."""
    with (
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ALLOWED_HOSTS": hosts}),
        pytest.raises(ValueError, match="trusted hosts"),
    ):
        http.create_http_app()


def test_default_configuration_and_main() -> None:
    """Loopback is the default trusted host; startup never logs request headers."""
    with (
        patch.dict("os.environ", {}, clear=True),
        patch(
            "pydantic_settings.sources.providers.dotenv."
            "DotEnvSettingsSource._read_env_files",
            return_value={},
        ),
    ):
        app = http.create_http_app()
        assert isinstance(app, http.NetworkAccess)
    with (
        patch.object(http, "create_http_app") as factory,
        patch.object(uvicorn, "run") as run,
    ):
        http.main()
    run.assert_called_once_with(
        factory.return_value,
        host="0.0.0.0",
        port=8080,
        access_log=False,
        proxy_headers=False,
    )


@pytest.mark.parametrize(
    "source", ["192.168.1.20", "100.90.1.2", "127.0.0.1", "::1", "fd7a:115c:a1e0::1"]
)
def test_configured_private_networks(source: str) -> None:
    """The deployed LAN and Tailscale ranges allow direct clients without tokens."""
    with patch.dict(
        "os.environ",
        {
            "ICLOUD_PHOTOS_ALLOWED_NETWORKS": (
                "192.168.1.0/24,100.64.0.0/10,127.0.0.0/8,::1/128,fd7a:115c:a1e0::/48"
            )
        },
    ):
        application = AsyncMock()
        app = http.NetworkAccess(application)
    scope: Scope = {"type": "http", "client": (source, 12345)}
    receive, send = AsyncMock(), AsyncMock()
    asyncio.run(app(scope, receive, send))
    application.assert_awaited_once_with(scope, receive, send)


@pytest.mark.parametrize("networks", ["", " , ", "192.168.1.1/invalid"])
def test_invalid_network_configuration(networks: str) -> None:
    """Invalid ranges fail closed rather than accepting arbitrary clients."""
    with (
        patch.dict("os.environ", {"ICLOUD_PHOTOS_ALLOWED_NETWORKS": networks}),
        pytest.raises(ValueError),
    ):
        http.NetworkAccess(AsyncMock())
