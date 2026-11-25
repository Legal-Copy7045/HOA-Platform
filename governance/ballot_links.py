"""Personal, single-use, signed-in ballot links (decision D24).

When a formal vote or an election opens, each current owner with a portal
login is emailed a link of their own. The link:

* needs sign-in (a session already open is fine);
* opens only for the person it was issued to: forwarded to anyone else it
  shows nothing, even to another signed-in owner;
* works once, and not after voting closes.

Casting itself also needs a recent re-authentication (password or passkey),
see governance views. Owners can always vote from the portal menu too: the
link is a convenience bound to one person, never a credential on its own.
"""

from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from audit.services import record_event
from comms.notify import send_now
from community.models import Ownership
from core.scoping import system_access
from governance.models import BallotLink, Election, ElectionState, Vote, VoteStatus


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue(target: Vote | Election) -> int:
    """Email every current owner with an active login a personal link."""
    is_vote = isinstance(target, Vote)
    title = target.title
    sent = 0
    with system_access("ballot links"):
        owners = (
            Ownership.objects.active()
            .select_related("person__user")
            .order_by("person_id")
            .distinct()
        )
        people = {o.person_id: o.person for o in owners}
        for person in people.values():
            user = person.user
            if user is None or not user.is_active or not user.email:
                continue
            token = secrets.token_urlsafe(32)
            with transaction.atomic():
                BallotLink.objects.create(
                    person=person,
                    vote=target if is_vote else None,
                    election=None if is_vote else target,
                    token_hash=_hash(token),
                )
            link = settings.PORTAL_BASE_URL + reverse("ballot_link", args=[token])
            # Never into the weekly digest: the link is personal and single-use.
            send_now(
                {user.email},
                subject=f"[{settings.SITE_NAME}] Voting is open: {title}"[:200],
                body=(
                    f"Voting is open: {title}.\n\n"
                    "This link is for you alone. Sign in to the portal to cast your ballot; "
                    "you will be asked to confirm it is you before it is recorded. "
                    "The link works once and cannot be used by anyone else, so please do "
                    f"not forward it.\n\n{link}\n\n"
                    "You can also vote from the portal menu at any time while voting is open."
                    f"\n\n-- \n{settings.SITE_NAME}"
                ),
            )
            sent += 1
    record_event(
        "vote.links_sent",
        actor=None,
        target=target,
        metadata={"count": sent, "kind": "vote" if is_vote else "election"},
    )
    return sent


def resolve(token: str, user):
    """The target page for a link this user may open, marking it used; None
    if the link is unknown, used, closed, or someone else's."""
    from core import policy

    if not token or len(token) > 100:
        return None
    person_id = policy.roles_for(user).person_id
    with transaction.atomic(), system_access("ballot link"):
        link = (
            BallotLink.objects.select_for_update(of=("self",))
            .select_related("vote", "election")
            .filter(token_hash=_hash(token))
            .first()
        )
        if link is None or link.used_at is not None or link.person_id != person_id:
            if link is not None and link.person_id != person_id:
                record_event(
                    "vote.link_refused",
                    actor=user,
                    metadata={"reason": "other_person"},
                )
            return None
        if link.vote_id and link.vote.status != VoteStatus.OPEN:
            return None
        if link.election_id and link.election.state != ElectionState.VOTING:
            return None
        BallotLink.objects.filter(pk=link.pk).update(used_at=timezone.now())
    if link.vote_id:
        return reverse("vote_detail", args=[link.vote_id])
    return reverse("election_detail", args=[link.election_id])
