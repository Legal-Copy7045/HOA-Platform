from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from core import policy
from core.health import run_checks


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness/readiness probe. Reveals nothing beyond up/down."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


def home(request):
    if not request.user.is_authenticated:
        return render(request, "home.html", {})
    from core import dashboard

    hour = timezone.localtime().hour
    greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"
    return render(
        request, "home.html", {"dash": dashboard.build(request.user), "greeting": greeting}
    )


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
            "health": run_checks(),
            "scanner": settings.DOCUMENT_SCANNER,
            "require_clean_scan": settings.DOCUMENT_REQUIRE_CLEAN_SCAN,
        },
    )


@login_required
def export_my_data(request):
    """'Download my data' (JSON). Only the signed-in person's own records."""
    import json

    from django.http import HttpResponse

    from audit.services import record_event
    from core.exports import export_for

    record_event("export.own_data", request=request, actor=request.user)
    response = HttpResponse(
        json.dumps(export_for(request.user), indent=2, default=str), content_type="application/json"
    )
    response["Content-Disposition"] = 'attachment; filename="my-portal-data.json"'
    response["Cache-Control"] = "no-store"
    return response


@login_required
def feature_settings(request):
    """Board: switch optional features on and off."""
    from django.contrib import messages
    from django.shortcuts import redirect

    from core import features

    policy.require(request.user, "features.manage")
    if request.method == "POST":
        key = request.POST.get("feature", "")
        if key not in features.BY_KEY:
            messages.error(request, "Unknown feature.")
        else:
            on = request.POST.get("enabled") == "on"
            features.set_feature(key, on, actor=request.user, request=request)
            label = features.BY_KEY[key].label
            messages.success(request, f"{label} switched {'on' if on else 'off'}.")
        return redirect("feature_settings")
    states = features.states(request)
    return render(
        request,
        "core/features.html",
        {"rows": [(f, states[f.key]) for f in features.FEATURES]},
    )
