"""Own matters: segregation of duty for board members' own homes (D65).

Board members are homeowners first. Their requests, ARC applications,
violations, bookings, accounts and so on are homeowners' matters like anyone
else's: every board member may read them, their own included. What a board
member (or the manager, or any staff role) may not do is *advance* their own
matter with staff powers: assign, start, decide, close, reopen, waive, post,
approve... Another board member does that. They still act on it as the
homeowner, through the owner paths (reply as the requester, withdraw, add
documents, pay).

A matter is one's own when the actor's Person

* is its requester, applicant or subject (the model names the fields:
  ``conflict_user_fields`` for logins, ``conflict_person_fields`` and
  ``scope_person_path`` for people), or
* is today an owner on the deed or an account contact (authorized,
  view-only or tenant) of the lot it concerns (``scope_lot_path``), or
* owned that lot when the matter arose (a former owner's era), or
* the model says so (``conflict_of_interest(user)``, e.g. ARC recusal for
  an affected neighbor).

Services call ``refuse(actor, obj, area=...)`` before any staff step; the
workflow engine does so for every step whose permission is staff-only
(``staff_permission``). Refusals are audited ``policy.own_matter_refused``
with ids only. Internal staff notes on one's own matter are hidden, as for a
recused ARC reviewer (comms.threads).
"""

from __future__ import annotations

import datetime

from django.core.exceptions import PermissionDenied

from core import policy

AUDIT_ACTION = "policy.own_matter_refused"


class OwnMatterError(PermissionDenied):
    """A staff step on the actor's own matter. The message is safe to show."""

    def __str__(self) -> str:
        return self.args[0] if self.args else NOTE


class OwnMatter(str):
    """A workflow refusal because the matter is the actor's own: shown as a
    disabled step with this reason (``Workflow.blocked``)."""


NOTE = "This is your own home's matter: another board member handles it."


def note_for(obj=None, actor=None) -> str:
    """The sentence shown instead of staff buttons on one's own matter (a
    model may word it itself with ``conflict_message(actor)``)."""
    custom = getattr(obj, "conflict_message", None)
    if custom is not None and actor is not None:
        return custom(actor)
    noun = getattr(obj, "conflict_noun", "") or "matter"
    if obj is not None and _lot_path(type(obj)) is None:
        return f"This is your own {noun}: another board member handles it."
    return f"This is your own home's {noun}: another board member handles it."


def staff_permission(permission: str) -> bool:
    """A permission only staff hold (no owner or resident group): taking such
    a step on one's own matter is refused."""
    groups = policy.PERMISSIONS.get(permission, frozenset())
    return bool(groups) and not (groups & policy.SELF_SERVICE)


def _lot_path(model) -> str | None:
    from community.models import Lot

    if model is Lot:
        return ""
    return getattr(model, "scope_lot_path", None)


def _follow(obj, path: str):
    """The id at the end of ``path`` ("lot", "account__lot", ...), or None."""
    parts = path.split("__")
    cur = obj
    for part in parts[:-1]:
        cur = getattr(cur, part, None)
        if cur is None:
            return None
    return getattr(cur, f"{parts[-1]}_id", None)


_DATE_FIELDS = ("created_at", "submitted_at", "opened_at", "observed_on", "posted_on", "starts_at")


def _matter_date(obj) -> datetime.date | None:
    for name in _DATE_FIELDS:
        value = getattr(obj, name, None)
        if isinstance(value, datetime.datetime):
            from django.utils import timezone

            return timezone.localdate(value) if timezone.is_aware(value) else value.date()
        if isinstance(value, datetime.date):
            return value
    return None


