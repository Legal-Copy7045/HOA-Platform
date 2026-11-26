"""Admin -> Server tasks (decision D35): start an allowlisted host task and
see the history. Sysadmins start tasks; the board sees the history."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from core import host_tasks, policy
from core.models import HostTask

HISTORY_ROWS = 50


@login_required
def server_tasks(request):
    policy.require(request.user, "system.view_tasks")
    if request.method == "POST":
        policy.require(request.user, "system.run_task")
        kind = request.POST.get("kind", "")
        try:
            host_tasks.request_task(request.user, kind, request=request)
        except host_tasks.HostTaskRefused as exc:
            messages.error(request, str(exc))
        else:
            label = host_tasks.BY_KIND[kind].label
            messages.success(
                request,
                f"{label}: requested. The server starts it within a minute; "
                "this page shows the result when it is done.",
            )
        # Only the one known page: never a URL taken from the form.
        if request.POST.get("next") == "system_status":
            return redirect("system_status")
        return redirect("server_tasks")
    can_run = policy.can(request.user, "system.run_task")
    rows = HostTask.objects.select_related("requested_by")[:HISTORY_ROWS]
    history = [HistoryRow(t) for t in rows]
    status = host_tasks.current()
    return render(
        request,
        "core/server_tasks.html",
        {
            "tasks": host_tasks.offered(),
            "can_run": can_run,
            "current": HistoryRow(status) if status else None,
            "history": history,
            # While a long task runs the agent is busy, not missing.
            "agent_warning": not host_tasks.agent_ok()
            and not (status and status.state == HostTask.State.RUNNING),
            "agent_seen": host_tasks.agent_seen(),
            "backups": host_tasks.backups_overview(),
            "update": _update_summary(),
        },
    )


class HistoryRow:
    """A task as shown: agent-written text cleaned again before display."""

    def __init__(self, task: HostTask):
        self.task = task
        spec = host_tasks.BY_KIND.get(task.kind)
        self.label = spec.label if spec else "Unknown task"
        self.summary = host_tasks.clean(task.summary, 500)
        self.tail = host_tasks.clean(task.output_tail, 8400)


def _update_summary():
    from core.models import UpdateStatus

    return UpdateStatus.objects.filter(pk=1).first()
