"""Append-only audit trail (CLAUDE.md "Audit", decisions D7 and D8).

``AuditEvent`` is permanent and can never change. The source IP address lives
in ``AuditEventNetwork`` so it can be purged after the retention period
without touching the event itself.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel


class ActorType(models.TextChoices):
    USER = "user", "User"
    ANONYMOUS = "anonymous", "Anonymous"
    SYSTEM = "system", "System"


class Channel(models.TextChoices):
    WEB = "web", "Web"
    CLI = "cli", "Management command"
    WORKER = "worker", "Background worker"


class Outcome(models.TextChoices):
    SUCCESS = "success", "Success"
    FAILURE = "failure", "Failure"
    DENIED = "denied", "Denied"


class AuditEvent(AppendOnlyModel):
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    action = models.CharField(max_length=64, db_index=True)
    outcome = models.CharField(max_length=16, choices=Outcome.choices, default=Outcome.SUCCESS)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    # PROTECT: users are never deleted, and audit history must keep its actor.
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    # True when the actor held the sysadmin role at the time (board-visible view).
    actor_is_sysadmin = models.BooleanField(default=False, db_index=True)
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.WEB)
    target_type = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    # Keyed hash of an identifier we must not store in clear, e.g. the email
    # typed in a failed login for an account that does not exist.
    subject_hash = models.CharField(max_length=64, blank=True)
    request_id = models.CharField(max_length=32, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [models.Index(fields=["target_type", "target_id"])]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(actor_type="user") | models.Q(actor__isnull=False),
                name="audit_event_user_actor_present",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} #{self.pk}"


class AuditEventNetwork(models.Model):
    """Source address of an audit event; deleted after AUDIT_IP_RETENTION_DAYS."""

    event = models.OneToOneField(
        AuditEvent, on_delete=models.PROTECT, primary_key=True, related_name="network"
    )
    ip_address = models.GenericIPAddressField()
    recorded_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self) -> str:
        return f"network for event #{self.event_id}"