def parties(obj) -> tuple[set[int], set[int], set[int], datetime.date | None]:
    """(lot ids, person ids, user ids, date) of the matter ``obj``: a model
    instance, a Lot or a Person."""
    from community.models import Lot, Person
    from core.scoping import system_access

    lots: set[int] = set()
    people: set[int] = set()
    users: set[int] = set()
    if obj is None:
        return lots, people, users, None
    if isinstance(obj, Lot):
        return {obj.pk}, people, users, None
    if isinstance(obj, Person):
        return lots, {obj.pk}, ({obj.user_id} if obj.user_id else set()), None
    model = type(obj)
    with system_access("own matter: parties"):
        lot_path = _lot_path(model)
        if lot_path:
            lot_id = _follow(obj, lot_path)
            if lot_id:
                lots.add(lot_id)
        person_path = getattr(model, "scope_person_path", None)
        if person_path:
            person_id = _follow(obj, person_path)
            if person_id:
                people.add(person_id)
        for name in getattr(model, "conflict_person_fields", ()):
            person_id = _follow(obj, name)
            if person_id:
                people.add(person_id)
        for name in getattr(model, "conflict_user_fields", ()):
            user_id = _follow(obj, name)
            if user_id:
                users.add(user_id)
    return lots, people, users, _matter_date(obj)


def _roles_lots(roles) -> frozenset[int]:
    return roles.owned_lot_ids | roles.account_lot_ids | roles.tenant_lot_ids


def own_matter(actor, obj, *, hooks: bool = True) -> bool:
    """Whether ``obj`` (a matter, a Lot or a Person) is ``actor``'s own.
    ``hooks=False`` leaves out the model's own rule (for that rule itself)."""
    if actor is policy.SYSTEM or actor is None or obj is None:
        return False
    if not getattr(actor, "is_authenticated", False):
        return False
    roles = policy.roles_for(actor)
    lots, people, users, on = parties(obj)
    if actor.pk in users:
        return True
    if roles.person_id is not None and roles.person_id in people:
        return True
    if lots & _roles_lots(roles):
        return True
    if lots and on is not None:
        for lot_id, start, end in roles.former_periods:
            if lot_id in lots and start <= on < end:
                return True
    hook = getattr(obj, "conflict_of_interest", None) if hooks else None
    return bool(hook and hook(actor))


def audit_refusal(actor, obj, *, area: str, action: str = "", request=None) -> None:
    from audit.services import record_event

    record_event(
        AUDIT_ACTION,
        actor=actor,
        request=request,
        target=obj if hasattr(obj, "_meta") else None,
        outcome="denied",
        metadata={
            "area": area[:60],
            "object_id": str(getattr(obj, "pk", "") or ""),
            "action": action[:60],
        },
    )


def refuse(
    actor, obj, *, area: str, action: str = "", request=None, error=None, message: str = ""
) -> None:
    """Refuse a staff step on the actor's own matter: audited, then raises
    ``error`` (an exception class taking the message; default
    ``OwnMatterError``). Call it before any transaction the step opens, so
    the audit record stays when the refusal rolls the step back."""
    if not own_matter(actor, obj):
        return
    audit_refusal(actor, obj, area=area, action=action, request=request)
    raise (error or OwnMatterError)(message or note_for(obj, actor))


def refuse_any(
    actor, objs, *, area: str, action: str = "", request=None, error=None, message: str = ""
) -> None:
    """``refuse`` for several matters at once (a batch of lines or payments)."""
    for obj in objs:
        refuse(actor, obj, area=area, action=action, request=request, error=error, message=message)


def acting_as_staff(actor, obj, permission: str) -> bool:
    """May ``actor`` take a step needing ``permission`` on ``obj`` now (holds
    it, and it is not their own matter)? For showing staff controls."""
    if actor is policy.SYSTEM:
        return True
    return policy.can(actor, permission) and not own_matter(actor, obj)


def page_context(user, obj, workflow=None) -> dict:
    """What a detail page needs for one's own matter: ``own_matter`` (base.html
    leaves out the Board badge and strip), ``own_note`` (shown instead of the
    staff controls) and, with ``workflow``, the staff steps shown disabled
    (``blocked_steps``, partials/workflow.html)."""
    own = own_matter(user, obj)
    ctx = {"own_matter": own, "own_note": note_for(obj, user) if own else ""}
    if own and workflow is not None:
        ctx["blocked_steps"] = [
            (t, reason)
            for t, reason in workflow.blocked(obj, user)
            if isinstance(reason, OwnMatter)
        ]
    return ctx


