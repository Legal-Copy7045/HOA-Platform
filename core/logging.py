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
            "event": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0]:
            # Type and frame locations only: exception messages and source lines can
            # echo user input or data.
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc_frames"] = [
                f"{frame.filename}:{frame.lineno} in {frame.name}"
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(payload, default=str)
