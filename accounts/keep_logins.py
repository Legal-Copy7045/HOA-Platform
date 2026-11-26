"""Keep the real logins across a demo reseed (decision D53).

``infra/poc/reseed-demo.sh`` replaces the whole database with a demo dataset.
Before it empties the database it runs ``keep_logins export`` (with the
owner role) into a root-only temporary file; after migrating, and before
seeding, ``keep_logins import`` puts them back; the file is shredded.

Kept: every login that no seed created, i.e. not linked to a demo person
(whose names end in "(demo)"), with

* the login itself, under the same id: a passkey names its login by id, so a
  new id would make the passkey useless (allauth's WebAuthn user handle);
  password hash, active flag and dates;
* its MFA authenticators (passkeys, authenticator app, recovery codes) and
  its email addresses as allauth stores them;
* its person (name and email) and an active sysadmin role, if any;
* the server-task history its owner started (same ids, so the reseed task
  that is running can report back), and the backup heartbeats.

Not kept: homes, board seats, offices, other roles and everything else; the
dataset brings its own. Demo logins are never exported.
"""

from __future__ import annotations

import datetime
import json

from django.conf import settings
from django.core.management.color import no_style
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from community import demo

FORMAT = 1
HEARTBEATS = ("backup_local", "backup_offsite", "offsite_lock")
TASK_FIELDS = (
    "id",
    "kind",
    "dataset",
    "requested_by_id",
    "requested_at",
    "state",
    "started_at",
    "finished_at",
    "summary",
    "output_tail",
    "audited_at",
)


class KeepLoginsError(Exception):
    pass


def _require_demo_allowed() -> None:
    if not settings.SEED_DEMO_ALLOWED:
        raise KeepLoginsError("Refusing: only on a demo deployment (SEED_DEMO_ALLOWED=true).")


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value):
    return parse_datetime(value) if value else None


def kept_users():
    """Logins no seed created (not linked to a demo person)."""
    from accounts.models import User
    from community.models import Person
    from core.scoping import system_access

    with system_access("keep_logins: find demo people"):
        demo_user_ids = set(
            Person.objects.filter(
                user__isnull=False, display_name__endswith=demo.DEMO_SUFFIX
            ).values_list("user_id", flat=True)
        )
    return User.objects.exclude(pk__in=demo_user_ids).order_by("pk")


def export_data() -> dict:
    from allauth.account.models import EmailAddress
    from allauth.mfa.models import Authenticator

    from community.models import Person, Role, RoleAssignment
    from core.models import Heartbeat, HostTask
    from core.scoping import system_access

    _require_demo_allowed()
    users = []
    kept_ids = set()
    with system_access("keep_logins export"):
        for user in kept_users():
            kept_ids.add(user.pk)
            person = Person.objects.filter(user=user).first()
            roles = []
            if person is not None:
                active = RoleAssignment.objects.filter(person=person, role=Role.SYSADMIN).active()
                roles = [
                    {
                        "role": r.role,
                        "start_date": r.start_date.isoformat(),
                        "citation": r.citation,
                    }
                    for r in active
                ]
            users.append(
                {
                    "id": user.pk,
                    "email": user.email,
                    "password": user.password,
                    "is_active": user.is_active,
                    "is_staff": user.is_staff,
                    "is_superuser": user.is_superuser,
                    "date_joined": _iso(user.date_joined),
                    "last_login": _iso(user.last_login),
                    "session_generation": user.session_generation,
                    "person": (
                        {"display_name": person.display_name, "email": person.email}
                        if person is not None
                        else None
                    ),
                    "roles": roles,
                    "authenticators": [
                        {
                            "type": a.type,
                            "data": a.data,
                            "created_at": _iso(a.created_at),
                            "last_used_at": _iso(a.last_used_at),
                        }
                        for a in Authenticator.objects.filter(user=user).order_by("pk")
                    ],
                    "email_addresses": [
                        {"email": e.email, "verified": e.verified, "primary": e.primary}
                        for e in EmailAddress.objects.filter(user=user).order_by("pk")
                    ],
                }
            )
    tasks = [
        {f: (_iso(v) if isinstance(v, datetime.datetime) else v) for f, v in row.items()}
        for row in HostTask.objects.filter(requested_by_id__in=kept_ids)
        .order_by("pk")
        .values(*TASK_FIELDS)
    ]
    heartbeats = [
        {"name": h.name, "at": _iso(h.at)} for h in Heartbeat.objects.filter(name__in=HEARTBEATS)
    ]
    return {"format": FORMAT, "users": users, "tasks": tasks, "heartbeats": heartbeats}


