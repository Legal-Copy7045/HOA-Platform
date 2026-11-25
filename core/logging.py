"""Structured JSON logging to stdout.

Log records carry event names and identifiers only. Never pass personal data
(names, emails, addresses) or request bodies into a log call.
"""

import json
import logging
import traceback
from datetime import UTC, datetime

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": _event(record),
        }
        for key, value in vars(record).items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key == "request":
                # Never the request itself: its repr carries the full path and
                # query string, which can hold invitation or reset tokens.
                payload.update(_request_summary(value))
            elif isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = value
            else:
                payload[key] = f"<{type(value).__name__}>"
        if record.exc_info and record.exc_info[0]:
            # Type and frame locations only: exception messages and source lines can
            # echo user input or data.
            payload["exc_type"] = record.exc_info[0].__name__
            code = _error_code(record.exc_info[1])
            if code:
                payload["exc_code"] = code
            payload["exc_frames"] = [
                f"{frame.filename}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(payload, default=str)


# Django's own request/security loggers put the URL path into the message.
_PATH_LOGGERS = ("django.request", "django.security", "django.server", "django.channels")


def _event(record: logging.LogRecord) -> str:
    if record.name.startswith(_PATH_LOGGERS) and record.args:
        args = record.args if isinstance(record.args, tuple) else (record.args,)
        safe = tuple("[path]" if isinstance(a, str) and ("/" in a or "?" in a) else a for a in args)
        try:
            return str(record.msg) % safe
        except (TypeError, ValueError):
            return str(record.msg)
    return record.getMessage()


def _request_summary(request) -> dict:
    match = getattr(request, "resolver_match", None)
    return {
        "method": getattr(request, "method", None),
        "view": getattr(match, "view_name", None),
        "request_id": getattr(request, "request_id", None),
    }


def _error_code(exc) -> str:
    """A storage or API error's short code (e.g. "AccessDenied"): says what
    went wrong without the message, which can echo data."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    code = str((response.get("Error") or {}).get("Code") or "")
    return code if code.replace(".", "").replace("_", "").isalnum() and len(code) <= 64 else ""
