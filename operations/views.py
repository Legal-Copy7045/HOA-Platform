from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from community.models import Person
from core import policy
from operations import services
from operations.models import BoardTask, TaskStatus


class TaskForm(forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))
    assignee = forms.ModelChoiceField(queryset=Person.objects.none(), required=False)
    due_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["assignee"].queryset = Person.objects.for_user(user).filter(user__isnull=False)


@login_required
def tasks(request):
    policy.require(request.user, "task.manage")
    form = TaskForm(request.POST or None, user=request.user)
    if request.method == "POST":
        if form.is_valid():
            services.create_task(actor=request.user, request=request, **form.cleaned_data)
            messages.success(request, "Task added.")
        else:
            messages.error(request, "A task needs a title.")
        return redirect("tasks")
    return render(
        request,
        "operations/tasks.html",
        {
            "open": BoardTask.objects.filter(status=TaskStatus.OPEN).select_related("assignee"),
            "closed": BoardTask.objects.exclude(status=TaskStatus.OPEN)[:50],
            "form": form,
        },
    )


@login_required
@require_POST
def task_close(request, pk, status):
    policy.require(request.user, "task.manage")
    task = get_object_or_404(BoardTask, pk=pk)
    try:
        services.close_task(task, status=status, actor=request.user, request=request)
    except services.OperationsError as exc:
        messages.error(request, str(exc))
    return redirect("tasks")


# --- Compliance calendar (Milestone 4) ------------------------------------------------


@login_required
def compliance(request):
    import datetime

    from django.contrib import messages

    from community.models import Person
    from operations.models import ComplianceCategory, ComplianceItem, Recurrence

    policy.require(request.user, "compliance.manage")
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "starters":
                n = services.add_starter_items(actor=request.user)
                messages.success(request, f"{n} suggested item(s) added: set their real dates.")
            elif action == "done":
                item = get_object_or_404(ComplianceItem, pk=request.POST.get("item"))
                seen = request.POST.get("due_on")
                services.complete_compliance_item(
                    item,
                    actor=request.user,
                    seen_due_on=datetime.date.fromisoformat(seen) if seen else None,
                    request=request,
                )
                messages.success(request, "Marked done.")
            else:
                item = None
                if request.POST.get("item"):
                    item = get_object_or_404(ComplianceItem, pk=request.POST["item"])
                responsible = None
                if request.POST.get("responsible"):
                    responsible = get_object_or_404(
                        Person.objects.for_user(request.user), pk=request.POST["responsible"]
                    )
                services.save_compliance_item(
                    item=item,
                    title=request.POST.get("title", ""),
                    category=request.POST.get("category") or "other",
                    due_on=datetime.date.fromisoformat(request.POST.get("due_on", "")),
                    recurrence=request.POST.get("recurrence") or "annual",
                    remind_days=int(request.POST.get("remind_days") or 30),  # ValueError -> message
                    notes=request.POST.get("notes", ""),
                    citation=request.POST.get("citation", ""),
                    responsible=responsible,
                    actor=request.user,
                    request=request,
                )
                messages.success(request, "Saved.")
        except (services.OperationsError, ValueError) as exc:
            messages.error(
                request,
                str(exc)
                if isinstance(exc, services.OperationsError)
                else "Check the date and the reminder days.",
            )
        return redirect("compliance")
    today = timezone.localdate()
    from community.services import staff_people

    return render(
        request,
        "operations/compliance.html",
        {
            "items": ComplianceItem.objects.filter(active=True).select_related("responsible"),
            "done": ComplianceItem.objects.filter(active=False).order_by("-last_done_on")[:20],
            "soon": {i.pk for i in services.due_soon(today)},
            "today": today,
            "categories": ComplianceCategory.choices,
            "recurrences": Recurrence.choices,
            "people": staff_people("compliance.manage"),
        },
    )
