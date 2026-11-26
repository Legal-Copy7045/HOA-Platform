import hmac

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from accounts import enrollment, services
from accounts.auth import has_strong_authenticator
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
            enrollment.grant(request, via="invite")
            messages.success(request, "Your password is saved.")
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
            enrollment.grant(request, via="reset")
            messages.success(request, "Code accepted.")
            return redirect("mfa_index")
        reset.refresh_from_db()
        error = "That code is not right."
    return render(request, "accounts/enroll_with_code.html", {"reset": reset, "error": error})


@never_cache
@require_GET
def account_setup_expired(request):
    return render(request, "accounts/setup_expired.html", status=403)


@never_cache
@require_POST
def account_setup_done(request):
    """Step 3's "I've saved them": ends the guided setup in this session.

    Closing the enrollment window early only narrows access; the middleware
    already sends anyone without a passkey or app back to step 2.
    """
    if not request.user.is_authenticated:
        return redirect("account_login")
    if not has_strong_authenticator(request.user):
        return redirect("mfa_index")
    enrollment.clear(request)
    messages.success(request, "You're all set. Next time, sign in with your passkey or password.")
    # D40: owners on the deed who have not chosen yet are asked once how they
    # want official notices (only now, with the authenticator in place).
    from community.services import notice_choice_due

    if notice_choice_due(request.user):
        return redirect("notice_choice")
    return redirect("home")


@never_cache
@require_GET
def signin_help(request):
    """Forgot password or lost my phone: one of the sign-in steps, so no sign-in needed."""
    return render(request, "accounts/signin_help.html")


# --- Bulk onboarding (Milestone 4) ---------------------------------------------------


@login_required
def onboarding(request):
    """The old Onboarding page: now part of Invitations and sign-ups."""
    from django.urls import reverse

    policy.require(request.user, "invite.send")
    query = request.GET.urlencode()
    return redirect(reverse("invitations") + (f"?{query}" if query else ""))
