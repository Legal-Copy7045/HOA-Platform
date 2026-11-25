"""Container healthcheck: GET /healthz using the first allowed hostname.

Sending a real allowed Host keeps strict ALLOWED_HOSTS intact in production.
"""

import os
import sys
import urllib.request

host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip() or "localhost"
request = urllib.request.Request(
    f"http://127.0.0.1:{os.environ.get('PORT', '8000')}/healthz", headers={"Host": host}
)
try:
    with urllib.request.urlopen(request, timeout=3) as response:  # nosec B310  # noqa: S310
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
