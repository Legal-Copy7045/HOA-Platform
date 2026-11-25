"""Decision D6: proxy headers count only when they come from a trusted proxy."""

import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from core.net import TrustedProxyMiddleware, client_ip

PROXY = "172.30.50.10"


@pytest.fixture(autouse=True)
def _trust_proxy(settings):
    settings.TRUSTED_PROXY_CIDRS = [f"{PROXY}/32"]
    settings.CLIENT_IP_HEADER = "CF-Connecting-IP"


def _run(rf: RequestFactory, remote: str, **headers):
    request = rf.get("/", REMOTE_ADDR=remote, **headers)
    TrustedProxyMiddleware(lambda r: HttpResponse())(request)
    return request


def test_trusted_proxy_supplies_client_ip_and_https(rf):
    request = _run(rf, PROXY, HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_PROTO="https")
    assert client_ip(request) == "198.51.100.7"
    assert request.is_secure()


def test_spoofed_headers_from_elsewhere_are_ignored_and_removed(rf):
    request = _run(
        rf,
        "203.0.113.50",
        HTTP_CF_CONNECTING_IP="198.51.100.7",
        HTTP_X_FORWARDED_PROTO="https",
        HTTP_X_FORWARDED_FOR="10.0.0.1",
    )
    assert client_ip(request) == "203.0.113.50"
    assert not request.is_secure()
    for key in ("HTTP_CF_CONNECTING_IP", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_FOR"):
        assert key not in request.META


def test_garbage_client_ip_header_falls_back_to_proxy_address(rf):
    request = _run(rf, PROXY, HTTP_CF_CONNECTING_IP="not-an-ip; DROP TABLE")
    assert client_ip(request) == PROXY


def test_nothing_trusted_by_default(rf, settings):
    settings.TRUSTED_PROXY_CIDRS = []
    request = _run(rf, PROXY, HTTP_CF_CONNECTING_IP="198.51.100.7", HTTP_X_FORWARDED_PROTO="https")
    assert client_ip(request) == PROXY
    assert not request.is_secure()


@pytest.mark.django_db
def test_healthz_is_not_redirected_to_https(client, settings):
    settings.SECURE_SSL_REDIRECT = True
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 301
