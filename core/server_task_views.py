"""Admin -> Server tasks (decision D36): start an allowlisted host task and
see the history. Sysadmins start tasks; the board sees the history.
Settings -> Demo data (D53): a sysadmin reseeds a demo or test portal.
System status -> Restart the portal (D91): a sysadmin restarts web and worker."""

from __future__ import annotations

from allauth.account.decorators import reauthentication_required
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render

from community import demo
from core import demo_reseed, host_tasks, policy
from core.models import HostTask

HISTORY_ROWS = 50


@login_required
def server_tasks(request):
    policy.require(request.user, "system.view_tasks")
    if request.method == "POST":
        policy.require(request.user, "system.run_task")
        kind = request.POST.get("kind", "")
        if kind == HostTask.Kind.RESEED_DEMO and demo_reseed.allowed():
            # Needs a dataset and the typed word: started from Demo data only.
            return redirect("demo_data")
        if kind == HostTask.Kind.RESTART_APP:
            # Needs its confirmation step and a fresh sign-in check (D91).
            return redirect("system_restart")
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
        if task.kind == HostTask.Kind.RESEED_DEMO and task.dataset in demo.BY_NAME:
            self.label = f"{self.label} ({demo.BY_NAME[task.dataset].label})"
        self.summary = host_tasks.clean(task.summary, 500)
        self.tail = host_tasks.clean(task.output_tail, 8400)


def _update_summary():
    from core.models import UpdateStatus

    return UpdateStatus.objects.filter(pk=1).first()


@login_required
def system_restart(request):
    """System status -> Restart the portal (D91): sysadmins only (others 403
    before anything else), then a fresh password or passkey check, then this
    confirmation page. POST asks the server agent for the fixed restart_app
    task; the agent restarts web and worker with both compose files."""
    policy.require(request.user, "system.run_task")
    return _system_restart(request)


@reauthentication_required
def _system_restart(request):
    if request.method == "POST":
        try:
            host_tasks.request_task(
                request.user,
                HostTask.Kind.RESTART_APP,
                request=request,
                confirmation=request.POST.get("confirm", ""),
            )
        except host_tasks.HostTaskRefused as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                "Restart requested. The server starts it within a minute; the portal is "
                "unavailable for up to a minute, then this page shows the result.",
            )
        return redirect("system_restart")
    status = host_tasks.current()
    last = host_tasks.last_restart()
    wait = host_tasks.restart_wait()
    return render(
        request,
        "core/system_restart.html",
        {
            "confirm_text": host_tasks.RESTART_CONFIRM_TEXT,
            "confirm_value": host_tasks.RESTART_CONFIRM_VALUE,
            "current": HistoryRow(status) if status else None,
            "last": HistoryRow(last) if last else None,
            "wait_minutes": max(1, -(-int(wait.total_seconds()) // 60)) if wait else 0,
            "agent_warning": not host_tasks.agent_ok()
            and not (status and status.state == HostTask.State.RUNNING),
        },
    )


class ReseedForm(forms.Form):
    dataset = forms.ChoiceField(label="Dataset", choices=[(d.name, d.label) for d in demo.DATASETS])
    attach_requester = forms.BooleanField(
        label=(
            "After reseeding, make me a board member and the owner of a demo home in the new data"
        ),
        required=False,
        initial=True,
    )
    confirm = forms.CharField(
        label=f"Type {host_tasks.RESEED_WORD} to confirm",
        max_length=20,
        widget=forms.TextInput(attrs={"autocomplete": "off", "spellcheck": "false"}),
    )

    def clean_confirm(self):
        value = self.cleaned_data["confirm"].strip()
        if value != host_tasks.RESEED_WORD:
            raise forms.ValidationError(f"Type {host_tasks.RESEED_WORD} in capitals.")
        return value


@login_required
def demo_data(request):
    """Settings -> Demo data: only where demo data is allowed (404 elsewhere),
    sysadmins only. Asks the server agent to reseed; never does it here."""
    if not demo_reseed.allowed():
        raise Http404
    policy.require(request.user, "system.run_task")
    form = ReseedForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            host_tasks.request_task(
                request.user,
                HostTask.Kind.RESEED_DEMO,
                request=request,
                dataset=form.cleaned_data["dataset"],
                confirmation=form.cleaned_data["confirm"],
                attach_requester=form.cleaned_data["attach_requester"],
            )
        except host_tasks.HostTaskRefused as exc:
            form.add_error(None, str(exc))
        else:
            label = demo.get(form.cleaned_data["dataset"]).label
            messages.success(
                request,
                f"Reseed with “{label}”: requested. The server starts within a minute and "
                "the portal is unavailable for about a minute; then sign in again.",
            )
            return redirect("demo_data")
    status = host_tasks.current()
    return render(
        request,
        "core/demo_data.html",
        {
            "form": form,
            "datasets": demo.DATASETS,
            "last_seed": demo_reseed.last_seed(),
            "labels": {d.name: d.label for d in demo.DATASETS},
            "reseeds": [HistoryRow(t) for t in demo_reseed.recent_reseeds()],
            "current": HistoryRow(status) if status else None,
            "confirm_text": host_tasks.RESEED_CONFIRM_TEXT,
            "word": host_tasks.RESEED_WORD,
            "agent_warning": not host_tasks.agent_ok()
            and not (status and status.state == HostTask.State.RUNNING),
        },
    )
