"""The entry point for the mcp-proxy application. It sets up the logging and runs the main function.

Two ways to run the application:
1. Run the application as a module `uv run -m mcp_proxy`
2. Run the application as a package `uv run mcp-proxy-resilient`

"""

import argparse
import asyncio
import json
import logging
import math
import os
import shlex
import sys
import typing as t
from pathlib import Path

from httpx import Auth, BasicAuth, DigestAuth
from httpx_auth import OAuth2ClientCredentials
from mcp.client.stdio import StdioServerParameters

from . import APP_NAME, __version__
from .backend import BackendParameters, HttpServerParameters
from .config_loader import load_named_server_configs_from_file, load_resilient_server_configs
from .mcp_server import DEFAULT_EXPOSE_HEADERS, MCPServerSettings, run_mcp_server
from .resilient import ResilienceSettings
from .resilient_transports import cache_identity_for, connector_for, run_resilient_stdio
from .sse_client import run_sse_client
from .streamablehttp_client import run_streamablehttp_client

# Deprecated env var. Here for backwards compatibility.
SSE_URL: t.Final[str | None] = os.getenv(
    "SSE_URL",
    None,
)


def _normalize_verify_ssl(value: str | bool | None) -> bool | str | None:
    """Normalize the verify_ssl argument into bool, str path, or None."""
    if isinstance(value, bool) or value is None:
        return value

    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False

    return value


def _positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return seconds


class _PortAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,  # noqa: ARG002 - argparse callback signature.
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,  # noqa: ARG002
    ) -> None:
        setattr(namespace, self.dest, values)
        namespace.explicit_port = True


def _resolve_frontend(args: argparse.Namespace) -> t.Literal["stdio", "http"]:
    """Retain legacy inference unless the user explicitly chooses a frontend."""
    is_url = bool(args.command_or_url and args.command_or_url.startswith(("http://", "https://")))
    frontend = args.frontend or ("stdio" if is_url else "http")
    named = bool(args.named_server_config or args.named_server_definitions)
    if frontend == "stdio" and args.frontend and getattr(args, "explicit_port", False):
        raise ValueError("A stdio frontend cannot bind --port/--sse-port")
    if args.server and (frontend != "stdio" or not args.named_server_config or args.command_or_url):
        raise ValueError(
            "--server requires stdio frontend and named config without a positional target"
        )
    if args.frontend == "stdio" and named and not args.server:
        raise ValueError("Named stdio frontend requires --named-server-config and --server")
    if not args.resilient and (args.server or (args.frontend and ((frontend == "http") == is_url))):
        raise ValueError("Same-transport frontends and --server require --resilient")
    return t.cast("t.Literal['stdio', 'http']", frontend)


def _resilience_settings(args: argparse.Namespace) -> ResilienceSettings | None:
    if not getattr(args, "resilient", False):
        return None
    return ResilienceSettings(
        connect_timeout=args.connect_timeout,
        retry_interval=args.retry_interval,
        tool_cache_dir=args.tool_cache_dir,
    )


