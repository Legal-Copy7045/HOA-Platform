"""Board/sysadmin actions on logins: invitations and assisted MFA resets."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts import mfa_reset, services
from accounts.board_forms import StartResetForm
from accounts.models import Invitation, MfaReset
from community.models import Person
from core import policy


@login_required
def invitations(request):
    policy.require(request.user, "invite.resend")
    rows = Invitation.objects.for_user(request.user).select_related("person")[:200]
    # Sysadmins see invitation status only, not the register (decision D9).
    return render(request, "accounts/board/invitations.html", {"invitations": rows})


@login_required
@require_POST
def invite_person(request, pk):
    # Board only: an invitation decides who controls a person's identity, so a
    # sysadmin (who sees no people, D9) cannot create one (review finding A1).
    policy.require(request.user, "invite.send")
    person = get_object_or_404(Person.objects.for_user(request.user), pk=pk)
    back = reverse("person_detail", args=[person.pk])
    try:
        services.create_invitation(person=person, actor=request.user, request=request)
    except services.InvitationError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, "Invitation queued to the address on file. It goes out within a minute."
        )
    return redirect(back)


@login_required
@require_POST
def invitation_action(request, pk, action):
    pk = int(pk)
    policy.require(request.user, "invite.resend" if action == "resend" else "invite.revoke")
    invitation = get_object_or_404(Invitation.objects.unscoped("invitation action"), pk=pk)
    try:
        if action == "resend":
            services.resend_invitation(invitation, actor=request.user, request=request)
            messages.success(request, "A new link is on its way; the old one no longer works.")
        else:
            services.revoke_invitation(invitation, actor=request.user, request=request)
            messages.success(request, "Invitation revoked.")
    except services.InvitationError as exc:
        messages.error(request, str(exc))
    return redirect("invitations")


@login_required
@require_POST
def reset_start(request, pk):
    policy.require(request.user, "mfa_reset.start")
    person = get_object_or_404(Person.objects.for_user(request.user), pk=pk)
    back = reverse("person_detail", args=[person.pk])
    form = StartResetForm(request.POST)
    if person.user is None or not form.is_valid():
        messages.error(request, "This person has no login, or the form was incomplete.")
        return redirect(back)
    try:
        mfa_reset.start_reset(
            subject=person.user, actor=request.user, request=request, **form.cleaned_data
        )
    except mfa_reset.MfaResetError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Reset started. A different board member must approve it.")
    return redirect(back)


@never_cache
@login_required
@require_POST
def reset_decide(request, pk, decision):
    pk = int(pk)
    policy.require(request.user, "mfa_reset.approve")
    reset = get_object_or_404(MfaReset.objects.for_user(request.user), pk=pk)
    try:
        if decision == "approve":
            code = mfa_reset.approve_reset(reset, actor=request.user, request=request)
            # Shown once, on this response only; never stored or logged.
            return render(request, "accounts/board/reset_code.html", {"code": code})
        mfa_reset.cancel_reset(reset, actor=request.user, request=request)
        messages.success(request, "Reset cancelled. Nothing was changed.")
    except mfa_reset.MfaResetError as exc:
        messages.error(request, str(exc))
    return redirect("approvals")
