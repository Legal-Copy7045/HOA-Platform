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
            # Monitoring: the hourly count, and the error's signature (class,
            # route name, code location; never the URL, message or user, D45).
            try:
                from core.health import record_server_error

                record_server_error()
            except Exception as exc:  # counting must never break a response
                logging.getLogger(__name__).warning(
                    "error tally failed", extra={"exc_type": type(exc).__name__}
                )
            try:
                from core import errors

                errors.record(request, response.status_code)
            except Exception as exc:  # recording must never break a response
                logging.getLogger(__name__).warning(
                    "error signature failed", extra={"exc_type": type(exc).__name__}
                )
        response["X-Request-ID"] = request.request_id
        # Browser features the portal never uses (review E10). Passkeys
        # (publickey-credentials-*) are deliberately left allowed.
        response.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
        return response

    def process_exception(self, request, exception):
        """Note the unhandled exception's class and code location for the
        error signature (D45). Never handles it: Django still answers 500."""
        try:
            from core import errors

            errors.capture_exception(request, exception)
        except Exception as exc:  # monitoring must never break a response
            logging.getLogger(__name__).warning(
                "error capture failed", extra={"exc_type": type(exc).__name__}
            )
        return None


class EmailUnavailableMiddleware:
    """When the mail server is down, say so plainly instead of a 500."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        from django.shortcuts import render

        from core.email import EmailUnavailable

        if isinstance(exception, EmailUnavailable):
            from core.errors import EMAIL_UNAVAILABLE, label_error

            label_error(request, EMAIL_UNAVAILABLE)
            return render(request, "email_unavailable.html", status=503)
        return None