def _setup_argument_parser() -> argparse.ArgumentParser:
    """Set up and return the argument parser for the MCP proxy."""
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description=("Start the MCP proxy in one of two possible modes: as a client or a server."),
        epilog=(
            "Examples:\n"
            "  mcp-proxy-resilient http://localhost:8080/sse\n"
            "  mcp-proxy-resilient --no-verify-ssl https://server.local/sse\n"
            "  mcp-proxy-resilient --transport streamablehttp http://localhost:8080/mcp\n"
            "  mcp-proxy-resilient --transport streamablehttp --username user --password pass "
            "https://server.local/mcp\n"
            "  mcp-proxy-resilient --headers Authorization 'Bearer YOUR_TOKEN' http://localhost:8080/sse\n"
            "  mcp-proxy-resilient --port 8080 -- your-command --arg1 value1 --arg2 value2\n"
            "  mcp-proxy-resilient --named-server fetch 'uvx mcp-server-fetch' --port 8080\n"
            "  mcp-proxy-resilient your-command --port 8080 "
            "-e KEY VALUE -e ANOTHER_KEY ANOTHER_VALUE\n"
            "  mcp-proxy-resilient your-command --port 8080 --allow-origin='*'\n"
            "  mcp-proxy-resilient --resilient --port 18080 --named-server-config servers.json\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    _add_arguments_to_parser(parser)
    return parser


def _add_arguments_to_parser(parser: argparse.ArgumentParser) -> None:
    """Add all arguments to the argument parser."""
    recovery = parser.add_argument_group("Resilient catalog proxy (all transport combinations)")
    recovery.add_argument(
        "--frontend",
        choices=["stdio", "http"],
        help="Client-facing transport; omitted preserves legacy inference",
    )
    recovery.add_argument("--server", help="Select one named backend for the stdio frontend")
    recovery.add_argument(
        "--resilient", action="store_true", help="Keep proxy alive when backend fails"
    )
    recovery.add_argument(
        "--connect-timeout",
        type=_positive_seconds,
        default=10,
        help="Resilient mode: connection/initialization/discovery timeout in seconds (default: 10)",
    )
    recovery.add_argument(
        "--retry-interval",
        type=_positive_seconds,
        default=5,
        help="Resilient mode: initial retry and idle health-check interval in seconds (default: 5)",
    )
    recovery.add_argument(
        "--tool-cache-dir",
        type=Path,
        help="Optional persistent tools/resources/prompts metadata cache",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {__version__}",
        help="Show the version and exit",
    )

    parser.add_argument(
        "command_or_url",
        help=(
            "Command or URL to connect to. When a URL, will run an SSE/StreamableHTTP client. "
            "Otherwise, if --named-server is not used, this will be the command "
            "for the default stdio client. If --named-server is used, this argument "
            "is ignored for stdio mode unless no default server is desired. "
            "See corresponding options for more details."
        ),
        nargs="?",
        default=SSE_URL,
    )

    client_group = parser.add_argument_group("SSE/StreamableHTTP client options")
    client_group.add_argument(
        "-H",
        "--headers",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help="Headers to pass to the SSE server. Can be used multiple times.",
        default=[],
    )
    client_group.add_argument(
        "--transport",
        choices=["sse", "streamablehttp"],
        default="sse",  # For backwards compatibility
        help="The transport to use for the client. Default is SSE.",
    )
    client_group.add_argument(
        "--client-id",
        type=str,
        help="OAuth2 client ID for authentication",
    )
    client_group.add_argument(
        "--client-secret",
        type=str,
        help="OAuth2 client secret for authentication",
    )
    client_group.add_argument(
        "--token-url",
        type=str,
        help="OAuth2 token URL for authentication",
    )
    client_group.add_argument(
        "--username",
        type=str,
        help="Username for authentication",
    )
    client_group.add_argument(
        "--password",
        type=str,
        help="Password for authentication",
    )
    client_group.add_argument(
        "--digest",
        action="store_true",
        help="Use digest authentication",
    )
    client_group.add_argument(
        "--verify-ssl",
        nargs="?",
        const=True,
        default=None,
        metavar="VALUE",
        dest="verify_ssl",
        help=(
            "Control SSL verification when acting as a client. Use without a value to "
            "force verification, pass 'false' to disable, or provide a path to a PEM bundle."
        ),
    )
    client_group.add_argument(
        "--no-verify-ssl",
        dest="verify_ssl",
        action="store_const",
        const=False,
        help=("Disable SSL verification (alias for --verify-ssl false)."),
    )

    stdio_client_options = parser.add_argument_group("stdio client options")
    stdio_client_options.add_argument(
        "args",
        nargs="*",
        help=(
            "Any extra arguments to the command to spawn the default server. "
            "Ignored if only named servers are defined."
        ),
    )
    stdio_client_options.add_argument(
        "-e",
        "--env",
        nargs=2,
        action="append",
        metavar=("KEY", "VALUE"),
        help=(
            "Environment variables used when spawning the default server. Can be "
            "used multiple times. For named servers, environment is inherited or "
            "passed via --pass-environment."
        ),
        default=[],
    )
    stdio_client_options.add_argument(
        "--cwd",
        default=None,
        help=(
            "The working directory to use when spawning the default server process. "
            "Named servers inherit the proxy's CWD."
        ),
    )
    stdio_client_options.add_argument(
        "--pass-environment",
        action=argparse.BooleanOptionalAction,
        help="Pass through all environment variables when spawning all server processes.",
        default=False,
    )
    stdio_client_options.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        metavar="LEVEL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the log level. Default is INFO.",
    )
    stdio_client_options.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        help=(
            "Enable debug mode with detailed logging output. Equivalent to --log-level DEBUG. "
            "If both --debug and --log-level are provided, --debug takes precedence."
        ),
        default=False,
    )
    stdio_client_options.add_argument(
        "--named-server",
        action="append",
        nargs=2,
        metavar=("NAME", "COMMAND_STRING"),
        help=(
            "Define a named stdio server. NAME is for the URL path /servers/NAME/. "
            "COMMAND_STRING is a single string with the command and its arguments "
            "(e.g., 'uvx mcp-server-fetch --timeout 10'). "
            "These servers inherit the proxy's CWD and environment from --pass-environment."
        ),
        default=[],
        dest="named_server_definitions",
    )
    stdio_client_options.add_argument(
        "--named-server-config",
        type=str,
        default=None,
        metavar="FILE_PATH",
        help=(
            "Path to a JSON configuration file for named stdio servers. "
            "If provided, this will be the exclusive source for named server definitions, "
            "and any --named-server CLI arguments will be ignored."
        ),
    )

    mcp_server_group = parser.add_argument_group("SSE server options")
    mcp_server_group.add_argument(
        "--port",
        action=_PortAction,
        type=int,
        default=0,
        help="Port to expose an SSE server on. Default is a random port",
    )
    mcp_server_group.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to expose an SSE server on. Default is 127.0.0.1",
    )
    mcp_server_group.add_argument(
        "--stateless",
        action=argparse.BooleanOptionalAction,
        help="Enable stateless mode for streamable http transports. Default is False",
        default=False,
    )
    mcp_server_group.add_argument(
        "--sse-port",
        action=_PortAction,
        type=int,
        default=0,
        help="(deprecated) Same as --port",
    )
    mcp_server_group.add_argument(
        "--sse-host",
        default="127.0.0.1",
        help="(deprecated) Same as --host",
    )
    mcp_server_group.add_argument(
        "--allow-origin",
        nargs="+",
        default=[],
        help=(
            "Allowed origins for the SSE server. Can be used multiple times. "
            "Default is no CORS allowed."
        ),
    )
    mcp_server_group.add_argument(
        "--expose-header",
        action="append",
        dest="expose_headers",
        metavar="HEADER",
        default=None,
        help=(
            "Headers to expose via Access-Control-Expose-Headers. "
            "Defaults to 'Mcp-Session-Id'. Can be used multiple times."
        ),
    )


