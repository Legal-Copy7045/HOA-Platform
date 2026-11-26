"""The Stripe client refuses redirects, so the key never reaches another host
(SEC-007 / R2-007). No network: a fake HTTPS handler answers."""

import email.message
import io
import urllib.request
import urllib.response
from unittest import mock

from payments import stripe_api


class _Fake302(urllib.request.BaseHandler):
    """Answers api.stripe.com with a redirect elsewhere and records every request."""

    handler_order = 100  # before the real HTTPSHandler

    def __init__(self):
        self.seen = []

    def https_open(self, req):
        self.seen.append(req.full_url)
        headers = email.message.Message()
        if req.full_url.startswith("https://api.stripe.com/"):
            headers["Location"] = "https://evil.example.com/steal"
            response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
            response.msg = "Found"
            return response
        response = urllib.response.addinfourl(io.BytesIO(b"{}"), headers, req.full_url, 200)
        response.msg = "OK"
        return response


def test_redirect_is_refused_and_key_not_forwarded():
    fake = _Fake302()
    opener = urllib.request.build_opener(fake, stripe_api._NoRedirect())
    with mock.patch.object(stripe_api, "_opener", opener):
        status, _body = stripe_api._transport(
            "GET",
            "https://api.stripe.com/v1/charges/ch_test1234",
            None,
            {"Authorization": "Bearer rk_test_fake"},
        )
    assert status == 302
    assert fake.seen == ["https://api.stripe.com/v1/charges/ch_test1234"]


def test_a_plain_opener_would_have_followed_it():
    """The fake really redirects: without the handler the key goes elsewhere."""
    fake = _Fake302()
    opener = urllib.request.build_opener(fake)
    request = urllib.request.Request(
        "https://api.stripe.com/v1/x", headers={"Authorization": "Bearer k"}
    )
    opener.open(request, timeout=1)
    assert fake.seen[-1] == "https://evil.example.com/steal"


def test_default_opener_carries_the_no_redirect_handler():
    opener = stripe_api.build_opener()
    assert any(isinstance(h, stripe_api._NoRedirect) for h in opener.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers)
