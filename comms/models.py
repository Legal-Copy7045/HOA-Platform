from django.conf import settings
from django.db import models
from django.utils import timezone


class Audience(models.TextChoices):
    MEMBERS = "members", "All owners and board"
    BOARD = "board", "Board, manager and auditor only"
    COMMITTEE = "committee", "One committee (and the board)"


class AudienceQuerySet(models.QuerySet):
    def for_user(self, user):
        from core import policy

        if not policy.can(user, "announcement.view"):
            return self.none()
        if policy.can(user, "document.view_board"):
            return self
        committees = policy.roles_for(user).committee_ids
        if committees and self.model is Event:
            return self.filter(
                models.Q(audience=Audience.MEMBERS)
                | models.Q(audience=Audience.COMMITTEE, committee_id__in=committees)
            )
        return self.filter(audience=Audience.MEMBERS)


class Announcement(models.Model):
    """Published announcements are never edited; post a correction instead."""

    title = models.CharField(max_length=200)
    body = models.TextField()
    audience = models.CharField(max_length=16, choices=Audience.choices, default=Audience.MEMBERS)
    urgent = models.BooleanField(default=False)
    published_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    emailed_count = models.PositiveIntegerField(default=0)

    objects = AudienceQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at", "-id"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Announcements are kept; publish a correction instead.")


class AnnouncementRead(models.Model):
    announcement = models.ForeignKey(Announcement, on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["announcement", "user"], name="announcement_read_once")
        ]

    def __str__(self) -> str:
        return f"read #{self.pk}"


class Event(models.Model):
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    audience = models.CharField(max_length=16, choices=Audience.choices, default=Audience.MEMBERS)
    cancelled = models.BooleanField(default=False)
    committee = models.ForeignKey(
        "governance.Committee", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = AudienceQuerySet.as_manager()

    class Meta:
        ordering = ["starts_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(ends_at__gte=models.F("starts_at")),
                name="event_ends_after_start",
            ),
            models.CheckConstraint(
                condition=(models.Q(audience="committee") & models.Q(committee__isnull=False))
                | (~models.Q(audience="committee") & models.Q(committee__isnull=True)),
                name="event_committee_matches_audience",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Events are cancelled, not deleted.")


# --- Message threads (Milestone 3) ----------------------------------------------------

from core.append_only import AppendOnlyModel  # noqa: E402


class ThreadMessage(AppendOnlyModel):
    """One message on the thread of a request, ARC request, violation or work
    order. Append-only: a correction is a new message. ``internal`` messages
    are for the board, manager and reviewers only (never the owner side);
    who may read a thread is decided by comms.threads, through the subject's
    own scoping."""

    subject_kind = models.CharField(max_length=24)
    subject_id = models.PositiveBigIntegerField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    internal = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["subject_kind", "subject_id"])]

    def __str__(self) -> str:
        return f"message #{self.pk}"


class FaqEntry(models.Model):
    """A question and answer on the FAQ page (board and manager keep it)."""

    question = models.CharField(max_length=300)
    answer = models.TextField()
    category = models.CharField(max_length=80, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    published = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["category", "sort_order", "question"]

    def __str__(self) -> str:
        return self.question

    def delete(self, *args, **kwargs):
        raise PermissionError("FAQ entries are unpublished, not deleted.")


class DigestItem(models.Model):
    """A routine update held for someone's weekly digest. Only a subject line
    and a link - never the content (emails carry links only)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    subject = models.CharField(max_length=200)
    link = models.CharField(max_length=500)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"digest item #{self.pk}"