def _setup_logging(*, level: str, debug: bool) -> logging.Logger:
    """Set up logging configuration and return the logger."""
    logging.basicConfig(
        level=logging.DEBUG if debug else level,
        format="[%(levelname)1.1s %(asctime)s.%(msecs).03d %(name)s] %(message)s",
    )
    return logging.getLogger(__name__)


def _http_parameters(
    args_parsed: argparse.Namespace,
    logger: logging.Logger,
    verify_ssl: bool | str | None = None,
) -> HttpServerParameters:
    """Share HTTP credentials and TLS options between both frontends."""
    if args_parsed.named_server_definitions:
        logger.warning(
            "--named-server arguments are ignored when command_or_url is an HTTP/HTTPS URL "
            "(SSE/StreamableHTTP client mode).",
        )
    # Start a client connected to the SSE server, and expose as a stdio server
    logger.debug("Starting SSE/StreamableHTTP client and stdio server")
    headers = dict(args_parsed.headers)
    if api_access_token := os.getenv("API_ACCESS_TOKEN", None):
        headers["Authorization"] = f"Bearer {api_access_token}"

    # Collect client credentials if provided
    client_id = args_parsed.client_id
    client_secret = args_parsed.client_secret
    token_url = args_parsed.token_url
    username = args_parsed.username
    password = args_parsed.password
    digest = args_parsed.digest

    auth: Auth | None = None
    if client_id and client_secret and token_url:
        auth = OAuth2ClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
        )
    elif username and password:
        auth = DigestAuth(username, password) if digest else BasicAuth(username, password)
    elif username or password or digest:
        logger.error("Both --username and --password are required")
        sys.exit(1)
    else:
        auth = None

    return HttpServerParameters(
        args_parsed.command_or_url,
        args_parsed.transport,
        headers=headers,
        auth=auth,
        verify_ssl=verify_ssl,
    )


