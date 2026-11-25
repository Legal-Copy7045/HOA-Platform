from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from core import policy


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness/readiness probe. Reveals nothing beyond up/down."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


HOME_ACTIONS = (
    "lot.view_own",
    "lot.view_all",
    "person.manage",
    "audit.view_sysadmin_activity",
    "system.view_status",
    "invite.send",
)


def home(request):
    context = {}
    if request.user.is_authenticated:
        context["can"] = {a.replace(".", "_"): policy.can(request.user, a) for a in HOME_ACTIONS}
    return render(request, "home.html", context)


@login_required
def system_status(request):
    """Operations view for sysadmins: health only, no owner data (decision D9)."""
    policy.require(request.user, "system.view_status")
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status, count(*) FROM procrastinate_jobs GROUP BY status ORDER BY status"
        )
        jobs = cursor.fetchall()
    return render(
        request,
        "core/system_status.html",
        {
            "version": settings.APP_VERSION,
            "pending_migrations": len(pending),
            "jobs": jobs,
            "thin": policy.thinly_covered_actions(),
            "email_redirect": bool(settings.EMAIL_REDIRECT_ALL_TO),
        },
    )
