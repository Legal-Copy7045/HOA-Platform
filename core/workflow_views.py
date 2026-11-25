"""A POST endpoint that applies one workflow transition to a scoped object."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.workflow import Workflow, WorkflowError


def transition_view(*, model, workflow: Workflow, detail_url_name: str):
    @login_required
    @require_POST
    def view(request, pk, action):
        obj = get_object_or_404(model.objects.for_user(request.user), pk=pk)
        if action not in workflow.transitions:
            messages.error(request, "Unknown action.")
            return redirect(detail_url_name, pk=obj.pk)
        try:
            workflow.apply(
                obj, action, actor=request.user, note=request.POST.get("note", ""), request=request
            )
        except WorkflowError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{workflow.transitions[action].label}: done.")
        return redirect(detail_url_name, pk=obj.pk)

    return view