def _handle_sse_client_mode(
    args_parsed: argparse.Namespace,
    logger: logging.Logger,
    verify_ssl: bool | str | None = None,
) -> None:
    """Handle legacy SSE/StreamableHTTP client mode operation."""
    params = _http_parameters(args_parsed, logger, verify_ssl)
    headers, auth = params.headers, params.auth
    recovery_options = {}
    if settings := _resilience_settings(args_parsed):
        recovery_options["resilience"] = settings
    if args_parsed.transport == "streamablehttp":
        asyncio.run(
            run_streamablehttp_client(
                args_parsed.command_or_url,
                headers=headers,
                auth=auth,
                verify_ssl=verify_ssl,
                **recovery_options,
            ),
        )
    else:
        asyncio.run(
            run_sse_client(
                args_parsed.command_or_url,
                headers=headers,
                auth=auth,
                verify_ssl=verify_ssl,
                **recovery_options,
            ),
        )


def _configure_default_server(
    args_parsed: argparse.Namespace,
    base_env: dict[str, str],
    logger: logging.Logger,
) -> StdioServerParameters | None:
    """Configure the default server if applicable."""
    if not (
        args_parsed.command_or_url
        and not args_parsed.command_or_url.startswith(("http://", "https://"))
    ):
        return None

    default_server_env = base_env.copy()
    default_server_env.update(dict(args_parsed.env))  # Specific env vars for default server

    default_stdio_params = StdioServerParameters(
        command=args_parsed.command_or_url,
        args=args_parsed.args,
        env=default_server_env,
        cwd=args_parsed.cwd or None,
    )
    if not getattr(args_parsed, "resilient", False):
        logger.info(
            "Configured default server: %s %s",
            args_parsed.command_or_url,
            " ".join(args_parsed.args),
        )
    return default_stdio_params


def _load_named_servers_from_config(
    config_path: str,
    base_env: dict[str, str],
    logger: logging.Logger,
) -> dict[str, StdioServerParameters]:
    """Load named server configurations from a file."""
    try:
        return load_named_server_configs_from_file(config_path, base_env)
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        # Specific errors are already logged by the loader function
        # We log a generic message here before exiting
        logger.exception(
            "Failed to load server configurations from %s. Exiting.",
            config_path,
        )
        sys.exit(1)
    except Exception:  # Catch any other unexpected errors from loader
        logger.exception(
            "An unexpected error occurred while loading server configurations from %s. Exiting.",
            config_path,
        )
        sys.exit(1)


def _configure_named_servers_from_cli(
    named_server_definitions: list[tuple[str, str]],
    base_env: dict[str, str],
    logger: logging.Logger,
) -> dict[str, StdioServerParameters]:
    """Configure named servers from CLI arguments."""
    named_stdio_params: dict[str, StdioServerParameters] = {}

    for name, command_string in named_server_definitions:
        try:
            command_parts = shlex.split(command_string)
            if not command_parts:  # Handle empty command_string
                logger.error("Empty COMMAND_STRING for named server '%s'. Skipping.", name)
                continue

            command = command_parts[0]
            command_args = command_parts[1:]
            # Named servers inherit base_env (which includes passed-through env)
            # and use the proxy's CWD.
            named_stdio_params[name] = StdioServerParameters(
                command=command,
                args=command_args,
                env=base_env.copy(),  # Each named server gets a copy of the base env
                cwd=None,  # Named servers run in the proxy's CWD
            )
            logger.info("Configured named server '%s': %s", name, command_string)
        except IndexError:  # Should be caught by the check for empty command_parts
            logger.exception(
                "Invalid COMMAND_STRING for named server '%s': '%s'. Must include a command.",
                name,
                command_string,
            )
            sys.exit(1)
        except Exception:
            logger.exception("Error parsing COMMAND_STRING for named server '%s'", name)
            sys.exit(1)

    return named_stdio_params


def _create_mcp_settings(args_parsed: argparse.Namespace) -> MCPServerSettings:
    """Create MCP server settings from parsed arguments."""
    expose_headers = (
        list(DEFAULT_EXPOSE_HEADERS)
        if not args_parsed.expose_headers
        else list(args_parsed.expose_headers)
    )
    return MCPServerSettings(
        bind_host=args_parsed.host if args_parsed.host is not None else args_parsed.sse_host,
        port=args_parsed.port if args_parsed.port is not None else args_parsed.sse_port,
        stateless=args_parsed.stateless,
        allow_origins=args_parsed.allow_origin if len(args_parsed.allow_origin) > 0 else None,
        expose_headers=expose_headers,
        log_level="DEBUG" if args_parsed.debug else args_parsed.log_level,
        resilience=_resilience_settings(args_parsed),
    )


