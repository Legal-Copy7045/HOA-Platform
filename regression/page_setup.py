"""Helpers for the page stage.

python regression/page_setup.py reset-db <name>     # drop and create the database
python regression/page_setup.py sessions <out.json> # one signed-in session per role
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


def sessions(out: str) -> None:
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
        owner = owned.select_related("person").order_by("-lot__number").first().person
    result = {
        "board": login(board, "reg-board@example.com"),
        "owner": login(owner, "reg-owner@example.com"),
        "manager": login(
            staff("Regression Manager", "reg-mgr@example.com", Role.MANAGER), "reg-mgr@example.com"
        ),
        "auditor": login(
            staff("Regression Auditor", "reg-aud@example.com", Role.AUDITOR), "reg-aud@example.com"
        ),
    }
    Path(out).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    command, arg = sys.argv[1], sys.argv[2]
    reset_db(arg) if command == "reset-db" else sessions(arg)
