"""Message threads on requests, ARC requests, violations and work orders.

The subject object is always fetched through its own scoped manager first
(``subject_for``), so a thread is only reachable by someone who may see the
subject. On top of that:

* owner-side people (applicant, lot owners, requester) read and post public
  messages only, and only if they may act (not view-only or former owners);
* staff with the subject's view permission also read internal messages, and
  post with its manage permission;
* an ARC reviewer who is recused is treated as owner side (no deliberations);
* emails carry only a link, never the message (CLAUDE.md: no personal data in
  mail beyond what is needed).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.urls import reverse

from audit.services import record_event
from comms.models import ThreadMessage
from core import policy
from core.scoping import system_access

MAX_BODY = 5000


class ThreadError(ValueError):
    pass


@dataclass(frozen=True)
class Access:
    read: bool = False
    read_internal: bool = False
    post: bool = False
    post_internal: bool = False


def _model(kind: str):
    if kind == "request":
        from operations.models import OwnerRequest

        return OwnerRequest
    if kind == "arc":
        from arc.models import ArcRequest

        return ArcRequest
    if kind == "violation":
        from compliance.models import Violation

        return Violation
    if kind == "work_order":
        from operations.models import WorkOrder

        return WorkOrder
    if kind == "committee_topic":
        from governance.models import CommitteeTopic

        return CommitteeTopic
    raise Http404


KINDS = ("request", "arc", "violation", "work_order", "committee_topic")
LABELS = {
    "request": "request",
    "arc": "architectural request",
    "violation": "notice",
    "work_order": "work order",
    "committee_topic": "committee discussion",
}
DETAIL_URLS = {
    "request": "request_detail",
    "arc": "arc_detail",
    "violation": "violation_detail",
    "work_order": "work_order_detail",
    "committee_topic": "committee_topic_detail",
}
STAFF = {
    # kind: (view internal, post as staff)
    "request": ("request.manage", "request.manage"),
    "arc": ("arc.view_all", "arc.intake"),
    "violation": ("violation.view_all", "violation.manage"),
    "work_order": ("workorder.manage", "workorder.manage"),
}


def subject_for(kind: str, pk: int, user):
    """The subject, fetched through its own scoping (404 if out of scope)."""
    model = _model(kind)
    if kind == "committee_topic":
        from governance.services import can_view_committee

        topic = model.objects.select_related("committee").filter(pk=pk).first()
        if topic is None or not can_view_committee(user, topic.committee):
            raise Http404
        return topic
    if kind == "work_order":
        if not policy.can(user, "workorder.manage"):
            raise Http404
        qs = model.objects.all()
    else:
        qs = model.objects.for_user(user)
    obj = qs.filter(pk=pk).first()
    if obj is None:
        raise Http404
    return obj


def _owner_side_may_post(kind: str, obj, user) -> bool:
    """Owner side posts only while they may still act on the lot: never
    view-only contacts, and never former owners (read-only history)."""
    roles = policy.roles_for(user)
    if kind == "request":
        if not policy.can(user, "request.submit"):
            return False
        # Owners and authorized contacts of that lot; never a view-only
        # contact of it, even one who owns another home (streamlining).
        if obj.lot_id in roles.owned_lot_ids | roles.authorized_lot_ids:
            return True
        return obj.requested_by_id == user.pk and obj.lot_id in roles.tenant_lot_ids
    if kind == "arc":
        return (
            obj.applicant_id == roles.person_id
            and obj.lot_id in roles.owned_lot_ids
            and policy.can(user, "arc.submit")
        )
    if kind == "violation":
        return obj.lot_id in roles.owned_lot_ids and obj.noticed_at is not None
    return False


def _recused(kind: str, obj, user) -> bool:
    """A recused ARC reviewer, and since D65 anyone on their own matter (a
    request, ARC request or notice about their own home, or one they
    raised), reads and posts on the owner side only: no internal notes."""
    if kind not in ("request", "arc", "violation"):
        return False
    from core import conflicts

    return conflicts.own_matter(user, obj)


def access(kind: str, obj, user) -> Access:
    """What ``user`` may do on the thread of ``obj`` (already scoped)."""
    if kind == "committee_topic":
        from governance.services import can_view_committee, can_work_in_committee

        committee = obj.committee
        read = can_view_committee(user, committee)
        return Access(
            read=read, post=read and not obj.closed and can_work_in_committee(user, committee)
        )
    view_perm, post_perm = STAFF[kind]
    if policy.can(user, view_perm) and not _recused(kind, obj, user):
        staff_post = policy.can(user, post_perm) or (
            kind == "arc" and policy.can(user, "arc.decide")
        )
        return Access(True, True, staff_post, staff_post)
    return Access(read=True, post=_owner_side_may_post(kind, obj, user))


def messages_for(kind: str, obj, user):
    acc = access(kind, obj, user)
    with system_access("thread messages"):
        qs = ThreadMessage.objects.filter(subject_kind=kind, subject_id=obj.pk).select_related(
            "author"
        )
        if not acc.read_internal:
            qs = qs.filter(internal=False)
        return list(qs)


def post(kind: str, obj, *, body: str, internal: bool = False, actor, request=None):
    """Post a message. ``obj`` must come from ``subject_for`` (scoped)."""
    acc = access(kind, obj, actor)
    if internal and not acc.post_internal:
        raise ThreadError("Only the board, manager or reviewers can post internal notes.")
    if not acc.post:
        raise ThreadError("You can read this thread but not post to it.")
    body = (body or "").strip()
    if not body:
        raise ThreadError("Write a message first.")
    if len(body) > MAX_BODY:
        raise ThreadError(f"Please keep messages under {MAX_BODY:,} characters.")
    if kind == "committee_topic" and internal:
        raise ThreadError("Committee discussions have no internal notes.")
    staff = acc.post_internal
    with transaction.atomic(), system_access("post thread message"):
        message = ThreadMessage.objects.create(
            subject_kind=kind, subject_id=obj.pk, author=actor, body=body, internal=internal
        )
        record_event(
            "thread.message_posted",
            actor=actor,
            request=request,
            target=obj,
            metadata={"kind": kind, "object_id": obj.pk, "internal": internal},
        )
    if kind == "request" and staff and not internal:
        from operations.services import mark_request_responded

        mark_request_responded(obj)
    if kind == "committee_topic":
        _notify_committee(obj, exclude=actor)
    elif internal:
        pass  # internal notes notify nobody outside the staff page
    elif staff:
        notify_owner_side(kind, obj, exclude=actor)
    else:
        notify_staff(kind, obj, exclude=actor)
    return message


# --- Notifications (link only) ----------------------------------------------------------


def _link(kind: str, obj) -> str:
    return settings.PORTAL_BASE_URL + reverse(DETAIL_URLS[kind], args=[obj.pk])


def _send(kind: str, obj, emails: set[str], board: bool = False) -> None:
    """``board``: to the staff who work on it ("[Board] Update on request
    #12", D64); otherwise to the owner side ("Update on your request #12")."""
    if not emails:
        return
    label = LABELS[kind]
    whose = "" if board else "your "
    subject = f"[{settings.SITE_NAME}] Update on {whose}{label} #{obj.pk}"
    body = (
        f"There is an update on {label} #{obj.pk}.\n\n"
        f"Sign in to read it:\n{_link(kind, obj)}\n\n-- \n{settings.SITE_NAME}"
    )

    from comms.notify import send_update

    send_update(emails, subject=subject, body=body, link=_link(kind, obj), board=board)


def _owner_side_emails(kind: str, obj) -> set[str]:
    from community.models import Ownership, Person

    with system_access("thread owner emails"):
        if kind == "request":
            return {obj.requested_by.email}
        if kind == "arc":
            person = Person.objects.filter(pk=obj.applicant_id).select_related("user").first()
            return {person.user.email} if person and person.user_id else set()
        if kind == "violation":
            return set(
                Ownership.objects.active()
                .filter(lot_id=obj.lot_id, person__user__isnull=False)
                .values_list("person__user__email", flat=True)
            )
    return set()


def _staff_emails(kind: str, obj) -> set[str]:
    from accounts.models import User

    if kind == "request" and obj.assignee_id:
        with system_access("assignee email"):
            user = User.objects.filter(person__pk=obj.assignee_id, is_active=True).first()
        if user:
            return {user.email}
    _view, post_perm = STAFF[kind]
    return {
        u.email
        for u in User.objects.filter(is_active=True)
        if policy.can(u, post_perm) and not _recused(kind, obj, u)
    }


def notify_owner_side(kind: str, obj, exclude=None) -> None:
    emails = _owner_side_emails(kind, obj)
    if exclude is not None:
        emails.discard(exclude.email)
    _send(kind, obj, emails)


def notify_staff(kind: str, obj, exclude=None) -> None:
    emails = _staff_emails(kind, obj)
    if exclude is not None:
        emails.discard(exclude.email)
    _send(kind, obj, emails, board=True)


def _notify_committee(topic, exclude=None) -> None:
    from governance.services import committee_members

    with system_access("committee emails"):
        emails = {
            m.person.user.email
            for m in committee_members(topic.committee)
            if m.person.user_id and m.person.user.is_active
        }
    if exclude is not None:
        emails.discard(exclude.email)
    _send("committee_topic", topic, emails)
