"""Logging setup shared by the API and the scheduler (`FOOTBALL_RESULTS_LOG_FORMAT`)."""

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


def configure_logging(log_format: Literal["text", "json"], level: int = logging.INFO) -> None:
    """Log to stderr in the given format; with `json`, uvicorn's own loggers use it too."""
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


def _writes_to_console(handler: logging.Handler) -> bool:
    """Ours, or a console handler a library installed (the MCP SDK adds a Rich handler)."""
    if getattr(handler, _OWN_HANDLER, False) or type(handler).__name__ == "RichHandler":
        return True
    return isinstance(handler, logging.StreamHandler) and handler.stream in (
        sys.stderr,
        sys.stdout,
    )
