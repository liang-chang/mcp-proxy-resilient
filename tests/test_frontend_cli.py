"""Explicit frontends preserve legacy defaults and reject ambiguous invocations."""

import json
import logging
from pathlib import Path

import pytest

from mcp_proxy.__main__ import _resolve_frontend, _run_resilient, _setup_argument_parser


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["http://localhost/mcp"], "stdio"),
        (["some-command"], "http"),
        (["--resilient", "--frontend", "http", "http://localhost/mcp"], "http"),
        (["--resilient", "--frontend", "stdio", "some-command"], "stdio"),
        (["--resilient", "--frontend", "stdio", "--", "command", "--port", "1234"], "stdio"),
    ],
)
def test_frontend_resolution(argv: list[str], expected: str) -> None:
    """The explicit choice overrides the preserved legacy defaults."""
    assert _resolve_frontend(_setup_argument_parser().parse_args(argv)) == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["--frontend", "stdio", "command"],
        ["--frontend", "http", "https://example.test"],
        ["--resilient", "--frontend", "stdio", "--port", "0", "command"],
        ["--resilient", "--frontend", "stdio", "--sse-port", "12", "command"],
        ["--resilient", "--frontend", "stdio", "--named-server-config", "servers.json"],
        ["--resilient", "--server", "one", "--named-server-config", "servers.json"],
        ["--resilient", "--frontend", "stdio", "--server", "one", "command"],
        [
            "--resilient",
            "--frontend",
            "stdio",
            "--server",
            "one",
            "--named-server-config",
            "servers.json",
            "command",
        ],
    ],
)
def test_frontend_conflicts(argv: list[str]) -> None:
    """Reject ambiguity before starting any process or listener."""
    with pytest.raises(ValueError, match=r"requires?|cannot"):
        _resolve_frontend(_setup_argument_parser().parse_args(argv))


@pytest.mark.parametrize("name", ["unknown", "disabled"])
def test_unknown_or_disabled_selection(tmp_path: Path, name: str) -> None:
    """Reject an unavailable named selection before starting any backend."""
    config = tmp_path / "servers.json"
    config.write_text(json.dumps({"mcpServers": {"disabled": {"enabled": False}}}))
    args = _setup_argument_parser().parse_args(
        [
            "--resilient",
            "--frontend",
            "stdio",
            "--named-server-config",
            str(config),
            "--server",
            name,
        ]
    )
    with pytest.raises(ValueError, match="unknown or disabled"):
        _run_resilient(args, "stdio", logging.getLogger(__name__))
