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
    """Invitations and sign-ups: the per-lot sign-up status and the bulk
    buttons (board, ``invite.send``) above every invitation with re-send and
    revoke (board or sysadmin). Each button keeps its own permission check;
    sysadmins see invitation status only, not the register (decision D9)."""
    from documents.attachments import pdf_response

    policy.require(request.user, "invite.resend")
    can_send = policy.can(request.user, "invite.send")
    if request.method == "POST":
        policy.require(request.user, "invite.send")
        action = request.POST.get("action")
        if action == "invite_all":
            n = services.invite_everyone(actor=request.user, request=request)
            messages.success(request, f"{n} invitation(s) sent.")
        elif action == "remind":
            n = services.remind_pending(actor=request.user, request=request)
            messages.success(request, f"{n} reminder(s) sent.")
        return redirect("invitations")
    context = {
        "invitations": Invitation.objects.for_user(request.user).select_related("person")[:200],
        "can_send": can_send,
        "can_resend": policy.can(request.user, "invite.resend"),
        "can_revoke": policy.can(request.user, "invite.revoke"),
    }
    if can_send:
        rows = services.onboarding_rows()
        if request.GET.get("letters") == "1":
            return pdf_response(
                services.email_request_letters_pdf([r for r in rows if r["status"] == "no_email"]),
                "portal-letters.pdf",
            )
        counts = dict.fromkeys(services.ONBOARDING_LABELS, 0)
        for r in rows:
            counts[r["status"]] += 1
            r["label"] = services.ONBOARDING_LABELS[r["status"]]
        context.update(
            {
                "rows": rows,
                "counts": counts,
                "signed_up_percent": round(100 * counts["signed_up"] / len(rows)) if rows else 0,
                "remind_count": len(services.reminder_due(rows=rows)),
            }
        )
        # D44: how homes get official notices (counts only) and the wording.
        from community import notices
        from community.services import notice_choice_counts

        context.update(
            {
                "notice_counts": notice_choice_counts(),
                "notice_version": notices.CONSENT_VERSION,
                "notice_wording": notices.consent_text(),
            }
        )
    elif request.GET.get("letters") == "1":
        policy.require(request.user, "invite.send")
    return render(request, "accounts/board/invitations.html", context)


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
