"""The ARC decision email and whether it reached the applicant (0.43.0).

When a decision (or an appeal decision, or a deemed approval) is recorded,
the worker emails the applicant that it is ready, with a link to the request
(sign-in needed: the letter itself stays behind the sign-in). It is sent as
an official notice (a bounce asks the board for a paper copy, D74) and
tagged ``arc_decision:<id>``, so the board sees on the ARC request whether it
was delivered, bounced or is still pending (SMTP2Go's delivery reports,
comms/delivery.py). A retry never emails an address that already got it.
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse

from core.scoping import system_access

ABOUT = "arc_decision"


def about(decision) -> str:
    return f"{ABOUT}:{decision.pk}"


def queue_decision_email(decision) -> None:
    """After the decision's transaction commits, the worker sends it."""
    from comms.tasks import send_arc_decision_email

    decision_id = decision.pk
    transaction.on_commit(lambda: send_arc_decision_email.defer(decision_id=decision_id))


def _addresses(person) -> list[str]:
    from community import services as community

    out = []
    login = person.user.email if person.user_id else ""
    for address in [login or person.email, *community.notice_emails(person)]:
        if address and address.lower() not in [a.lower() for a in out]:
            out.append(address)
    return out


def send_decision_email(decision_id: int) -> int:
    """Email the applicant (one message per address). Returns how many were
    sent now."""
    from arc.models import ArcState, Decision
    from comms import bounces
    from comms.models import OutgoingEmail
    from core.email import NOTICE, mail_about, mail_kind
    from core.text import single_line

    decision = Decision.objects.filter(pk=decision_id).select_related("request").first()
    if decision is None:
        return 0
    with system_access("ARC decision email"):
        req = decision.request
        applicant = req.applicant
        addresses = _addresses(applicant)
        done = set(
            OutgoingEmail.objects.filter(about=about(decision)).values_list(
                "address_hash", flat=True
            )
        )
    outcome = dict(ArcState.choices).get(decision.outcome, decision.outcome).lower()
    link = settings.PORTAL_BASE_URL + reverse("arc_detail", args=[req.pk])
    subject = f"[{settings.SITE_NAME}] Decision on your architectural request"
    body = (
        f"The {'board' if decision.is_appeal else 'architectural committee'} has decided your "
        f'request "{single_line(req.title)}": {outcome}.\n\n'
        "Sign in to read the decision letter (the reasons, any conditions and how to appeal):\n"
        f"{link}\n\n-- \n{settings.SITE_NAME}"
    )
    sent = 0
    for address in addresses:
        if bounces.address_hash(address) in done:
            continue  # this address already got it (a retry)
        with mail_kind(NOTICE), mail_about(ABOUT, decision.pk):
            sent += send_mail(subject, body, None, [address])
    return sent


def delivery_for(req, user) -> dict:
    """Decision id -> delivery rows, for the board on the ARC request page.
    Only for those who can see every ARC request and are not on the
    homeowner's side of this one (the caller already got the request through
    its scoped lookup). No addresses."""
    from arc import services
    from comms import delivery
    from core import policy

    if not policy.can(user, "arc.view_all") or services.is_recused(user, req):
        return {}
    return {d.pk: delivery.status_for(about(d)) for d in req.decisions.all()}
