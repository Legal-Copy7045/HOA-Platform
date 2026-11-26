"""Helpers for the page stage.

python regression/page_setup.py reset-db <name>     # drop and create the database
python regression/page_setup.py sessions <out.json> # one signed-in session per role
python regression/page_setup.py walk-sessions <out.json>  # every role, for the walkthrough

The page stage uses six roles (board, owner, manager, auditor, tenant, former
owner) and a signed-out passkey tester. The walkthrough stage adds the
authorized and view-only contacts, an officer, an ARC reviewer and a sysadmin,
and makes no passkey tester (the two stages run side by side, each on its own
database).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def reset_db(name: str) -> None:
    import psycopg
    from psycopg import sql

    url = os.environ["DATABASE_URL"].rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def sessions(out: str, *, every_role: bool = False) -> None:
    import django

    django.setup()
    from allauth.mfa.models import Authenticator
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
    from django.contrib.sessions.backends.db import SessionStore

    from accounts.models import User
    from community import services
    from community.models import AppointedBy, Ownership, Person, Role
    from community.tests.factories import enroll_totp
    from core.policy import SYSTEM
    from core.scoping import system_access

    def login(person: Person, email: str) -> str:
        if person.user_id is None:
            user = User.objects.filter(email=email).first() or User.objects.create_user(email)
            services.link_login(person, user, actor=SYSTEM)
            person.refresh_from_db()
        user = person.user
        if not Authenticator.objects.filter(user=user).exists():
            enroll_totp(user)
        store = SessionStore()
        store[SESSION_KEY] = str(user.pk)
        store[BACKEND_SESSION_KEY] = "allauth.account.auth_backends.AuthenticationBackend"
        store[HASH_SESSION_KEY] = user.get_session_auth_hash()
        store.create()
        return store.session_key

    def staff(name: str, email: str, role: str) -> Person:
        person = services.record_person(display_name=name, email=email, actor=SYSTEM)
        services.grant_role(person=person, role=role, appointed_by=AppointedBy.BOARD, actor=SYSTEM)
        return person

    with system_access("regression personas"):
        board = Person.objects.filter(board_seats__isnull=False).first()
        owned = Ownership.objects.active().exclude(person__board_seats__isnull=False)
        # The owner of the last lot: the board's crawl reaches low-numbered lots
        # first, so "owner is refused board page" tests someone else's lot.
        first = owned.select_related("person", "lot").order_by("-lot__number").first()
        owner, owner_lot = first.person, first.lot
        from community.models import AccountContact

        tenant = AccountContact.objects.active().filter(kind="tenant").first().person
        sellers = Ownership.objects.filter(end_date__isnull=False).exclude(
            person__in=Ownership.objects.active().values("person")
        )
        former = sellers.select_related("person").first().person
    result = {
        "tenant": login(tenant, "reg-tenant@example.com"),
        "former_owner": login(former, "reg-former@example.com"),
        "board": login(board, "reg-board@example.com"),
        "owner": login(owner, "reg-owner@example.com"),
        "manager": login(
            staff("Regression Manager", "reg-mgr@example.com", Role.MANAGER), "reg-mgr@example.com"
        ),
        "auditor": login(
            staff("Regression Auditor", "reg-aud@example.com", Role.AUDITOR), "reg-aud@example.com"
        ),
    }
    if every_role:
        result.update(_more_roles(login, staff))
    Path(out).write_text(json.dumps(result), encoding="utf-8")
    if every_role:
        return

    # A signed-out tester for the passkey stage: password + authenticator app.
    from accounts.tests.helpers import PASSWORD
    from community.tests.factories import days_ago

    email = "reg-passkey@example.com"
    tester = User.objects.filter(email=email).first()
    if tester is None:
        tester = User.objects.create_user(email, password=PASSWORD)
        person = services.record_person(display_name="Passkey Tester", email=email, actor=SYSTEM)
        services.link_login(person, tester, actor=SYSTEM)
        services.record_ownership(
            person=person, lot=owner_lot, start_date=days_ago(10), actor=SYSTEM
        )
    Authenticator.objects.filter(user=tester).delete()
    secret = enroll_totp(tester)
    Path(out).with_name("passkey-user.json").write_text(
        json.dumps({"email": email, "password": PASSWORD, "secret": secret}), encoding="utf-8"
    )


def _more_roles(login, staff) -> dict[str, str]:
    """The walkthrough's extra personas, from the seeded demo community."""
    from community import services
    from community.models import AccountContact, OfficeHolding, Person, Role, RoleAssignment
    from core.policy import SYSTEM
    from core.scoping import system_access

    with system_access("regression walkthrough personas"):
        officer = OfficeHolding.objects.active().order_by("office").first().person
        # A board member who holds no office: board rights from the seat only.
        plain = (
            Person.objects.filter(board_seats__isnull=False)
            .exclude(pk__in=OfficeHolding.objects.active().values("person"))
            .first()
        )
        contacts = AccountContact.objects.active().select_related("person")
        authorized = contacts.filter(kind="authorized").first().person
        view_only = contacts.filter(kind="view_only").first().person
        reviewer = (
            RoleAssignment.objects.active()
            .filter(role=Role.ARC_REVIEWER)
            .exclude(person__board_seats__isnull=False)
            .first()
            .person
        )
        sysadmin = services.record_person(
            display_name="Regression Sysadmin", email="reg-sys@example.com", actor=SYSTEM
        )
        # Sysadmin normally needs a nomination and a second board member's
        # approval; this throwaway demo database records it directly.
        services._record_role(
            person=sysadmin,
            role=Role.SYSADMIN,
            start_date=services.today(),
            appointed_by="",
            citation="",
            actor=SYSTEM,
            request=None,
        )
    out = {
        "officer": login(officer, "reg-officer@example.com"),
        "authorized": login(authorized, "reg-authorized@example.com"),
        "view_only": login(view_only, "reg-viewonly@example.com"),
        "arc_reviewer": login(reviewer, "reg-arc@example.com"),
        "sysadmin": login(sysadmin, "reg-sys@example.com"),
    }
    if plain is not None:
        out["board"] = login(plain, "reg-board2@example.com")
    return out


def totp(secret: str) -> None:
    import django

    django.setup()
    from accounts.tests.helpers import current_totp

    print(current_totp(secret))


if __name__ == "__main__":
    command, arg = sys.argv[1], sys.argv[2]
    if command == "walk-sessions":
        sessions(arg, every_role=True)
    else:
        {"reset-db": reset_db, "sessions": sessions, "totp": totp}[command](arg)
