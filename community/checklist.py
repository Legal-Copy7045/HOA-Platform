"""The getting-started checklist for new owners, contacts and tenants (D73).

Every step is worked out from what is really recorded (sign-in methods,
the contact profile, the notices choice, automatic payments, the person's
own actions in the audit log), never from ticks. The only marks the person
sets are ones that are themselves the step: "I've read them" for the
governing documents, "These are right" for contact details already on file,
opening the board's page or Ask the board from here, and hiding an optional
step (or the whole list). Marks are append-only ``HelpMark`` rows of the
signed-in login only; nothing here reads or writes another person's.

Which steps a person gets follows their role: owners on the deed get the
notices choice and (when switched on) automatic payments and text alerts;
account contacts get the statement email; tenants only what applies to
them. The list shows on the dashboard for ``help.welcome_days`` after the
login was created, until every step is done or the person hides it. My
items gets one entry for the steps nothing else there already asks for
(the backup sign-in method and the notices choice are My items' own).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from community.terms import term_text
from core import policy

ALL = "all"  # the HelpMark key that hides the whole checklist
DISMISSIBLE = frozenset({"texts", "autopay"})
# Pages opened from the checklist that count as the step done.
OPENABLE = {"board": "board_and_committees"}


@dataclass
class Step:
    key: str
    title: str
    detail: str
    url: str
    done: bool
    link_label: str = "Open"
    # A button on the Getting started page that is the step itself.
    action: str = ""
    action_label: str = ""
    dismissible: bool = False
    # My items already asks for this step (D58 backup method, D44 notices).
    in_my_items: bool = False
    optional: bool = False


# --- Marks ---------------------------------------------------------------------------------


def _marks(user) -> set[tuple[str, str]]:
    from community.models import HelpMark

    if not getattr(user, "is_authenticated", False):
        return set()
    return set(HelpMark.objects.filter(user=user).values_list("kind", "key"))


def mark(user, kind: str, key: str = "") -> bool:
    """Record one mark for this login (once). True if it was new."""
    from community.models import HelpMark

    if kind not in HelpMark.Kind.values:
        raise ValueError("Unknown mark.")
    if not getattr(user, "is_authenticated", False):
        return False
    if HelpMark.objects.filter(user=user, kind=kind, key=key).exists():
        return False
    try:
        with transaction.atomic():
            HelpMark.objects.create(user=user, kind=kind, key=key)
    except IntegrityError:  # the same mark from two tabs at once
        return False
    return True


def welcome_seen(user) -> bool:
    from community.models import HelpMark

    return HelpMark.objects.filter(user=user, kind=HelpMark.Kind.WELCOME).exists()


# --- The window ------------------------------------------------------------------------------


def window_days() -> int:
    from community.rules import rule

    value = rule("help.welcome_days")
    return 0 if value is None else int(value)


def in_window(user, today: datetime.date | None = None) -> bool:
    days = window_days()
    if days <= 0 or not getattr(user, "date_joined", None):
        return False
    today = today or timezone.localdate()
    return today - timezone.localdate(user.date_joined) <= datetime.timedelta(days=days)


# --- The steps -------------------------------------------------------------------------------


def _audited(user, actions, has_key: str = "") -> bool:
    from audit.models import AuditEvent

    rows = AuditEvent.objects.filter(actor=user, action__in=actions)
    if has_key:
        rows = rows.filter(metadata__has_key=has_key)
    return rows.exists()


def _strong_methods(user) -> int:
    from allauth.mfa.models import Authenticator

    types = list(Authenticator.objects.filter(user=user).values_list("type", flat=True))
    passkeys = sum(1 for t in types if t == Authenticator.Type.WEBAUTHN)
    return passkeys + (1 if Authenticator.Type.TOTP in types else 0)


def _profile(roles):
    from community.models import ContactProfile
    from core.scoping import system_access

    if not roles.person_id:
        return None
    with system_access("own checklist profile"):
        return ContactProfile.objects.filter(person_id=roles.person_id).first()


def steps(user, request=None) -> list[Step]:
    """This person's steps, in order, each with whether it is done."""
    from community.models import HelpMark
    from core import features

    roles = policy.roles_for(user)
    g = roles.groups
    if not g & policy.RESIDENTS:
        return []
    marks = _marks(user)
    profile = _profile(roles)
    K = HelpMark.Kind
    out: list[Step] = []

    strong = _strong_methods(user)
    out.append(
        Step(
            "signin",
            "Add a backup sign-in method",
            "A second passkey (on another phone or computer) or an authenticator app, so "
            "losing one device never locks you out.",
            reverse("mfa_index"),
            strong >= 2,
            "Sign-in and security",
            in_my_items=strong == 1,
        )
    )
    out.append(
        Step(
            "contact",
            "Check your contact details",
            "Your email, phone and the address for post, so the board can reach you.",
            reverse("my_profile") + "#reach",
            (K.CONFIRMED, "contact") in marks
            or _audited(user, ("contact.method_added", "contact.method_changed"))
            or _audited(user, ("profile.updated",), has_key="mailing"),
            "Contact details",
            action="confirm_contact",
            action_label="These are right",
        )
    )
    if roles.owned_lot_ids:
        from community.rules import rule
        from community.services import notice_choice_due

        if rule("notices.electronic_consent_required") is not False:
            due = notice_choice_due(user)
            out.append(
                Step(
                    "notices",
                    "Choose how to get official notices",
                    "By email or by post: meeting, budget and other notices the association "
                    "must send you.",
                    reverse("notice_choice") if due else reverse("my_profile") + "#notices",
                    bool(profile and profile.consent_changed_at),
                    "Choose",
                    in_my_items=due,
                )
            )
    if g & policy.ACCOUNT_VIEWERS:
        on = profile is None or profile.statement_emails
        out.append(
            Step(
                "statements",
                "Decide about the monthly statement email",
                term_text(
                    "When the month's {term} is posted you get an email with the amount due "
                    "and the statement. "
                )
                + ("It is on." if on else "It is off."),
                reverse("my_profile") + "#choices",
                (K.CONFIRMED, "statements") in marks
                or not on
                or _audited(user, ("profile.updated",), has_key="in_directory"),
                "Statement email",
                action="confirm_statements",
                action_label="Keep it on" if on else "Keep it off",
            )
        )
    if policy.can(user, "sms.opt_in") and features.enabled("sms", request):
        out.append(
            Step(
                "texts",
                "Text alerts for urgent news (optional)",
                "A text when something urgent happens, such as a water shut-off. Only if "
                "you sign up.",
                reverse("my_profile") + "#phone",
                bool(profile and profile.sms_opt_in) or (K.DISMISSED, "texts") in marks,
                "Sign up on My profile",
                dismissible=True,
                optional=True,
            )
        )
    autopay = _autopay_step(user, roles, marks, request)
    if autopay:
        out.append(autopay)
    if policy.can(user, "document.view_owner"):
        out.append(
            Step(
                "documents",
                "Read the governing documents",
                "The Declaration, bylaws and rules: what the association may do, and what "
                "every home must do.",
                reverse("documents"),
                (K.DOCS_READ, "governing") in marks,
                "Documents",
                action="docs_read",
                action_label="I've read them",
            )
        )
    if policy.can(user, "board_committees.view"):
        out.append(
            Step(
                "board",
                "See who is on the board",
                "The volunteers who run the association, and their offices.",
                reverse("board_and_committees"),
                (K.OPENED, "board") in marks,
                "Board and committees",
                action="open_board",
                action_label="See the board",
            )
        )
    out.append(
        Step(
            "ask",
            "Know how to ask the board",
            "Questions, billing queries and problems go to the board as a request, with "
            "replies on the portal and by email.",
            reverse("ask_board"),
            (K.OPENED, "ask") in marks or _has_asked(user),
            "Ask the board",
        )
    )
    return out


