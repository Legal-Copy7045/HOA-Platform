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


class ErrorSignature(models.Model):
    """One kind of server error in one hour (decision D45): which exception
    class, which route (URL name or view path, never the request path, query
    string or URL arguments), where in our code, the status and the release,
    with a count. Never an exception message, source line, variable, user or
    IP address."""

    hour = models.DateTimeField()
    fingerprint = models.CharField(max_length=64)
    exc_type = models.CharField(max_length=120)
    route = models.CharField(max_length=200, blank=True)
    method = models.CharField(max_length=10, blank=True)
    where = models.CharField(max_length=300, blank=True)
    status = models.PositiveSmallIntegerField()
    release = models.CharField(max_length=40, blank=True)
    count = models.PositiveIntegerField(default=0)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["hour", "fingerprint"], name="error_signature_per_hour")
        ]
        indexes = [models.Index(fields=["last_seen"])]

    def __str__(self) -> str:
        return f"{self.hour:%Y-%m-%d %H}:00 {self.exc_type} ({self.count})"


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
    # The release number (VERSION) on GitHub's copy of the branch (D37).
    # A database default, so the agent's INSERT works before and after this
    # column exists (an update pulls the agent before migrating).
    latest_version = models.CharField(max_length=20, blank=True, default="", db_default="")
    behind = models.PositiveIntegerField(default=0)
    changes = models.TextField(blank=True)  # commit subjects, newest first
    checked_at = models.DateTimeField(null=True, blank=True)
    check_requested_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=12, default="idle"
    )  # idle scheduled requested running succeeded failed
    state_changed_at = models.DateTimeField(null=True, blank=True)
    # "Install tonight": the agent turns a scheduled update into a request at
    # this time (D38). Nullable, so the agent's INSERT never has to name it.
    scheduled_for = models.DateTimeField(null=True, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    message = models.CharField(max_length=300, blank=True)
    agent_seen_at = models.DateTimeField(null=True, blank=True)
    # Host security services as the agent last saw them, e.g.
    # {"falco": "active", "auditd": "active", ...} (infra/poc/harden-host.sh).
    host_security = models.JSONField(default=dict, blank=True)
    # Server memory, swap and load, from the agent every minute (D39).
    # Nullable, so the agent's INSERT never has to name it.
    host_resources = models.JSONField(null=True, blank=True)

    def __str__(self) -> str:
        return f"update status ({self.state})"

    @classmethod
    def get(cls) -> UpdateStatus:
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class HostTask(models.Model):
    """A server task requested on Admin -> Server tasks (decision D36): a
    backup, a restore drill, a lock check... The web app only inserts the
    request; the VM agent (infra/poc/portal-agent.sh) claims it, runs the
    fixed command for its kind and writes back the state and a short summary.
    Rows are kept as the history; at most one is requested or running."""

    class Kind(models.TextChoices):
        BACKUP = "backup", "Take a backup"
        RESTORE_DRILL = "restore_drill", "Restore drill"
        RESTORE_DRILL_OFFSITE = "restore_drill_offsite", "Restore drill from the off-site copy"
        OFFSITE_LOCK_CHECK = "offsite_lock_check", "Check the off-site lock"
        HARDEN_HOST = "harden_host", "Re-apply host security"
        HEALTH_CHECK = "health_check", "Run the health checks"
        RESEED_DEMO = "reseed_demo", "Reseed with demo data"

    class State(models.TextChoices):
        REQUESTED = "requested", "Waiting for the server"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Done"
        FAILED = "failed", "Failed"
        REFUSED = "refused", "Refused"

    ACTIVE = (State.REQUESTED, State.RUNNING)

    kind = models.CharField(max_length=30, choices=Kind.choices)
    # reseed_demo only: the dataset (community/demo), checked against the
    # registry by the portal and again by the agent; [a-z_] only (D53). The
    # agent may read this before the migration lands, hence the db_default.
    dataset = models.CharField(max_length=40, blank=True, default="", db_default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    requested_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=10, choices=State.choices, default=State.REQUESTED)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Written by the agent: printable ASCII only, capped (infra/poc/host-tasks.sh).
    summary = models.CharField(max_length=500, blank=True)
    output_tail = models.TextField(blank=True)  # at most 40 lines of 200 characters
    # Set by `manage.py record_host_tasks` once the result is in the audit log.
    audited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                models.Value(1),
                condition=models.Q(state__in=["requested", "running"]),
                name="core_hosttask_one_active",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=["requested", "running", "succeeded", "failed", "refused"]
                ),
                name="core_hosttask_state_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(dataset__regex=r"^[a-z_]{0,40}$"),
                name="core_hosttask_dataset_plain",
            ),
        ]

    def __str__(self) -> str:
        return f"server task #{self.pk} ({self.kind}, {self.state})"

    def delete(self, *args, **kwargs):
        raise PermissionError("Server task history is kept.")


class DemoSeed(models.Model):
    """Which demo dataset was loaded, and when (``seed_demo``, decision D53).
    Shown on Settings -> Demo data. A reseed replaces the whole database, so
    the newest row is the data the portal holds now."""

    dataset = models.CharField(max_length=40)
    seeded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-seeded_at", "-id"]

    def __str__(self) -> str:
        return f"demo seed #{self.pk} ({self.dataset})"

    def delete(self, *args, **kwargs):
        raise PermissionError("Demo seed records are kept.")
