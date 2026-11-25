import json
import logging
import sys

from core.logging import JsonFormatter


def test_json_formatter_drops_exception_messages():
    formatter = JsonFormatter()
    try:
        raise ValueError("owner@example.com is not valid")
    except ValueError:
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "auth.failed", None, exc_info=True
        )
        record.exc_info = sys.exc_info()
    payload = json.loads(formatter.format(record))
    assert payload["event"] == "auth.failed"
    assert payload["exc_type"] == "ValueError"
    assert "owner@example.com" not in json.dumps(payload)
