from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
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
