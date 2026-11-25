from datetime import timedelta

import pytest
from django.test import RequestFactory
from django.utils import timezone

from accounts.models import User
from audit import actions
from audit.models import ActorType, AuditEvent, AuditEventNetwork, Channel
from audit.services import (
    AuditError,
    hash_identifier,
    purge_expired_ip_addresses,
    record_event,
)

TEST_ACTION = actions.ActionSpec("test.thing", "Test action", frozenset({"count", "label"}))


@pytest.fixture(autouse=True)
def _test_action():
    actions.register(TEST_ACTION)
    yield
    actions.ACTIONS.pop(TEST_ACTION.name, None)


@pytest.fixture
def web_request(rf: RequestFactory):
    request = rf.get("/somewhere", REMOTE_ADDR="203.0.113.9")
    request.request_id = "abc123"
    return request


@pytest.mark.django_db
def test_records_actor_target_ip_and_request_id(web_request):
    user = User.objects.create_user("actor@example.com")
    web_request.user = user
    event = record_event("test.thing", request=web_request, target=user, metadata={"count": 2})
    assert event.actor == user
    assert event.actor_type == ActorType.USER
    assert event.channel == Channel.WEB
    assert event.target_type == "accounts.user"
    assert event.target_id == str(user.pk)
    assert event.request_id == "abc123"
    assert event.metadata == {"count": 2}
    assert event.network.ip_address == "203.0.113.9"


@pytest.mark.django_db
def test_anonymous_and_system_actors(web_request):
    from django.contrib.auth.models import AnonymousUser

    web_request.user = AnonymousUser()
    assert record_event("test.thing", request=web_request).actor_type == ActorType.ANONYMOUS
    system = record_event("test.thing")
    assert system.actor_type == ActorType.SYSTEM
    assert system.channel == Channel.WORKER
    assert not AuditEventNetwork.objects.filter(event=system).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("action", "metadata"),
    [
        ("no.such.action", {}),
        ("test.thing", {"email": "owner@example.com"}),  # field not on the allowlist
        ("test.thing", {"label": {"nested": "value"}}),  # not a scalar
        ("test.thing", {"label": "x" * 500}),  # too long
    ],
)
def test_rejects_unregistered_actions_and_unsafe_metadata(action, metadata):
    with pytest.raises(AuditError):
        record_event(action, metadata=metadata)
    assert not AuditEvent.objects.filter(action=action).exists()


def test_hash_identifier_is_keyed_and_normalised(settings):
    settings.AUDIT_HASH_KEY = "key-one-" + "x" * 30
    first = hash_identifier("  Someone@Example.com ")
    assert first == hash_identifier("someone@example.com")
    assert "someone" not in first
    settings.AUDIT_HASH_KEY = "key-two-" + "x" * 30
    assert hash_identifier("someone@example.com") != first


def test_hash_identifier_requires_key(settings):
    settings.AUDIT_HASH_KEY = ""
    with pytest.raises(AuditError):
        hash_identifier("someone@example.com")


@pytest.mark.django_db
def test_purge_removes_only_expired_ips_and_is_itself_audited(settings, rf):
    settings.AUDIT_IP_RETENTION_DAYS = 30
    now = timezone.now()
    old = record_event("test.thing", request=rf.get("/", REMOTE_ADDR="198.51.100.1"))
    recent = record_event("test.thing", request=rf.get("/", REMOTE_ADDR="198.51.100.2"))
    AuditEventNetwork.objects.filter(event=old).update(recorded_at=now - timedelta(days=31))

    assert purge_expired_ip_addresses(now=now) == 1

    assert not AuditEventNetwork.objects.filter(event=old).exists()
    assert AuditEventNetwork.objects.filter(event=recent).exists()
    assert AuditEvent.objects.filter(pk=old.pk).exists()  # the event itself stays
    purge_event = AuditEvent.objects.filter(action="audit.ip_purged").latest("id")
    assert purge_event.metadata == {"purged": 1, "retention_days": 30}
