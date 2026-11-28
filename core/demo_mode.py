"""Demo mode (decision D97): on the POC, a board member or sysadmin may view
the portal as one of the demo dataset's test personas, and switch back.

Nothing about the rules changes: while switched, every page and service runs
as the persona, with the persona's roles, scope, recusals and own matters.
What is different is only *who is signed in underneath*:

* the session stays the real user's (no sign-out, no new sign-in, no MFA
  prompt for the persona). The MFA middleware has already checked the real
  user's own passkey or authenticator before this middleware switches;
* the persona is ``request.user`` for the rest of the request, and
  ``request.demo_real_user`` is the real user;
* every audit event recorded while switched carries ``demo_switch_by`` (the
  real user's id) next to the persona as actor (``audit.services``).

Where it exists (all three, checked on every request):

1. :func:`configured`: ``SEED_DEMO_ALLOWED`` and ``DEPLOYMENT_STAGE == "poc"``
   (production settings force the flag off anywhere else; a system check,
   ``hoa.E003``, refuses a gate that ever opens outside the POC);
2. the database holds a seeded demo dataset (``core.DemoSeed``) with personas;
3. the real user is not a demo persona, has a strong authenticator, and may
   ``demo.switch`` (board and sysadmin).

Targets are only logins the seed marked (``core.DemoPersona``, written by
:func:`mark_persona`) that still have no usable password and no
authenticator: a real login is never a target, whatever its name or email.

Refused while switched (:data:`REFUSED_URL_NAMES`, :data:`REFUSED_PREFIXES`):
Keys and connections, Restart, server tasks (backups, Install update),
Demo data, every sign-in, password and MFA page, Preview as owner, online
payment and bank-feed set-up (they reach real services), texts, the admin.
Services that grant or take away powers over a login (roles, seats,
offices, sysadmin, linking a login, sign-in resets) refuse a real login's
person or login while switched (:func:`refuse_real_target`), so one real
person can never use two personas to act on a real account.

The re-authentication step before a vote (D24) is the real user's: allauth's
page runs as the real user and the persona's request then counts it
(``accounts.adapter.AccountAdapter.is_reauthentication_required``).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import Resolver404, resolve
from django.views.decorators.http import require_POST

SESSION_KEY = "demo_switch"
STAGE = "poc"
SWITCH_ACTION = "demo.switch"


class DemoModeError(Exception):
    pass


@dataclass(frozen=True)
class Switch:
    real_user_id: int
    persona_user_id: int
    key: str
    label: str


_current: contextvars.ContextVar[Switch | None] = contextvars.ContextVar(
    "demo_switch", default=None
)


def current() -> Switch | None:
    """The switch in effect for this request, if any (None in the worker)."""
    return _current.get()


# --- Where it exists ---------------------------------------------------------------------


def configured() -> bool:
    """The settings half of the gate: the POC with demo data allowed."""
    return bool(settings.SEED_DEMO_ALLOWED) and settings.DEPLOYMENT_STAGE == STAGE


def _seeded() -> bool:
    from core.models import DemoPersona, DemoSeed

    return DemoSeed.objects.exists() and DemoPersona.objects.exists()


def available(request=None) -> bool:
    """Demo mode is on: the POC, demo data allowed, and a seeded demo dataset
    with personas in the database (remembered for the request)."""
    if not configured():
        return False
    if request is not None:
        cached = getattr(request, "_demo_available", None)
        if cached is not None:
            return cached
    result = _seeded()
    if request is not None:
        request._demo_available = result
    return result


# --- Personas ----------------------------------------------------------------------------


def _has_any_way_in(user) -> bool:
    from allauth.mfa.models import Authenticator

    return user.has_usable_password() or Authenticator.objects.filter(user=user).exists()


def mark_persona(user, *, key: str, label: str, position: int = 0):
    """Called by the demo seed only: mark ``user`` as test persona ``key``.
    Refuses outside a demo deployment, and for any login that has a password,
    an authenticator or staff powers (a real person's login)."""
    from community.models import Person
    from core.models import DemoPersona
    from core.scoping import system_access

    if not settings.SEED_DEMO_ALLOWED:
        raise DemoModeError("Refusing: only on a demo deployment (SEED_DEMO_ALLOWED=true).")
    if user.is_staff or user.is_superuser or _has_any_way_in(user):
        raise DemoModeError("Refusing: only a seeded login with no way in can be a persona.")
    with system_access("demo persona"):
        person = Person.objects.filter(user=user).only("display_name").first()
    if person is None or not person.display_name.endswith("(demo)"):
        raise DemoModeError("Refusing: a persona is a demo person's login.")
    persona, _ = DemoPersona.objects.update_or_create(
        key=key, defaults={"user": user, "label": label[:160], "position": position}
    )
    return persona


def is_persona(user) -> bool:
    from core.models import DemoPersona

    pk = getattr(user, "pk", None)
    return pk is not None and DemoPersona.objects.filter(user_id=pk).exists()


def targets():
    """Personas one may switch to: marked by the seed, active, and still
    without a password or an authenticator (a login someone has set up is
    treated as a person's own and is never a target)."""
    from allauth.mfa.models import Authenticator

    from core.models import DemoPersona

    with_authenticator = Authenticator.objects.values("user_id")
    out = []
    for persona in (
        DemoPersona.objects.select_related("user")
        .filter(user__is_active=True, user__is_staff=False, user__is_superuser=False)
        .exclude(user_id__in=with_authenticator)
    ):
        if persona.user.has_usable_password():
            continue
        out.append(persona)
    return out


def _target(user_id) -> object | None:
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    return next((p for p in targets() if p.user_id == user_id), None)


def refusal(user) -> str:
    """Why ``user`` (the real, signed-in user) may not switch; "" when they may."""
    from accounts.auth import has_strong_authenticator
    from core import policy

    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return "signed_out"
    if is_persona(user):
        return "persona"
    if not has_strong_authenticator(user):
        return "no_mfa"
    if not policy.can(user, SWITCH_ACTION):
        return "not_allowed"
    return ""


def names(user_ids) -> dict[int, str]:
    """Display names (fictional) of the personas' people, by login id."""
    from community.models import Person
    from core.scoping import system_access

    with system_access("demo persona names"):
        return dict(
            Person.objects.filter(user_id__in=list(user_ids)).values_list(
                "user_id", "display_name"
            )
        )


# --- Refused while switched --------------------------------------------------------------

# Pages that run as the real user even while switched: switching itself,
# signing out, and the real user's own re-authentication (D24).
REAL_USER_URL_NAMES = frozenset(
    {
        "demo_switch",
        "demo_switch_back",
        "account_logout",
        "account_reauthenticate",
        "mfa_reauthenticate",
        "mfa_reauthenticate_webauthn",
    }
)
REFUSED_URL_NAMES = frozenset(
    {
        # Keys and connections, Restart, server tasks (Install update,
        # backups, restore drills), Demo data (D93, D91, D36, D53).
        "system_restart",
        "server_tasks",
        "demo_data",
        # Another identity on top of this one.
        "owner_preview_start",
        # Sign-in steps of a login.
        "enroll_with_code",
        "invite_accept",
        "invite_landing",
        "recovery_request",
        "recovery_link",
        "recovery_continue",
        "account_setup_done",
        # Real services: Stripe, Teller, texts.
        "pay_checkout",
        "autopay_start",
        "autopay_cancel",
        "autopay_schedule",
        "bank_connect",
        "bank_disconnect",
        "bank_sync",
        "sms_start",
        "sms_verify",
    }
)
REFUSED_PREFIXES = ("keys_", "mfa_", "account_")
NO_PERSONAS_MESSAGE = (
    "Demo mode is not available: the demo data loaded on this portal has no test personas "
    "to view as. Load a dataset with personas (Settings → Demo data) to use it."
)
REFUSED_MESSAGE = (
    "Not available while you are viewing as a demo persona. Choose “Back to me” first; "
    "this page needs your own login and second factor."
)


def _url_name(request) -> str | None:
    try:
        return resolve(request.path_info).url_name or ""
    except Resolver404:
        return None


def is_refused(request, name: str | None) -> bool:
    admin = getattr(settings, "ADMIN_URL", "")
    if admin and request.path_info.lstrip("/").startswith(admin.lstrip("/")):
        return True
    if not name:
        return False
    if name in REAL_USER_URL_NAMES:
        return False
    return name in REFUSED_URL_NAMES or name.startswith(REFUSED_PREFIXES)


REAL_TARGET_MESSAGE = "Not while viewing as a demo persona: this would change a real login."


def refuse_real_target(*, person_id=None, user_id=None, error=PermissionDenied) -> None:
    """While switched, refuse a step on a real (non-persona) login or its
    person: granting or ending roles, seats, offices or sysadmin, linking a
    login, starting or approving a sign-in reset. A no-op otherwise."""
    switch = current()
    if switch is None:
        return
    if user_id is None and person_id is not None:
        from community.models import Person
        from core.scoping import system_access

        with system_access("demo mode: real login?"):
            user_id = Person.objects.filter(pk=person_id).values_list("user_id", flat=True).first()
    if user_id is None:
        return  # a register entry without a login: demo data
    if user_id == switch.real_user_id or not _is_persona_id(user_id):
        raise error(REAL_TARGET_MESSAGE)


def _is_persona_id(user_id: int) -> bool:
    from core.models import DemoPersona

    return DemoPersona.objects.filter(user_id=user_id).exists()


# --- Middleware --------------------------------------------------------------------------


class DemoSwitchMiddleware:
    """After the MFA middleware (which checks the real user): while the session
    holds a valid switch, the persona becomes ``request.user``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        session = getattr(request, "session", None)
        data = session.get(SESSION_KEY) if session is not None else None
        if not data:
            return self.get_response(request)
        real = request.user
        persona = self._resolve(request, real, data)
        if persona is None:
            session.pop(SESSION_KEY, None)
            return self.get_response(request)
        switch = Switch(real.pk, persona.user_id, persona.key, persona.label)
        request.demo_real_user = real
        request.demo_switch = switch
        name = _url_name(request)
        if name in REAL_USER_URL_NAMES:
            return self.get_response(request)
        user = persona.user
        request.user = user

        async def auser():
            return user

        request.auser = auser
        token = _current.set(switch)
        try:
            if is_refused(request, name):
                from audit.services import record_event

                record_event(
                    "demo.page_refused",
                    request=request,
                    actor=user,
                    metadata={"url_name": (name or "")[:80], "persona": persona.key},
                )
                return render(
                    request, "core/demo_refused.html", {"message": REFUSED_MESSAGE}, status=403
                )
            return self.get_response(request)
        finally:
            _current.reset(token)

    def process_exception(self, request, exception):
        """A re-authentication prompt while switched is the real user's: hand
        allauth the real user so it offers their password or second factor."""
        from allauth.core.exceptions import ReauthenticationRequired

        real = getattr(request, "demo_real_user", None)
        if real is not None and isinstance(exception, ReauthenticationRequired):
            request.user = real
        return None

    @staticmethod
    def _resolve(request, real, data):
        if not isinstance(data, dict) or not getattr(real, "is_authenticated", False):
            return None
        if data.get("real") != real.pk or not available(request) or refusal(real):
            return None
        return _target(data.get("persona"))


def real_user_recently_reauthenticated(request) -> bool | None:
    """While switched: whether the *real* user re-authenticated recently in
    this session (allauth's own check, run as them). None when not switched."""
    real = getattr(request, "demo_real_user", None) if request is not None else None
    if real is None or getattr(request, "demo_switch", None) is None:
        return None
    from allauth.account.internal.flows.reauthentication import did_recently_reauthenticate

    persona = request.user
    request.user = real
    try:
        return did_recently_reauthenticate(request)
    finally:
        request.user = persona


# --- Banner ------------------------------------------------------------------------------


def banner(request) -> dict | None:
    """What the demo banner shows on this page, or None outside demo mode."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or not available(request):
        return None
    switch = getattr(request, "demo_switch", None)
    real = getattr(request, "demo_real_user", None) or user
    may = not refusal(real)
    personas = targets() if may else []
    labels = names(p.user_id for p in personas) if may else {}
    if switch is not None:
        persona_names = labels or names([switch.persona_user_id])
        viewing = persona_names.get(switch.persona_user_id, switch.key)
        role = switch.label
    else:
        viewing, role = "yourself", "your own login"
    return {
        "switched": switch is not None,
        "viewing": viewing,
        "role": role,
        "may_switch": may,
        "current": switch.persona_user_id if switch else None,
        "personas": [
            {"user_id": p.user_id, "name": labels.get(p.user_id, p.key), "label": p.label}
            for p in personas
        ],
    }


# --- Views -------------------------------------------------------------------------------


def _audit(action: str, request, *, actor, target=None, **metadata) -> None:
    from audit.services import record_event

    record_event(action, request=request, actor=actor, target=target, metadata=metadata)


@login_required
@require_POST
def switch(request):
    """Switch this session to a demo persona (POST, CSRF; D97)."""
    if not configured():
        raise Http404
    if not available(request):
        # The POC, but the demo dataset loaded (e.g. a blank one) has no
        # personas: say so plainly; nothing is switched.
        return render(
            request, "core/demo_refused.html", {"message": NO_PERSONAS_MESSAGE}, status=404
        )
    real = getattr(request, "demo_real_user", None) or request.user
    why = refusal(real)
    if why:
        _audit("demo.switch_refused", request, actor=real, reason=why)
        raise PermissionDenied("You can't switch personas.")
    persona = _target(request.POST.get("persona"))
    if persona is None:
        _audit("demo.switch_refused", request, actor=real, reason="not_a_persona")
        raise Http404
    from core import preview

    request.session.pop(preview.SESSION_KEY, None)
    previous = (request.session.get(SESSION_KEY) or {}).get("persona")
    request.session[SESSION_KEY] = {"persona": persona.user_id, "real": real.pk}
    _audit(
        "demo.switched",
        request,
        actor=real,
        target=persona.user,
        persona=persona.key,
        persona_user_id=persona.user_id,
        from_persona_user_id=previous if isinstance(previous, int) else None,
    )
    messages.info(request, f"Demo: you are now viewing as {persona.label}.")
    return redirect("home")


@login_required
@require_POST
def switch_back(request):
    """Back to the real signed-in user."""
    if not available(request):
        request.session.pop(SESSION_KEY, None)
        raise Http404
    data = request.session.pop(SESSION_KEY, None)
    real = getattr(request, "demo_real_user", None) or request.user
    if isinstance(data, dict):
        _audit(
            "demo.switched_back",
            request,
            actor=real,
            persona_user_id=data.get("persona") if isinstance(data.get("persona"), int) else None,
        )
        messages.info(request, "Demo: you are back to your own login.")
    return redirect("home")
