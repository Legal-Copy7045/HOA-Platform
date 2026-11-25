"""Follow-up dates on violations, collection cases and work orders: one
form, each object fetched through its own scoping and changed through its
own app's service."""

from __future__ import annotations

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from core import policy


def _parse(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return False


@login_required
@require_POST
def set_follow_up(request, kind, pk):
    on = _parse(request.POST.get("on", ""))
    user = request.user
    if kind == "violation":
        from compliance import services
        from compliance.models import Violation

        policy.require(user, "violation.manage")
        obj = Violation.objects.for_user(user).filter(pk=pk).first()
        setter, back = services.set_follow_up, "violation_detail"
    elif kind == "collection":
        from ledger import services
        from ledger.models import CollectionCase

        policy.require(user, "collections.manage")
        obj = CollectionCase.objects.for_user(user).filter(pk=pk).first()
        setter, back = services.set_case_follow_up, "collection_detail"
    elif kind == "work_order":
        from operations import services
        from operations.models import WorkOrder

        policy.require(user, "workorder.manage")
        obj = WorkOrder.objects.filter(pk=pk).first()
        setter, back = services.set_work_order_follow_up, "work_order_detail"
    else:
        raise Http404
    if obj is None:
        raise Http404
    if on is False:
        messages.error(request, "Enter a valid date.")
    else:
        setter(obj, on, actor=user, request=request)
        messages.success(request, "Follow-up date saved." if on else "Follow-up date cleared.")
    return redirect(reverse(back, args=[obj.pk]))
