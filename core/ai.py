"""Gemini, used only to read scanned, signed ARC sign-off forms.

Sends the scanned file and the list of expected neighbours (reference and
address) and asks for JSON: which reference each signature is for, approve
or object, the printed name, the date and a confidence. Nothing here decides
anything: results are proposals that a person confirms (arc.signoffs).

Standard library only (urllib). The key comes from GEMINI_API_KEY and is sent
in a header, never in the URL. No personal data is logged.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

log = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_BYTES = 15 * 1024 * 1024  # inline data limit, with headroom
ALLOWED = {"application/pdf", "image/png", "image/jpeg"}


class AIError(RuntimeError):
    """Reading failed; the message is safe to show to staff."""


PROMPT = (
    "This is a scanned paper form on which neighbours approve or object to a homeowner's "
    "architectural project. Each signature block has a reference like SO-12. For every "
    "block that has been filled in, return: reference, position ('approve' or 'object', "
    "from the ticked box), signer_name (as printed), signed_on (YYYY-MM-DD if a date is "
    "written, else empty), and confidence (0-100, how sure you are of the position and "
    "name). Skip blocks left blank. Expected blocks: {expected}"
)

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "reference": {"type": "STRING"},
            "position": {"type": "STRING", "enum": ["approve", "object"]},
            "signer_name": {"type": "STRING"},
            "signed_on": {"type": "STRING"},
            "confidence": {"type": "INTEGER"},
        },
        "required": ["reference", "position", "signer_name", "confidence"],
    },
}


def read_signed_form(data: bytes, content_type: str, expected: list[dict]) -> list[dict]:
    if not settings.GEMINI_API_KEY:
        raise AIError("No Gemini key is configured.")
    if content_type not in ALLOWED:
        raise AIError("Only PDF, PNG or JPEG scans can be read.")
    if len(data) > MAX_BYTES:
        raise AIError("The scan is too large to read automatically (15 MB at most).")
    body = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT.format(expected=json.dumps(expected))},
                    {
                        "inline_data": {
                            "mime_type": content_type,
                            "data": base64.b64encode(data).decode(),
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0,
        },
    }
    url = ENDPOINT.format(model=settings.GEMINI_MODEL)
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": settings.GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310  # nosec B310
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        log.warning("gemini request failed", extra={"status": exc.code})
        raise AIError(f"The reading service refused the request (HTTP {exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.warning("gemini request failed", extra={"exc_type": type(exc).__name__})
        raise AIError("The reading service could not be reached.") from exc
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
        items = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise AIError("The reading service returned something unexpected.") from exc
    if not isinstance(items, list):
        raise AIError("The reading service returned something unexpected.")
    return [i for i in items if isinstance(i, dict)][:200]
