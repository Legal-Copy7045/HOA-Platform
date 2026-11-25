"""Board page for the contact-details retention rule."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from community import retention as rules
from community.models import RetentionRun
from community.rules import rule_with_citation
from core import policy


@login_required
def retention(request):
    policy.require(request.user, "retention.manage")
    days, citation = rule_with_citation(rules.RULE_KEY)
    return render(
        request,
        "community/retention.html",
        {
            "days": days or 0,
            "citation": citation,
            # Dry run: who the next run would clear, and what.
            "due": rules.candidates(),
            "runs": RetentionRun.objects.select_related("ran_by")[:10],
        },
    )


@login_required
@require_POST
def retention_run(request):
    policy.require(request.user, "retention.manage")
    result = rules.run(actor=request.user, request=request)
    if result is None:
        messages.error(request, "The retention rule is switched off (0 days).")
    else:
        messages.success(
            request,
            f"Done: contact details cleared for {result.people} "
            f"{'person' if result.people == 1 else 'people'}.",
        )
    return redirect("retention")
