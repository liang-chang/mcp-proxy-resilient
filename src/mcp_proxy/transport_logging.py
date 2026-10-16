"""Keep third-party transport diagnostics out of recoverable connection logs."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_private: ContextVar[bool] = ContextVar("private_transport_logs", default=False)
_TRANSPORT_LOGGERS = ("httpx", "httpcore", "mcp.client", "mcp_proxy.httpx_client")


class _TransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not (_private.get() and record.name.startswith(_TRANSPORT_LOGGERS))


_filter = _TransportFilter()


@contextmanager
def private_transport_logs() -> Iterator[None]:
    """Suppress SDK wire logs only in this connection's inherited async context.

    HTTPX/SDK logs can contain arbitrary header values, URL tokens, response bodies
    and exception URLs. An allowlist of secret header names cannot protect them.
    The supervisor still emits sanitized state/error-class diagnostics. Filters
    remain installed, but are inert outside this context, including legacy mode.
    """
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and name.startswith(_TRANSPORT_LOGGERS):
            logger.addFilter(_filter)
    # bugfix: lazy HTTP imports may create new loggers after entering this scope.
    # Handler filtering covers those records without suppressing unrelated tasks.
    for handler in logging.getLogger().handlers:
        handler.addFilter(_filter)
    token = _private.set(True)
    try:
        yield
    finally:
        _private.reset(token)
