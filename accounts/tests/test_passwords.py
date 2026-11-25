import hashlib
import io
import urllib.error
from unittest import mock

import pytest
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from accounts.validators import BreachedPasswordValidator, breach_count

BREACHED = "password123456"


def _range_response(password: str, count: int = 42):
    digest = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324
    body = f"0000000000000000000000000000000000A:1\r\n{digest[5:]}:{count}\r\n"
    return mock.MagicMock(__enter__=lambda s: io.BytesIO(body.encode()), __exit__=lambda *a: None)


def test_breached_password_is_rejected(settings):
    settings.PASSWORD_BREACH_CHECK = True
    with (
        mock.patch("urllib.request.urlopen", return_value=_range_response(BREACHED)) as call,
        pytest.raises(ValidationError, match="data breach"),
    ):
        BreachedPasswordValidator().validate(BREACHED)
    sent_url = call.call_args.args[0].full_url
    assert sent_url.endswith(hashlib.sha1(BREACHED.encode()).hexdigest().upper()[:5])  # noqa: S324
    assert BREACHED not in sent_url


def test_unbreached_password_passes(settings):
    settings.PASSWORD_BREACH_CHECK = True
    with mock.patch("urllib.request.urlopen", return_value=_range_response("something else")):
        BreachedPasswordValidator().validate("a perfectly fresh passphrase")


def test_api_outage_fails_open_with_a_warning(settings, caplog):
    settings.PASSWORD_BREACH_CHECK = True
    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        assert breach_count("anything at all") is None
        BreachedPasswordValidator().validate("anything at all")
    assert "password.breach_check_unavailable" in caplog.text


@pytest.mark.parametrize("password", ["short", "elevenchars"])
def test_minimum_length_is_twelve(password):
    with pytest.raises(ValidationError):
        validate_password(password)


def test_long_simple_passphrase_needs_no_symbols():
    validate_password("rivers and orchards in autumn")