def main() -> None:  # noqa: C901 - retain the legacy routing path unchanged.
    """Start the client using asyncio."""
    parser = _setup_argument_parser()
    args_parsed = parser.parse_args()
    logger = _setup_logging(level=args_parsed.log_level, debug=args_parsed.debug)
    logger.info("%s %s (resilient mode: %s)", APP_NAME, __version__, args_parsed.resilient)
    try:
        frontend = _resolve_frontend(args_parsed)
    except ValueError as exc:
        parser.error(str(exc))

    if args_parsed.resilient:
        try:
            _run_resilient(args_parsed, frontend, logger)
        except ValueError as exc:
            parser.error(str(exc))
        return

    # Validate required arguments
    if (
        not args_parsed.command_or_url
        and not args_parsed.named_server_definitions
        and not args_parsed.named_server_config
    ):
        parser.print_help()
        logger.error(
            "Either a command_or_url for a default server or at least one --named-server "
            "(or --named-server-config) must be provided for stdio mode.",
        )
        sys.exit(1)

    # Handle SSE client mode if URL is provided
    if args_parsed.command_or_url and args_parsed.command_or_url.startswith(
        ("http://", "https://"),
    ):
        verify_ssl = _normalize_verify_ssl(getattr(args_parsed, "verify_ssl", None))
        _handle_sse_client_mode(args_parsed, logger, verify_ssl=verify_ssl)
        return

    # Start stdio client(s) and expose as an SSE server
    logger.debug("Configuring stdio client(s) and SSE server")

    # Base environment for all spawned processes
    base_env: dict[str, str] = {}
    if args_parsed.pass_environment:
        base_env.update(os.environ)

    # Configure default server
    default_stdio_params = _configure_default_server(args_parsed, base_env, logger)

    # Configure named servers
    named_stdio_params: dict[str, StdioServerParameters] = {}
    if args_parsed.named_server_config:
        if args_parsed.named_server_definitions:
            logger.warning(
                "--named-server CLI arguments are ignored when --named-server-config is provided.",
            )
        named_stdio_params = _load_named_servers_from_config(
            args_parsed.named_server_config,
            base_env,
            logger,
        )
    elif args_parsed.named_server_definitions:
        named_stdio_params = _configure_named_servers_from_cli(
            args_parsed.named_server_definitions,
            base_env,
            logger,
        )

    # Ensure at least one server is configured
    if not default_stdio_params and not named_stdio_params:
        parser.print_help()
        logger.error(
            "No stdio servers configured. Provide a default command or use --named-server.",
        )
        sys.exit(1)

    # Create MCP server settings and run the server
    mcp_settings = _create_mcp_settings(args_parsed)
    asyncio.run(
        run_mcp_server(
            default_server_params=default_stdio_params,
            named_server_params=named_stdio_params,
            mcp_settings=mcp_settings,
        ),
    )


def _run_resilient(args: argparse.Namespace, frontend: str, logger: logging.Logger) -> None:
    """Resolve backend descriptions once, then compose either frontend with the same core."""
    base_env = dict(os.environ) if args.pass_environment else {}
    default: BackendParameters | None = None
    if args.command_or_url and args.command_or_url.startswith(("http://", "https://")):
        default = _http_parameters(args, logger, _normalize_verify_ssl(args.verify_ssl))
    else:
        default = _configure_default_server(args, base_env, logger)
    named: dict[str, BackendParameters] = {}
    if args.named_server_config:
        named = load_resilient_server_configs(args.named_server_config, base_env)
    elif args.named_server_definitions:
        # Suppress command-string logging in the legacy CLI parser; arguments may be secrets.
        quiet = logging.getLogger("resilient-config")
        quiet.setLevel(logging.CRITICAL)
        named.update(
            _configure_named_servers_from_cli(args.named_server_definitions, base_env, quiet)
        )
    if frontend == "stdio":
        if args.server:
            if args.server not in named:
                raise ValueError("Selected server is unknown or disabled")
            selected, name = named[args.server], args.server
        elif default is not None and not named:
            selected, name = default, "default"
        else:
            raise ValueError("Stdio frontend requires exactly one selected backend")
        settings = _resilience_settings(args)
        if settings is None:
            raise ValueError("This frontend requires --resilient")
        asyncio.run(
            run_resilient_stdio(
                connector_for(selected), settings, cache_identity=cache_identity_for(name, selected)
            )
        )
    else:
        if default is None and not named:
            raise ValueError("No enabled backend configured")
        asyncio.run(run_mcp_server(_create_mcp_settings(args), default, named))


if __name__ == "__main__":
    main()
