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
        request, "home.html", {"dash": dashboard.build(request.user, request), "greeting": greeting}
    )


def _update_status():
    from core.models import UpdateStatus

    status = UpdateStatus.objects.filter(pk=1).first()
    if status is None:
        return None
    agent_ok = bool(
        status.agent_seen_at
        and status.agent_seen_at > timezone.now() - __import__("datetime").timedelta(minutes=5)
    )
    host = _host_security(status.host_security)
    return {
        "s": status,
        "agent_ok": agent_ok,
        "changes": status.changes.splitlines()[:10],
        "host": host,
        "host_problem": bool(host) and not all(ok for _, _, ok in host),
    }


# Host security services the VM agent reports (infra/poc/harden-host.sh).
HOST_SECURITY_SERVICES = [
    ("updates", "Security updates"),
    ("livepatch", "Livepatch"),
    ("auditd", "Audit log"),
    ("integrity", "File integrity check"),
    ("falco", "Falco monitoring"),
    ("alerts", "Alert emails"),
]


def _host_security(states):
    """[(label, state, ok)] for the System status card; None before the
    hardening script has run (nothing reported active)."""
    states = states if isinstance(states, dict) else {}
    rows = [
        (label, str(states.get(key) or "not installed")[:20], states.get(key) == "active")
        for key, label in HOST_SECURITY_SERVICES
    ]
    if not any(ok for _, _, ok in rows):
        return None
    return rows


def _update_action(request, action):
    """Ask the VM's update agent to check GitHub, or to install the update.
    The web app never touches the server itself: it records the request and
    the agent on the host acts within a minute (decision D25)."""
    from django.contrib import messages
    from django.db import transaction
    from django.shortcuts import redirect

    from audit.services import record_event
    from core.models import UpdateStatus

    with transaction.atomic():
        UpdateStatus.get()
        status = UpdateStatus.objects.select_for_update().get(pk=1)
        if action == "check_updates":
            status.check_requested_at = timezone.now()
            status.save(update_fields=["check_requested_at"])
            messages.success(request, "Checking for updates: refresh in a minute.")
        elif status.state in ("requested", "running"):
            messages.info(request, "An update is already on its way.")
        elif not status.behind:
            messages.info(request, "The portal is already up to date.")
        else:
            status.state = "requested"
            status.state_changed_at = timezone.now()
            status.requested_by = request.user
            status.message = ""
            status.save(update_fields=["state", "state_changed_at", "requested_by", "message"])
            messages.success(
                request,
                "Update requested. It starts within a minute and takes a few minutes; "
                "the site may be unavailable for a few seconds at the end.",
            )
    record_event(f"system.{action}", actor=request.user, request=request, metadata={})
    return redirect("system_status")


def _send_test_email(request):
    """Only to the signed-in person's own address, so the button cannot be
    used to email anyone else. Audited."""
    from django.contrib import messages
    from django.core.mail import send_mail
    from django.shortcuts import redirect

    from audit.services import record_event
    from core.email import EmailUnavailable

    ok = True
    try:
        send_mail(
            f"[{settings.SITE_NAME}] Test email",
            "This is a test email from the portal. If you can read it, outgoing email works.",
            None,
            [request.user.email],
        )
    except EmailUnavailable:
        ok = False
    record_event("system.test_email_sent", actor=request.user, request=request, metadata={"ok": ok})
    if ok:
        where = "the redirect inbox" if settings.EMAIL_REDIRECT_ALL_TO else "your email address"
        messages.success(request, f"Test email sent to {where}. Check the inbox (and spam).")
    else:
        messages.error(
            request,
            "The mail server refused or did not answer. Check the EMAIL_ settings in .env "
            "(see Outgoing email below), restart, and try again.",
        )
    return redirect("system_status")


@login_required
def system_status(request):
    """Operations view for sysadmins: health only, no owner data (decision D9)."""
    policy.require(request.user, "system.view_status")
    if request.method == "POST" and request.POST.get("action") == "test_email":
        return _send_test_email(request)
    if request.method == "POST" and request.POST.get("action") in (
        "check_updates",
        "install_update",
    ):
        return _update_action(request, request.POST["action"])
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT status, count(*) FROM procrastinate_jobs GROUP BY status ORDER BY status"
        )
        jobs = cursor.fetchall()
        # Which kinds of job failed (task names only: never their arguments,
        # which can name people).
        cursor.execute(
            "SELECT task_name, count(*) FROM procrastinate_jobs WHERE status = 'failed' "
            "GROUP BY task_name ORDER BY count(*) DESC, task_name LIMIT 5"
        )
        failed_jobs = [(name.rsplit(".", 1)[-1], n) for name, n in cursor.fetchall()]
    return render(
        request,
        "core/system_status.html",
        {
            "version": settings.APP_VERSION,
            "pending_migrations": len(pending),
            "jobs": jobs,
            "failed_jobs": failed_jobs,
            "thin": policy.thinly_covered_actions(),
            "email_redirect": bool(settings.EMAIL_REDIRECT_ALL_TO),
            "health": run_checks(),
            "update": _update_status(),
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
    open_items = _feature_open_items()
    return render(
        request,
        "core/features.html",
        {
            "rows": [
                (f, states[f.key], _switch_off_warning(f, open_items.get(f.key)))
                for f in features.FEATURES
            ],
        },
    )


def _feature_open_items() -> dict[str, tuple[int, str, str]]:
    """Open records each feature would hide, for the switch-off warning
    (counts only; cheap queries)."""
    from compliance.models import Violation
    from core.scoping import system_access
    from governance.models import Election, RecordsRequest, Vote
    from operations.models import WorkOrder

    with system_access("feature switch-off warning (counts only)"):
        return {
            "votes": (
                Vote.objects.filter(status="open").count(),
                "vote is open",
                "votes are open",
            ),
            "elections": (
                Election.objects.exclude(state="certified").count(),
                "election is under way",
                "elections are under way",
            ),
            "violations": (
                Violation.objects.exclude(state__in=["cured", "dismissed", "closed"]).count(),
                "violation is open",
                "violations are open",
            ),
            "work_orders": (
                WorkOrder.objects.exclude(state__in=["completed", "closed", "cancelled"]).count(),
                "work order is open",
                "work orders are open",
            ),
            "records_requests": (
                RecordsRequest.objects.exclude(state__in=["fulfilled", "denied", "closed"]).count(),
                "records request is open",
                "records requests are open",
            ),
        }


def _switch_off_warning(feature, open_item) -> str:
    """What disappears when a feature is switched off, named for the board."""
    text = (
        f"Switch off {feature.label}? It leaves the menu for everyone, owners included, "
        "and its pages stop opening."
    )
    if open_item and open_item[0]:
        count, one, many = open_item
        text += f" {count} {one if count == 1 else many}."
    return text + " Its records are kept and come back if you switch it on again."
