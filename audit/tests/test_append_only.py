"""Audit events cannot be changed or removed: model, trigger, and role layers."""

import psycopg
import pytest
from django.db import connection, transaction
from django.db.utils import DatabaseError

from audit.models import AuditEvent
from audit.services import record_event
from core.append_only import AppendOnlyError


@pytest.fixture
def event(db):
    return record_event("audit.ip_purged", metadata={"purged": 0, "retention_days": 365})


def test_model_refuses_update(event):
    event.action = "something.else"
    with pytest.raises(AppendOnlyError):
        event.save()


def test_model_refuses_delete(event):
    with pytest.raises(AppendOnlyError):
        event.delete()


def test_queryset_refuses_bulk_changes(event):
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.filter(pk=event.pk).update(action="x")
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.filter(pk=event.pk).delete()
    with pytest.raises(AppendOnlyError):
        AuditEvent.objects.bulk_update([event], ["action"])


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_auditevent SET action = 'tampered' WHERE id = %s",
        "DELETE FROM audit_auditevent WHERE id = %s",
    ],
)
def test_trigger_blocks_raw_sql_even_for_owner_role(event, statement):
    # The test connection is the owner role, so grants do not apply here:
    # only the trigger stands in the way.
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute(statement, [event.pk])


@pytest.mark.django_db
def test_trigger_blocks_truncate_even_for_owner_role():
    # No rows inserted in this transaction: pending FK checks would otherwise
    # block TRUNCATE before the trigger runs and hide what we are testing.
    with (
        pytest.raises(DatabaseError, match="append-only"),
        transaction.atomic(),
        connection.cursor() as cursor,
    ):
        cursor.execute("TRUNCATE audit_auditevent CASCADE")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_auditevent SET action = 'tampered' WHERE false",
        "DELETE FROM audit_auditevent WHERE false",
        "TRUNCATE audit_auditevent",
        "ALTER TABLE audit_auditevent DISABLE TRIGGER ALL",
        "DROP TRIGGER audit_auditevent_append_only_row ON audit_auditevent",
    ],
)
def test_app_role_is_refused_by_postgres(app_role_connection, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_role_connection.execute(statement)


@pytest.mark.django_db
def test_app_role_can_read_and_insert(app_role_connection):
    app_role_connection.execute("SELECT count(*) FROM audit_auditevent")
    row = app_role_connection.execute(
        "INSERT INTO audit_auditevent (occurred_at, action, outcome, actor_type,"
        " actor_is_sysadmin, channel, target_type, target_id, subject_hash,"
        " request_id, metadata) VALUES (now(), 'audit.ip_purged', 'success',"
        " 'system', false, 'worker', '', '', '', '', '{}') RETURNING id"
    ).fetchone()
    assert row[0] > 0
    # The committed row would outlive the test transaction, and no one can
    # delete it - by design. The test database is discarded after the run.
