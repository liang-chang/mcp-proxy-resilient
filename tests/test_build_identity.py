"""The fork must be identifiable even when invoked through Python or a renamed executable."""

import pytest

from mcp_proxy import APP_NAME, __version__
from mcp_proxy.__main__ import _setup_argument_parser


def test_version_identifies_fork_without_relying_on_executable_name(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic invocation must not look like the upstream mcp-proxy release."""
    monkeypatch.setattr("sys.argv", ["unrelated-launcher.exe"])
    with pytest.raises(SystemExit) as result:
        _setup_argument_parser().parse_args(["--version"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert output.strip() == f"{APP_NAME} {__version__}"
    assert "+resilient." in __version__


def test_help_examples_use_distinct_command() -> None:
    """Copying a new help example must not select the old executable on PATH."""
    help_text = _setup_argument_parser().format_help()
    assert "mcp-proxy-resilient --" in help_text
    assert "  mcp-proxy --" not in help_text