# --- Two-person coverage for own matters (My items) -----------------------------------


def _open_matters():
    """(label, model, open filter, staff permission) for each area whose open
    matters need another board member when they are a board member's own."""
    from arc.models import ArcRequest
    from community.models import AccountChangeRequest, Reservation
    from compliance.models import Violation
    from governance.models import RecordsRequest
    from ledger.models import CollectionCase, ResaleRequest
    from operations.models import OwnerRequest

    return [
        ("request", OwnerRequest, {"state__in": ["submitted", "in_progress", "waiting"]},
         "request.manage", "request_detail"),
        ("architectural request", ArcRequest,
         {"state__in": ["submitted", "in_review", "appeal"]}, "arc.intake", "arc_detail"),
        ("violation", Violation, {"state__in": ["open", "noticed", "responded", "hearing"]},
         "violation.decide", "violation_detail"),
        ("records request", RecordsRequest, {"state__in": ["submitted", "in_progress"]},
         "records_request.manage", "records_request_detail"),
        ("collection case", CollectionCase, {"state__in": ["open", "reminder_sent",
         "second_reminder", "final_notice", "payment_plan", "referred"]},
         "collections.manage", "collection_detail"),
        ("resale certificate", ResaleRequest, {"state__in": ["submitted", "in_progress"]},
         "resale.manage", "resale_detail"),
        ("booking", Reservation, {"state": "pending"}, "reservation.manage",
         "reservation_detail"),
        ("account change", AccountChangeRequest, {"state": "pending"},
         "account.approve_change", None),
    ]  # fmt: skip


def _conflicted_people(obj) -> set[int]:
    """Person ids for whom ``obj`` is their own matter (owners and contacts
    of its lot today, its requester or subject)."""
    from community.models import AccountContact, Ownership, Person
    from core.scoping import system_access

    lots, people, users, _on = parties(obj)
    with system_access("own matter: conflicted people"):
        out = set(people)
        if users:
            out |= set(Person.objects.filter(user_id__in=users).values_list("pk", flat=True))
        if lots:
            out |= set(
                Ownership.objects.active()
                .filter(lot_id__in=lots)
                .values_list("person_id", flat=True)
            )
            out |= set(
                AccountContact.objects.active()
                .filter(account__lot_id__in=lots, account__closed_on__isnull=True)
                .values_list("person_id", flat=True)
            )
    return out


def coverage_gaps(user) -> list[tuple[str, object, str | None]]:
    """The user's own open matters that no *other* person with two-step
    sign-in can act on: (label, matter, detail URL name)."""
    from core.policy_mfa import people_with_mfa
    from core.scoping import system_access

    roles = policy.roles_for(user)
    if roles.person_id is None or not (roles.groups & policy.PRIVILEGED_GROUPS):
        return []
    mine_lots = _roles_lots(roles)
    with_mfa = people_with_mfa()
    by_group = policy._people_by_group()
    out = []
    from django.db.models import Q

    with system_access("own matter coverage"):
        for label, model, open_filter, permission, url_name in _open_matters():
            if not policy.can(user, permission):
                continue
            condition = Q(pk__in=[])
            lot_path = _lot_path(model)
            if lot_path and mine_lots:
                condition |= Q(**{f"{lot_path}__in": mine_lots})
            person_path = getattr(model, "scope_person_path", None)
            if person_path:
                condition |= Q(**{person_path: roles.person_id})
            for name in getattr(model, "conflict_person_fields", ()):
                condition |= Q(**{name: roles.person_id})
            for name in getattr(model, "conflict_user_fields", ()):
                condition |= Q(**{name: user.pk})
            able = policy.people_able_to(permission, by_group)
            if with_mfa is not None:
                able &= with_mfa
            for obj in model._base_manager.filter(condition, **open_filter)[:20]:
                others = able - {roles.person_id} - _conflicted_people(obj)
                if not others:
                    out.append((label, obj, url_name))
    return out
