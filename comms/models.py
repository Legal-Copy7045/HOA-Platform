from django.conf import settings
from django.db import models
from django.utils import timezone


class Audience(models.TextChoices):
    MEMBERS = "members", "All owners and board"
    BOARD = "board", "Board, manager and auditor only"


class AudienceQuerySet(models.QuerySet):
    def for_user(self, user):
        from core import policy

        if not policy.can(user, "announcement.view"):
            return self.none()
        if policy.can(user, "document.view_board"):
            return self
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
            )
        ]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Events are cancelled, not deleted.")
