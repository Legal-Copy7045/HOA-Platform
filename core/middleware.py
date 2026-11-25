import logging
import uuid

from django.http import HttpRequest, HttpResponse

PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
    "microphone=(), payment=(), usb=(), browsing-topics=()"
)


class RequestIDMiddleware:
    """Gives every request a random ID, echoed in X-Request-ID and audit events.

    An incoming X-Request-ID is ignored: clients must not choose IDs that end
    up in the audit log.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.request_id = uuid.uuid4().hex
        response = self.get_response(request)
        if response.status_code >= 500:
            # Count only (monitoring): no URL, no message, no user.
            try:
                from core.health import record_server_error

                record_server_error()
            except Exception as exc:  # counting must never break a response
                logging.getLogger(__name__).warning(
                    "error tally failed", extra={"exc_type": type(exc).__name__}
                )
        response["X-Request-ID"] = request.request_id
        # Browser features the portal never uses (review E10). Passkeys
        # (publickey-credentials-*) are deliberately left allowed.
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response