def _has_asked(user) -> bool:
    from operations.models import OwnerRequest

    return (
        OwnerRequest.objects.unscoped("own checklist: has asked").filter(requested_by=user).exists()
    )


def _autopay_step(user, roles, marks, request) -> Step | None:
    from community.models import HelpMark
    from payments.autopay import OFF, autopay_enabled, state
    from payments.checkout import payable_lot_ids

    if not (roles.owned_lot_ids and autopay_enabled(request)):
        return None
    lots = sorted(payable_lot_ids(user) & roles.owned_lot_ids)
    if not lots:
        return None
    from community.models import Account
    from core.scoping import system_access

    with system_access("own checklist autopay"):
        accounts = list(Account.objects.filter(lot_id__in=lots, closed_on__isnull=True))
    on = any(state(a.pk).status != OFF for a in accounts)
    return Step(
        "autopay",
        term_text("Pay {terms} automatically (optional)"),
        term_text(
            "Each month's {term} paid on its due day from a saved bank account or card, or "
            "twelve months once a year. Turn it off at any time."
        ),
        reverse("autopay", args=[lots[0]]),
        on or (HelpMark.Kind.DISMISSED, "autopay") in marks,
        "Set up",
        dismissible=True,
        optional=True,
    )


# --- What the pages show ---------------------------------------------------------------------


