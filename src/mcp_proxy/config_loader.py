"""Configuration loader for MCP proxy.

This module provides functionality to load named server configurations from JSON files.
"""

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from mcp.client.stdio import StdioServerParameters

from .backend import BackendParameters, HttpServerParameters

logger = logging.getLogger(__name__)


def _string_map(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _backend_from_config(  # noqa: C901 - explicit validation of two disjoint config shapes.
    config: dict[str, Any],
    base_env: dict[str, str],
) -> BackendParameters:
    """Validate before constructing models so validation errors cannot expose secrets."""
    if ("command" in config) == ("url" in config):
        raise ValueError("Specify exactly one command or url")
    if "url" in config:
        url = config["url"]
        transport = config.get("transport")
        headers = config.get("headers", {})
        verify = config.get("verify_ssl")
        if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("Expected an HTTP(S) URL")
        if not urlsplit(url).hostname or transport not in {"sse", "streamablehttp"}:
            raise ValueError("HTTP backend requires host and explicit transport")
        if not _string_map(headers) or (verify is not None and not isinstance(verify, (bool, str))):
            raise ValueError("Invalid HTTP headers or verify_ssl type")
        if any(key in config for key in ("args", "env", "cwd", "auth")):
            raise ValueError("Unsupported HTTP backend fields")
        return HttpServerParameters(url, transport, headers, verify_ssl=verify)
    command, args, env = config["command"], config.get("args", []), config.get("env", {})
    cwd = config.get("cwd")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("Expected a nonempty command")
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("Expected string arguments")
    if not _string_map(env) or (cwd is not None and not isinstance(cwd, str)):
        raise ValueError("Invalid environment or cwd type")
    if any(key in config for key in ("transport", "headers", "verify_ssl", "auth")):
        raise ValueError("Unsupported stdio backend fields")
    return StdioServerParameters(command=command, args=args, env={**base_env, **env}, cwd=cwd)


def load_resilient_server_configs(
    path: str | Path,
    base_env: dict[str, str],
) -> dict[str, BackendParameters]:
    """Load strict mixed backends without echoing configuration values on failure."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
            raise TypeError("Expected mcpServers object")  # noqa: TRY301 - redact at boundary.
        result: dict[str, BackendParameters] = {}
        for name, config in data["mcpServers"].items():
            if not isinstance(config, dict) or not isinstance(config.get("enabled", True), bool):
                raise TypeError("Invalid backend entry")  # noqa: TRY301
            if not config.get("enabled", True):
                continue
            if not name or any(c in name for c in "/\\?#"):
                raise ValueError("Invalid backend name")  # noqa: TRY301
            result[name] = _backend_from_config(config, base_env)
    except (OSError, ValueError, TypeError):
        raise ValueError("Invalid or unreadable resilient server configuration") from None
    return result


def load_named_server_configs_from_file(
    config_file_path: str | Path,
    base_env: dict[str, str],
) -> dict[str, StdioServerParameters]:
    """Loads named server configurations from a JSON file.

    Args:
        config_file_path: Path to the JSON configuration file.
        base_env: The base environment dictionary to be inherited by servers.

    Returns:
        A dictionary of named server parameters.

    Raises:
        FileNotFoundError: If the config file is not found.
        json.JSONDecodeError: If the config file contains invalid JSON.
        ValueError: If the config file format is invalid.
    """
    named_stdio_params: dict[str, StdioServerParameters] = {}
    logger.info("Loading named server configurations from: %s", config_file_path)

    try:
        with Path(config_file_path).open() as f:
            config_data = json.load(f)
    except FileNotFoundError:
        logger.exception("Configuration file not found: %s", config_file_path)
        raise
    except json.JSONDecodeError:
        logger.exception("Error decoding JSON from configuration file: %s", config_file_path)
        raise
    except Exception as e:
        logger.exception(
            "Unexpected error opening or reading configuration file %s",
            config_file_path,
        )
        error_message = f"Could not read configuration file: {e}"
        raise ValueError(error_message) from e

    if not isinstance(config_data, dict) or "mcpServers" not in config_data:
        msg = f"Invalid config file format in {config_file_path}. Missing 'mcpServers' key."
        logger.error(msg)
        raise ValueError(msg)

    for name, server_config in config_data.get("mcpServers", {}).items():
        if not isinstance(server_config, dict):
            logger.warning(
                "Skipping invalid server config for '%s' in %s. Entry is not a dictionary.",
                name,
                config_file_path,
            )
            continue
        if not server_config.get("enabled", True):  # Default to True if 'enabled' is not present
            logger.info("Named server '%s' from config is not enabled. Skipping.", name)
            continue

        command = server_config.get("command")
        command_args = server_config.get("args", [])
        env = server_config.get("env", {})

        if not command:
            logger.warning(
                "Named server '%s' from config is missing 'command'. Skipping.",
                name,
            )
            continue
        if not isinstance(command_args, list):
            logger.warning(
                "Named server '%s' from config has invalid 'args' (must be a list). Skipping.",
                name,
            )
            continue

        new_env = base_env.copy()
        new_env.update(env)

        named_stdio_params[name] = StdioServerParameters(
            command=command,
            args=command_args,
            env=new_env,
            cwd=None,
        )
        logger.info(
            "Configured named server '%s' from config: %s %s",
            name,
            command,
            " ".join(command_args),
        )

    return named_stdio_params
