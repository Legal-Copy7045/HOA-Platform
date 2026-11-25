"""Record every portal request made by a sysadmin (decision D9d)."""

from audit.services import record_event
from core import policy


class SysadminRequestAuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        match = getattr(request, "resolver_match", None)
        if (
            user is not None
            and user.is_authenticated
            and match is not None
            and match.url_name not in ("healthz",)
            and policy.is_sysadmin(user)
        ):
            record_event(
                "sysadmin.request",
                request=request,
                actor=user,
                metadata={
                    "view": (match.view_name or "")[:100],
                    "method": request.method,
                    "status": response.status_code,
                },
            )
        return response