@dataclass
class Checklist:
    steps: list[Step]

    @property
    def done(self) -> int:
        return sum(1 for s in self.steps if s.done)

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def complete(self) -> bool:
        return self.done == self.total

    @property
    def waiting(self) -> list[Step]:
        return [s for s in self.steps if not s.done]

    @property
    def percent(self) -> int:
        return round(100 * self.done / self.total) if self.total else 100


def enabled_for(user, request=None) -> bool:
    from core import features

    return features.enabled("owner_help", request) and policy.can(user, "help.checklist")


def for_user(user, request=None) -> Checklist | None:
    """The checklist for the Getting started page (any time), or None for
    someone it is not for."""
    if not enabled_for(user, request):
        return None
    found = steps(user, request)
    return Checklist(found) if found else None


def for_dashboard(user, request=None) -> Checklist | None:
    """The dashboard card: within the window, not hidden, not complete."""
    if not enabled_for(user, request) or not in_window(user):
        return None
    from community.models import HelpMark

    if (HelpMark.Kind.DISMISSED, ALL) in _marks(user):
        return None
    found = for_user(user, request)
    if found is None or found.complete:
        return None
    return found


def my_items(user, request=None) -> list:
    """One My items entry while the dashboard card shows, counting only the
    steps My items does not already ask for."""
    from core.my_items import HOME, Item

    found = for_dashboard(user, request)
    if found is None:
        return []
    left = [s for s in found.waiting if not s.in_my_items and not s.optional]
    if not left:
        return []
    return [
        Item(
            "My home",
            "Finish getting started",
            f"{found.done} of {found.total} steps done. Next: {left[0].title.lower()}",
            reverse("getting_started"),
            "info",
            icon="check-circle",
            nav="",
            side=HOME,
        )
    ]


def act(user, action: str, request=None) -> str | None:
    """A button on the Getting started page. Returns the page to open next
    (or None to stay). Only this login's own marks change."""
    from community.models import HelpMark

    if not enabled_for(user, request):
        raise PermissionError
    K = HelpMark.Kind
    if action == "docs_read":
        mark(user, K.DOCS_READ, "governing")
    elif action == "confirm_contact":
        mark(user, K.CONFIRMED, "contact")
    elif action == "confirm_statements":
        mark(user, K.CONFIRMED, "statements")
    elif action == "open_board":
        mark(user, K.OPENED, "board")
        return reverse(OPENABLE["board"])
    elif action.startswith("dismiss_") and action[len("dismiss_") :] in DISMISSIBLE:
        mark(user, K.DISMISSED, action[len("dismiss_") :])
    elif action == "hide_all":
        mark(user, K.DISMISSED, ALL)
        return reverse("home")
    else:
        raise ValueError("Unknown step.")
    return None
