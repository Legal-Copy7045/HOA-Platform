"""Bounced email actions (D74): the board marks a person's bouncing address
fixed; the board or manager says a bounced official notice's paper copy went
to a home another way. Both POST only, scoped, audited by the service."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from comms import bounces
from core import policy


@login_required
@require_POST
def person_email_bounce_clear(request, pk):
    from community.models import Person

    policy.require(request.user, "person.manage")
    person = get_object_or_404(Person.objects.for_user(request.user), pk=pk)
    cleared = bounces.clear(person, actor=request.user, request=request)
    if cleared:
        messages.success(
            request,
            "Marked as fixed. If the next email bounces too, it is shown here again.",
        )
    else:
        messages.info(request, "No bouncing address to clear.")
    return redirect(reverse("person_detail", args=[person.pk]) + "#contact")


@login_required
@require_POST
def bounce_paper_done(request, pk):
    from community.models import Lot

    policy.require(request.user, "mail.print")
    lot = get_object_or_404(Lot.objects.for_user(request.user), pk=pk)
    done = bounces.mark_paper_sent(lot, actor=request.user, request=request)
    if done:
        messages.success(request, f"Noted: the paper copy for {lot.label} was sent.")
    else:
        messages.info(request, "Nothing was waiting for that home.")
    return redirect(reverse("paper_mail") + "#bounced")
