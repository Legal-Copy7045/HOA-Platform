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


def test_storage_error_codes_are_logged_without_the_message():
    """So a refused upload says why (e.g. AccessDenied), without data."""
    import logging
    import sys

    from botocore.exceptions import ClientError

    from core.logging import JsonFormatter

    try:
        raise ClientError(
            {"Error": {"Code": "InvalidArgument", "Message": "secret detail lot 12"}}, "PutObject"
        )
    except ClientError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    line = JsonFormatter().format(record)
    assert '"exc_code": "InvalidArgument"' in line
    assert "secret detail" not in line


def test_uploads_send_checksums_only_when_required(monkeypatch):
    from core.tests.test_production_settings import _load

    config = _load(monkeypatch).STORAGES["default"]["OPTIONS"]["client_config"]
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
