import hmac

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from accounts import enrollment, services
from accounts.forms import AcceptInvitationForm
from accounts.models import Invitation
from core import policy
from core.scoping import system_access

SESSION_KEY = "invitation_id"
SESSION_TOKEN_KEY = "invitation_token_hash"  # noqa: S105  # nosec B105
INVALID = "accounts/invite_invalid.html"


@never_cache
@require_GET
def invite_landing(request, token):
    """Swap the emailed token for a session marker, so the link leaves the URL bar."""
    if request.user.is_authenticated:
        return render(request, INVALID, {"signed_in": True}, status=400)
    invitation = services.find_open_invitation(token)
    if invitation is None:
        return render(request, INVALID, status=404)
    request.session.cycle_key()
    request.session[SESSION_KEY] = invitation.pk
    request.session[SESSION_TOKEN_KEY] = services.token_hash(token)
    return redirect("invite_accept")


@never_cache
@require_http_methods(["GET", "POST"])
def invite_accept(request):
    invitation_id = request.session.get(SESSION_KEY)
    token_hash = request.session.get(SESSION_TOKEN_KEY) or ""
    with system_access("invite_accept"):
        invitation = Invitation.objects.filter(pk=invitation_id).pending().first()
    # The link that started this session must still be the current one: a
    # resent or revoked invitation ends any half-finished acceptance.
    if (
        request.user.is_authenticated
        or invitation is None
        or not token_hash
        or not hmac.compare_digest(invitation.token_hash or "", token_hash)
    ):
        request.session.pop(SESSION_KEY, None)
        request.session.pop(SESSION_TOKEN_KEY, None)
        return render(request, INVALID, status=404)

    form = AcceptInvitationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = services.accept_invitation(
                invitation, password=form.cleaned_data["password1"], request=request
            )
        except ValidationError as exc:
            form.add_error("password1", exc)
        except services.InvitationError as exc:
            form.add_error(None, str(exc))
        else:
            request.session.pop(SESSION_KEY, None)
            request.session.pop(SESSION_TOKEN_KEY, None)
            # Tell allauth a password was just used, so MFA enrollment does not
            # immediately ask for it again ("reauthenticate").
            from allauth.account.internal.flows.login import record_authentication

            record_authentication(
                request, user, method="password", email=user.email, reauthenticated=True
            )
            login(request, user, backend="allauth.account.auth_backends.AuthenticationBackend")
            enrollment.grant(request)
            messages.success(
                request,
                "Welcome! One more step: set up a passkey or an authenticator app. "
                "Every account needs one.",
            )
            return redirect("mfa_index")
    return render(request, "accounts/invite_accept.html", {"form": form, "email": invitation.email})


@never_cache
@require_http_methods(["GET", "POST"])
def enroll_with_code(request):
    """After an assisted reset: the code from the approving board member."""
    from accounts import mfa_reset

    if not request.user.is_authenticated:
        return redirect("account_login")
    reset = mfa_reset.open_reset_for(request.user)
    if reset is None:
        return redirect("mfa_index")
    error = None
    if request.method == "POST" and reset.status == "awaiting_code":
        if mfa_reset.redeem_code(
            user=request.user, code=request.POST.get("code", ""), request=request
        ):
            enrollment.grant(request)
            messages.success(
                request, "Code accepted. Now set up a passkey or an authenticator app."
            )
            return redirect("mfa_index")
        reset.refresh_from_db()
        error = "That code is not right."
    return render(request, "accounts/enroll_with_code.html", {"reset": reset, "error": error})


@never_cache
@require_GET
def account_setup_expired(request):
    return render(request, "accounts/setup_expired.html", status=403)


# --- Bulk onboarding (Milestone 4) ---------------------------------------------------


@login_required
def onboarding(request):
    from django.contrib import messages
    from django.shortcuts import redirect

    from documents.attachments import pdf_response

    policy.require(request.user, "invite.send")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "invite_all":
            n = services.invite_everyone(actor=request.user, request=request)
            messages.success(request, f"{n} invitation(s) sent.")
        elif action == "remind":
            n = services.remind_pending(actor=request.user, request=request)
            messages.success(request, f"{n} reminder(s) sent.")
        return redirect("onboarding")
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
    return render(
        request,
        "accounts/onboarding.html",
        {
            "rows": rows,
            "counts": counts,
            "labels": services.ONBOARDING_LABELS,
            "signed_up_percent": round(100 * counts["signed_up"] / len(rows)) if rows else 0,
            "remind_count": len(services.reminder_due(rows=rows)),
        },
    )
