"""An in-memory stand-in for USPS Informed Visibility (no network in tests).

Answers the sign-in (/oauth/authenticate), the refresh (/oauth/token) and
piece lookups (/ivws_api/informedvisapi/api/mt/get/piece/imb/<imb>) like the
real service: access tokens expire after ``token_seconds`` on the fake clock.
"""

from __future__ import annotations

import itertools
import json
import urllib.parse

USERNAME = "test-bcg-user"
PASSWORD = "test-only-pass$#word"  # nosec B105  # a fake, for the fake server


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeUsps:
    def __init__(self, clock: FakeClock, token_seconds: int = 900):
        self.clock = clock
        self.token_seconds = token_seconds
        self.calls: list[dict] = []
        self.tokens: dict[str, float] = {}  # access token -> expiry on the fake clock
        self.refresh_tokens: set[str] = set()
        self.refuse_login = False
        self.refuse_refresh = False
        # imb -> list of responses, consumed in order (the last one repeats):
        # ("scans", [scan dicts]) | 404 | 403 | 429 | 500
        self.pieces: dict[str, list] = {}
        self._n = itertools.count(1)

    # --- set-up helpers --------------------------------------------------------------
    def answer(self, imb: str, *responses) -> None:
        self.pieces[imb] = list(responses)

    def piece_calls(self) -> list[dict]:
        return [c for c in self.calls if "/api/mt/get/piece/imb/" in c["url"]]

    def auth_calls(self) -> list[dict]:
        return [c for c in self.calls if c["url"].endswith("/oauth/authenticate")]

    # --- the transport ---------------------------------------------------------------
    def __call__(self, method, url, data, headers):
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "url": url, "body": body, "headers": dict(headers)})
        path = urllib.parse.urlsplit(url).path
        if method == "POST" and path == "/oauth/authenticate":
            if self.refuse_login or body.get("password") != PASSWORD:
                return 401, b'{"error":"invalid_grant"}', {}
            if body.get("username") != USERNAME:
                return 401, b'{"error":"invalid_grant"}', {}
            return self._issue()
        if method == "POST" and path == "/oauth/token":
            if self.refuse_refresh or body.get("refresh_token") not in self.refresh_tokens:
                return 400, b'{"error":"invalid_grant"}', {}
            self.refresh_tokens.discard(body["refresh_token"])
            return self._issue()
        prefix = "/ivws_api/informedvisapi/api/mt/get/piece/imb/"
        if method == "GET" and path.startswith(prefix):
            token = headers.get("Authorization", "").removeprefix("Bearer ")
            if self.tokens.get(token, 0) <= self.clock.now:
                return 401, b'{"message":"unauthorized"}', {}
            imb = path[len(prefix) :]
            queue = self.pieces.get(imb) or [404]
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if answer == 429:
                return 429, b'{"message":"too many"}', {"Retry-After": "30"}
            if isinstance(answer, int):
                return answer, b'{"data":null,"message":"x"}', {}
            _, scans = answer
            envelope = {"data": {"imb": imb, "scans": scans}, "message": "success"}
            return 200, json.dumps(envelope).encode(), {}
        return 404, b"{}", {}

    def _issue(self):
        n = next(self._n)
        access, refresh = f"access-token-{n:04d}-xyz", f"refresh-token-{n:04d}-xyz"
        self.tokens[access] = self.clock.now + self.token_seconds
        self.refresh_tokens.add(refresh)
        payload = {
            "access_token": access,
            "token_type": "Bearer",  # nosec B105  # the token type, not a secret
            "expires_in": self.token_seconds,
            "refresh_token": refresh,
        }
        return 200, json.dumps(payload).encode(), {}


def scan(when: str, opcode: str, **extra) -> dict:
    entry = {
        "scan_date_time": when,
        "scan_event_code": opcode,
        "scan_facility_name": "PHILADELPHIA PA P&DC",
        "scan_facility_city": "PHILADELPHIA",
        "scan_facility_state": "PA",
        "scan_facility_zip": "19176",
        "scanner_type": "DBCS",
        "mail_phase": "Phase 1 - Origin Processing",
        "machine_name": "DBCS-012",
        "handling_event_type": "L",
    }
    entry.update(extra)
    return entry
