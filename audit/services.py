"""The only supported way to write audit events. Other apps call these."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import models, transaction
from django.http import HttpRequest
from django.utils import timezone

from audit.actions import ACTIONS
from audit.models import ActorType, AuditEvent, AuditEventNetwork, Channel, Outcome
from core.net import client_ip

MAX_METADATA_VALUE_LENGTH = 200
_SCALARS = (str, int, bool, type(None))


class AuditError(ValueError):
    pass


def hash_identifier(value: str) -> str:
    """Keyed HMAC-SHA256 of a normalised identifier (decision D7).

    Lets the board spot repeats ("the same unknown address tried 40 times") and
    check a specific address later, without storing the address.
    """
    key = settings.AUDIT_HASH_KEY
    if not key:
        raise AuditError("AUDIT_HASH_KEY is not configured")
    normalised = (value or "").strip().lower()
    return hmac.new(key.encode(), normalised.encode(), hashlib.sha256).hexdigest()


def _validate_metadata(action: str, metadata: dict[str, Any]) -> dict[str, Any]:
    spec = ACTIONS.get(action)
    if spec is None:
        raise AuditError(f"Unknown audit action {action!r}; add it to audit/actions.py")
    unexpected = set(metadata) - spec.metadata_fields
    if unexpected:
        raise AuditError(f"Metadata fields not allowed for {action}: {sorted(unexpected)}")
    for key, value in metadata.items():
        if not isinstance(value, _SCALARS):
            raise AuditError(f"Metadata {key!r} must be a scalar")
        if isinstance(value, str) and len(value) > MAX_METADATA_VALUE_LENGTH:
            raise AuditError(f"Metadata {key!r} is too long")
    return dict(metadata)


def _is_sysadmin(user) -> bool:
    # Replaced in step 4 by the policy layer's role lookup.
    checker = getattr(user, "is_sysadmin", None)
    return bool(checker() if callable(checker) else checker)


def record_event(
    action: str,
    *,
    request: HttpRequest | None = None,
    actor=None,
    target: models.Model | None = None,
    outcome: str = Outcome.SUCCESS,
    metadata: dict[str, Any] | None = None,
    subject_hash: str = "",
    channel: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    clean_metadata = _validate_metadata(action, metadata or {})
    if outcome not in Outcome.values:
        raise AuditError(f"Unknown outcome {outcome!r}")

    if actor is None and request is not None:
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            actor = user
    if actor is not None:
        actor_type = ActorType.USER
    elif request is not None:
        actor_type = ActorType.ANONYMOUS
    else:
        actor_type = ActorType.SYSTEM

    if channel is None:
        channel = Channel.WEB if request is not None else Channel.WORKER

    with transaction.atomic():
        event = AuditEvent.objects.create(
            occurred_at=occurred_at or timezone.now(),
            action=action,
            outcome=outcome,
            actor_type=actor_type,
            actor=actor,
            actor_is_sysadmin=actor is not None and _is_sysadmin(actor),
            channel=channel,
            target_type=target._meta.label_lower if target is not None else "",
            target_id=str(target.pk) if target is not None else "",
            subject_hash=subject_hash,
            request_id=getattr(request, "request_id", "") if request is not None else "",
            metadata=clean_metadata,
        )
        ip = client_ip(request) if request is not None else None
        if ip:
            AuditEventNetwork.objects.create(event=event, ip_address=ip)
    return event


def purge_expired_ip_addresses(now: datetime | None = None) -> int:
    """Delete IP rows older than the retention period, and audit the purge."""
    now = now or timezone.now()
    days = settings.AUDIT_IP_RETENTION_DAYS
    cutoff = now - timedelta(days=days)
    with transaction.atomic():
        purged, _ = AuditEventNetwork.objects.filter(recorded_at__lt=cutoff).delete()
        record_event(
            "audit.ip_purged",
            metadata={"purged": purged, "retention_days": days},
            channel=Channel.WORKER,
        )
    return purged
