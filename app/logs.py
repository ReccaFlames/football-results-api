"""Logging setup shared by the API and the scheduler (`FOOTBALL_RESULTS_LOG_FORMAT`)."""

import ipaddress
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal

TEXT_FORMAT = "%(asctime)s %(levelname)s %(message)s"
_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}
_OWN_HANDLER = "_football_results_handler"
_ACCESS_FIELDS = ("client", "method", "path", "http_version", "status")


def anonymize_ip(value: str) -> str:
    """The network of an IP address: /24 for IPv4, /48 for IPv6; other values unchanged.

    Accepts uvicorn's `host:port` and `[host]:port` client strings and drops the port.
    """
    host = value
    if value.startswith("["):
        host = value[1:].split("]", 1)[0]
    elif value.count(":") == 1:
        host = value.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False).network_address)


class AnonymizeClientFilter(logging.Filter):
    """Replaces the client address of uvicorn access records with its network (GDPR)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[0], str):
            record.args = (anonymize_ip(args[0]), *args[1:])
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, plus any `extra` fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # uvicorn access records carry the request as positional arguments.
        args = record.args
        if record.name == "uvicorn.access" and isinstance(args, tuple) and len(args) == 5:
            entry.update(zip(_ACCESS_FIELDS, args, strict=True))
        entry.update(
            (key, value)
            for key, value in vars(record).items()
            if key not in _RESERVED and not key.startswith("_")
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(
    log_format: Literal["text", "json"],
    level: int = logging.INFO,
    *,
    anonymize_ips: bool = False,
) -> None:
    """Log to stderr in the given format; with `json`, uvicorn's own loggers use it too.

    With ``anonymize_ips``, access logs keep only the client's /24 (IPv4) or /48 (IPv6) network.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if _writes_to_console(handler):
            root.removeHandler(handler)
    handler = logging.StreamHandler()
    setattr(handler, _OWN_HANDLER, True)
    handler.setFormatter(
        JsonFormatter() if log_format == "json" else logging.Formatter(TEXT_FORMAT)
    )
    root.addHandler(handler)
    root.setLevel(level)
    if log_format == "json":
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    for item in list(access.filters):
        if isinstance(item, AnonymizeClientFilter):
            access.removeFilter(item)
    if anonymize_ips:
        access.addFilter(AnonymizeClientFilter())


def _writes_to_console(handler: logging.Handler) -> bool:
    """Ours, or a console handler a library installed (the MCP SDK adds a Rich handler)."""
    if getattr(handler, _OWN_HANDLER, False) or type(handler).__name__ == "RichHandler":
        return True
    return isinstance(handler, logging.StreamHandler) and handler.stream in (
        sys.stderr,
        sys.stdout,
    )
