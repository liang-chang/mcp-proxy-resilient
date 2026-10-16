"""Strict recoverable backend configuration and cache identity boundaries."""

import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from mcp.client.stdio import StdioServerParameters

import mcp_proxy.resilient_transports as transports
from mcp_proxy.backend import HttpServerParameters
from mcp_proxy.config_loader import load_resilient_server_configs
from mcp_proxy.resilient_transports import cache_identity_for


def test_mixed_backends_and_identity(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Mixed transports preserve credentials in memory and isolate cache identities."""
    path = tmp_path / "servers.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {"command": "app", "args": ["--test"], "env": {"TOKEN": "secret"}},
                    "remote": {
                        "url": "https://example.test/mcp",
                        "transport": "streamablehttp",
                        "headers": {"Authorization": "secret"},
                    },
                    "disabled": {"enabled": False},
                }
            }
        )
    )
    result = load_resilient_server_configs(path, {"BASE": "yes"})
    assert set(result) == {"local", "remote"}
    assert isinstance(result["local"], StdioServerParameters)
    assert result["local"].env == {"BASE": "yes", "TOKEN": "secret"}
    assert isinstance(result["remote"], HttpServerParameters)
    assert result["remote"].headers == {"Authorization": "secret"}
    assert (
        cache_identity_for("local", result["local"]) == "local:" + result["local"].model_dump_json()
    )
    original = cache_identity_for("remote", result["remote"])
    result["remote"].headers["Authorization"] = "changed"
    assert original != cache_identity_for("remote", result["remote"])
    result["remote"].auth = httpx.BasicAuth("user", "password")
    assert cache_identity_for("remote", result["remote"]) is None
    assert "secret" not in caplog.text


def test_http_connector_preserves_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frontend selection must not drop credentials or TLS settings."""
    connect = Mock()
    monkeypatch.setattr(transports, "http_connector", connect)
    auth = httpx.BasicAuth("user", "password")
    params = HttpServerParameters(
        "https://example.test/mcp",
        "streamablehttp",
        headers={"Authorization": "secret"},
        auth=auth,
        verify_ssl=False,
    )
    assert transports.connector_for(params) is connect.return_value
    connect.assert_called_once_with(
        params.url, transport=params.transport, headers=params.headers, auth=auth, verify_ssl=False
    )


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"command": "secret", "url": "https://secret"},
        {"url": "https://secret"},
        {"url": "file:///secret", "transport": "sse"},
        {"url": "https://secret", "transport": "invalid"},
        {"url": "https://secret", "transport": "sse", "headers": {"x": 1}},
        {"command": 123},
        {"command": "secret", "args": "secret"},
        {"command": "secret", "args": [1]},
        {"command": "secret", "env": []},
        {"command": "secret", "enabled": "false"},
    ],
)
def test_invalid_config_is_rejected_without_secret(tmp_path: Path, entry: dict) -> None:
    """Invalid values never appear in validation errors."""
    path = tmp_path / "servers.json"
    path.write_text(json.dumps({"mcpServers": {"backend": entry}}))
    with pytest.raises(ValueError, match="configuration") as error:
        load_resilient_server_configs(path, {})
    assert "secret" not in str(error.value)
