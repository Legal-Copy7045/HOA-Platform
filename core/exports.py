"""'Download my data': each app contributes its own section (CLAUDE.md: owners
see only their own data). Register with @exporter("name")."""

from __future__ import annotations

from collections.abc import Callable

_EXPORTERS: dict[str, Callable] = {}


def exporter(name: str):
    def decorator(fn):
        _EXPORTERS[name] = fn
        return fn

    return decorator


def export_for(user) -> dict:
    data = {}
    for name, fn in sorted(_EXPORTERS.items()):
        data[name] = fn(user)
    return data


@exporter("account")
def _account(user):
    return {"email": user.email, "date_joined": user.date_joined.isoformat()}


@exporter("community")
def _community(user):
    from community.models import (
        BoardSeat,
        ContactProfile,
        OfficeHolding,
        Ownership,
        Person,
        RoleAssignment,
    )

    person = Person.objects.for_user(user).filter(user=user).first()
    if person is None:
        return {}
    profile = ContactProfile.objects.for_user(user).filter(person=person).first()
    own = {"person": person}
    return {
        "name": person.display_name,
        "email_on_file": person.email,
        "profile": {
            "phone": profile.phone,
            "mailing_address": profile.mailing_address,
            "mailing_address_for_post": {
                "same_as_home": profile.mail_to_home,
                "street": profile.mail_street,
                "unit": profile.mail_unit,
                "city": profile.mail_city,
                "state": profile.mail_state,
                "zip": profile.mail_zip,
            },
            "electronic_notice_consent": profile.electronic_notice_consent,
            "notice_consent_version": profile.notice_consent_version,
            "consent_changed_at": profile.consent_changed_at.isoformat()
            if profile.consent_changed_at
            else None,
            "in_directory": profile.in_directory,
        }
        if profile
        else None,
        "ownerships": [
            {
                "address": o.lot.label,
                "lot": o.lot.number,
                "type": o.ownership_type,
                "start": o.start_date.isoformat(),
                "end": o.end_date.isoformat() if o.end_date else None,
            }
            for o in Ownership.objects.for_user(user).filter(**own).select_related("lot")
        ],
        "board_seats": [
            {"type": s.seat_type, "start": s.term_start.isoformat()}
            for s in BoardSeat.objects.for_user(user).filter(**own)
        ],
        "offices": [o.office for o in OfficeHolding.objects.for_user(user).filter(**own)],
        "roles": [r.role for r in RoleAssignment.objects.for_user(user).filter(**own)],
    }
