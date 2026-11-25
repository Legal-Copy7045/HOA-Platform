from django.contrib import messages
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from accounts import services
from accounts.forms import AcceptInvitationForm
from accounts.models import Invitation
from core.scoping import system_access

SESSION_KEY = "invitation_id"
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
    return redirect("invite_accept")


@never_cache
@require_http_methods(["GET", "POST"])
def invite_accept(request):
    invitation_id = request.session.get(SESSION_KEY)
    with system_access("invite_accept"):
        invitation = Invitation.objects.filter(pk=invitation_id).pending().first()
    if request.user.is_authenticated or invitation is None:
        request.session.pop(SESSION_KEY, None)
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
            login(request, user, backend="allauth.account.auth_backends.AuthenticationBackend")
            messages.success(
                request,
                "Welcome! One more step: set up a passkey or an authenticator app. "
                "Every account needs one.",
            )
            return redirect("mfa_index")
    return render(request, "accounts/invite_accept.html", {"form": form, "email": invitation.email})
