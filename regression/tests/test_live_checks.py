"""regression/run.py live stage: the Cloudflare Access check reads the
redirect's host, not a substring of the whole URL (CodeQL
py/incomplete-url-substring-sanitization). No database or network needed."""

from __future__ import annotations

import pytest

from regression import run


@pytest.mark.parametrize(
    "location",
    [
        "https://oakcreek.cloudflareaccess.com/cdn-cgi/access/login/dev.example.org",
        "https://cloudflareaccess.com/login",
    ],
)
def test_access_redirects_are_recognised(location):
    assert run._is_cloudflare_access(location)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/?next=cloudflareaccess.com",
        "https://cloudflareaccess.com.evil.example/login",
        "https://notcloudflareaccess.com/login",
        "/accounts/login/",
        "",
    ],
)
def test_other_redirects_are_not(location):
    assert not run._is_cloudflare_access(location)
