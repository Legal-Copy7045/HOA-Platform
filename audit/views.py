import datetime

from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.utils import timezone

from audit.models import AuditEvent
from core import policy

# Sysadmin activity shows one window of days at a time, newest first (WT-013).
WINDOW_DAYS = 30


def sysadmin_events():
    """Everything sysadmins did, plus every change to who is a sysadmin and
    every command run on the server (D9). Never narrowed by default beyond
    the date window the page states."""
    return AuditEvent.objects.filter(
        Q(actor_is_sysadmin=True)
        | Q(action__startswith="role.sysadmin")
        | Q(action__in=["role.granted", "role.ended"], metadata__role="sysadmin")
        | Q(action="sysadmin.breakglass")
        | Q(channel="cli")
    ).select_related("actor")


def _day(value: str | None) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _bounds(day: datetime.date) -> datetime.datetime:
    """Midnight at the start of ``day`` in the site's time zone."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


ROWS_PER_PAGE = 100
DETAILS_PER_ROW = 20


def _collapse(events) -> list[dict]:
    """Consecutive events (newest first) with the same actor, channel and
    action on the same day become one row: count, first and last time, and
    the first DETAILS_PER_ROW events' details (WT-028)."""
    rows: list[dict] = []
    for e in events:
        day = timezone.localtime(e.occurred_at).date()
        key = (day, e.actor_id, e.actor_type, e.channel, e.action)
        if rows and rows[-1]["key"] == key:
            row = rows[-1]
            row["count"] += 1
            row["first"] = e
            if len(row["events"]) < DETAILS_PER_ROW:
                row["events"].append(e)
            continue
        rows.append({"key": key, "day": day, "event": e, "first": e, "count": 1, "events": [e]})
    for row in rows:
        row["hidden"] = row["count"] - len(row["events"])
    return rows


@login_required
def sysadmin_activity(request):
    """Sysadmin activity, a window of days at a time (the last 30 by
    default) with Older/Newer, grouped by day, filtered by person and
    action, and an audited CSV export of what is shown (WT-013)."""
    policy.require(request.user, "audit.view_sysadmin_activity")
    today = timezone.localdate()
    end = min(_day(request.GET.get("to")) or today, today)
    start = _day(request.GET.get("from")) or end - datetime.timedelta(days=WINDOW_DAYS - 1)
    if start > end:
        start, end = end, start
    everything = sysadmin_events()
    events = everything.filter(
        occurred_at__gte=_bounds(start),
        occurred_at__lt=_bounds(end + datetime.timedelta(days=1)),
    )
    # Choices come from the whole log, so a filter never offers an empty pick.
    people = dict(
        everything.filter(actor__isnull=False)
        .order_by()
        .values_list("actor_id", "actor__email")
        .distinct()
    )
    actions = sorted(set(everything.order_by().values_list("action", flat=True).distinct()))
    who = request.GET.get("who", "")
    action = request.GET.get("action", "")
    if who.isdigit() and int(who) in people:
        events = events.filter(actor_id=int(who))
    else:
        who = ""
    if action in actions:
        events = events.filter(action=action)
    else:
        action = ""
    events = list(events.order_by("-occurred_at", "-id"))

    from core.lists import csv_response, wants_csv

    if wants_csv(request):
        return csv_response(
            request,
            "sysadmin-activity",
            ["When", "Who", "Channel", "What", "Details"],
            (
                [
                    timezone.localtime(e.occurred_at).strftime("%Y-%m-%d %H:%M"),
                    e.actor.email if e.actor else e.get_actor_type_display(),
                    e.get_channel_display(),
                    e.action,
                    " ".join(f"{k}={v}" for k, v in (e.metadata or {}).items()),
                ]
                for e in events
            ),
            audit_kind="sysadmin_activity",
        )
    # Runs of the same thing by the same actor on the same day are one row
    # with a count (WT-028); the CSV export above stays one line per event.
    rows = _collapse(events)
    pages = max(1, -(-len(rows) // ROWS_PER_PAGE))
    page = int(request.GET["page"]) if request.GET.get("page", "").isdigit() else 1
    page = min(max(page, 1), pages)
    shown = rows[(page - 1) * ROWS_PER_PAGE : page * ROWS_PER_PAGE]
    days: dict[datetime.date, list] = {}
    for row in shown:
        days.setdefault(row["day"], []).append(row)
    span = (end - start).days + 1

    def page_url(n):
        query = request.GET.copy()
        query.pop("export", None)
        query["page"] = str(n)
        return "?" + query.urlencode()

    def window(new_end):
        query = request.GET.copy()
        query.pop("from", None)
        query.pop("export", None)
        query.pop("page", None)
        query["to"] = new_end.isoformat()
        return "?" + query.urlencode()

    newer_end = end + datetime.timedelta(days=span)
    export = request.GET.copy()
    export.pop("page", None)
    export["from"], export["to"], export["export"] = start.isoformat(), end.isoformat(), "csv"
    return render(
        request,
        "audit/sysadmin_activity.html",
        {
            "days": days,
            "count": len(events),
            "row_count": len(rows),
            "page": page,
            "pages": pages,
            "prev_page_url": page_url(page - 1) if page > 1 else "",
            "next_page_url": page_url(page + 1) if page < pages else "",
            "start": start,
            "end": end,
            "who": who,
            "action": action,
            "people": sorted(people.items(), key=lambda p: p[1]),
            "actions": actions,
            "older_url": window(start - datetime.timedelta(days=1)),
            "newer_url": window(min(newer_end, today)) if end < today else "",
            "export_url": "?" + export.urlencode(),
            "older_exists": everything.filter(occurred_at__lt=_bounds(start)).exists(),
            **_server_tasks(request.user),
        },
    )


SERVER_TASK_ROWS = 5


def _server_tasks(user) -> dict:
    """Server task history for the board (WT-007): who asked for what, when
    and how it went, with the backups line. The board reads it here; only
    sysadmins start tasks, on Server tasks."""
    if not policy.can(user, "system.view_tasks"):
        return {}
    from core import host_tasks
    from core.models import HostTask
    from core.server_task_views import HistoryRow

    rows = HostTask.objects.select_related("requested_by")[:SERVER_TASK_ROWS]
    return {
        "server_tasks": [HistoryRow(t) for t in rows],
        "server_backups": host_tasks.backups_overview(),
        "server_tasks_more": HostTask.objects.count() > SERVER_TASK_ROWS,
    }
