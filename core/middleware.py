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
        response["X-Request-ID"] = request.request_id
        # Browser features the portal never uses (review E10). Passkeys
        # (publickey-credentials-*) are deliberately left allowed.
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response