def import_data(data: dict) -> tuple[int, int]:
    """Re-create the exported logins in a freshly migrated database. Returns
    (logins, tasks). Refuses if any of the ids or emails is already taken."""
    from allauth.account.models import EmailAddress
    from allauth.mfa.models import Authenticator

    from accounts.models import User
    from audit.models import Channel
    from audit.services import record_event
    from community import services as community
    from core.models import Heartbeat, HostTask
    from core.policy import SYSTEM

    _require_demo_allowed()
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise KeepLoginsError("Not a keep_logins file this version can read.")
    users = data.get("users") or []
    ids = [u["id"] for u in users]
    emails = [u["email"].lower() for u in users]
    if User.objects.filter(pk__in=ids).exists() or any(
        User.objects.filter(email__iexact=e).exists() for e in emails
    ):
        raise KeepLoginsError("Refusing: a login with one of those ids or emails already exists.")
    with transaction.atomic():
        for row in users:
            user = User(
                pk=row["id"],
                email=row["email"],
                password=row["password"],
                is_active=row["is_active"],
                is_staff=row.get("is_staff", False),
                is_superuser=row.get("is_superuser", False),
                date_joined=_dt(row["date_joined"]) or timezone.now(),
                last_login=_dt(row.get("last_login")),
                session_generation=row.get("session_generation", 0),
            )
            user.save(force_insert=True)
            for a in row.get("authenticators", []):
                Authenticator.objects.create(
                    user=user,
                    type=a["type"],
                    data=a["data"],
                    created_at=_dt(a["created_at"]) or timezone.now(),
                    last_used_at=_dt(a.get("last_used_at")),
                )
            for e in row.get("email_addresses", []):
                EmailAddress.objects.create(
                    user=user, email=e["email"], verified=e["verified"], primary=e["primary"]
                )
            person_row = row.get("person")
            if person_row:
                person = community.record_person(
                    display_name=person_row["display_name"],
                    email=person_row.get("email", ""),
                    actor=SYSTEM,
                )
                community.link_login(person, user, actor=SYSTEM)
                for r in row.get("roles", []):
                    community._record_role(
                        person=person,
                        role=r["role"],
                        start_date=parse_date(r["start_date"]),
                        appointed_by="",
                        citation=r.get("citation") or "kept across a demo reseed",
                        actor=SYSTEM,
                        request=None,
                    )
        kept = set(ids)
        tasks = 0
        for t in data.get("tasks") or []:
            if t.get("requested_by_id") not in kept:
                continue
            values = {f: t.get(f) for f in TASK_FIELDS}
            for f in ("requested_at", "started_at", "finished_at", "audited_at"):
                values[f] = _dt(values[f])
            values["dataset"] = values["dataset"] or ""
            values["summary"] = values["summary"] or ""
            values["output_tail"] = values["output_tail"] or ""
            HostTask(**values).save(force_insert=True)
            tasks += 1
        for h in data.get("heartbeats") or []:
            if h.get("name") in HEARTBEATS and h.get("at"):
                Heartbeat.objects.update_or_create(name=h["name"], defaults={"at": _dt(h["at"])})
        # New rows (demo logins, later tasks) continue after the kept ids.
        with connection.cursor() as cursor:
            for statement in connection.ops.sequence_reset_sql(no_style(), [User, HostTask]):
                cursor.execute(statement)
        record_event(
            "system.logins_kept",
            actor=None,
            channel=Channel.CLI,
            metadata={"count": len(users), "tasks": tasks},
        )
    return len(users), tasks


def dumps(data: dict) -> str:
    return json.dumps(data, sort_keys=True)
