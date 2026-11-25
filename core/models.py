"""Shared models. Also ensures Django sends core's post_migrate signal, which
applies runtime DB grants (see core.db_roles)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel


class WorkflowEvent(AppendOnlyModel):
    """One step in any workflow (ARC, invoice, violation, ...). Never changed."""

    workflow = models.CharField(max_length=32)
    object_id = models.CharField(max_length=32)
    action = models.CharField(max_length=32)
    from_state = models.CharField(max_length=32)
    to_state = models.CharField(max_length=32)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["workflow", "object_id"])]

    def __str__(self) -> str:
        return f"{self.workflow}#{self.object_id} {self.action}"


class Heartbeat(models.Model):
    """Last time a named background job ran (the worker's pulse)."""

    name = models.CharField(max_length=40, primary_key=True)
    at = models.DateTimeField()

    def __str__(self) -> str:
        return self.name


class ErrorTally(models.Model):
    """Server errors per hour (counts only: no URLs, no messages, no users)."""

    hour = models.DateTimeField(primary_key=True)
    count = models.PositiveIntegerField(default=0)

    def __str__(self) -> str:
        return f"{self.hour:%Y-%m-%d %H}:00 ({self.count})"


class AlertState(models.Model):
    """Which health checks are failing and when sysadmins were last told."""

    name = models.CharField(max_length=40, primary_key=True)
    failing = models.BooleanField(default=False)
    since = models.DateTimeField(null=True, blank=True)
    last_alerted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return self.name


class FeatureSetting(models.Model):
    """The board's choice for one optional feature (core.features). A missing
    row means the feature's default."""

    key = models.CharField(max_length=40, primary_key=True)
    enabled = models.BooleanField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"{self.key}: {'on' if self.enabled else 'off'}"


class UpdateStatus(models.Model):
    """One row, kept by the VM's update agent (infra/poc/portal-agent.sh):
    which commit runs, whether GitHub has a newer one, and an install
    requested from System status. The web app only ever sets the two
    "requested" fields; the agent on the host does the work (decision D25)."""

    branch = models.CharField(max_length=100, blank=True)
    running_commit = models.CharField(max_length=40, blank=True)
    latest_commit = models.CharField(max_length=40, blank=True)
    behind = models.PositiveIntegerField(default=0)
    changes = models.TextField(blank=True)  # commit subjects, newest first
    checked_at = models.DateTimeField(null=True, blank=True)
    check_requested_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=12, default="idle"
    )  # idle requested running succeeded failed
    state_changed_at = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    message = models.CharField(max_length=300, blank=True)
    agent_seen_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"update status ({self.state})"

    @classmethod
    def get(cls) -> UpdateStatus:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
