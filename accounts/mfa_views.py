"""Passkey pages that keep at least one strong authenticator (review SEC-004).

allauth's own passkey removal does not ask the MFA adapter, so an owner with
only a passkey could remove it and be locked out until two board members
reset their sign-in. These subclasses come before allauth's URLs.
"""

from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from allauth.mfa.webauthn.views import ListWebAuthnView, RemoveWebAuthnView
from django.contrib import messages
from django.shortcuts import redirect

from accounts.mfa_adapter import STRONG

LAST_ONE = (
    "This is your only passkey or authenticator app, so it cannot be removed. "
    "Add a new passkey (or an authenticator app) first, then remove this one."
)


class ListPasskeysView(ListWebAuthnView):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        strong = Authenticator.objects.filter(user=self.request.user, type__in=STRONG).count()
        context["can_remove"] = strong > 1
        return context


class RemovePasskeyView(RemoveWebAuthnView):
    def _refuse_last(self):
        if get_adapter().can_delete_authenticator(self.get_object()):
            return None
        messages.error(self.request, LAST_ONE)
        return redirect("mfa_list_webauthn")

    def get(self, request, *args, **kwargs):
        return self._refuse_last() or super().get(request, *args, **kwargs)

    def form_valid(self, form):
        return self._refuse_last() or super().form_valid(form)


list_passkeys = ListPasskeysView.as_view()
remove_passkey = RemovePasskeyView.as_view()
