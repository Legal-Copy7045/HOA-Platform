"""Meetings, the resolution/policy register, records requests and votes."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet

# --- Meetings -----------------------------------------------------------------------


class MeetingKind(models.TextChoices):
    BOARD = "board", "Board meeting"
    ANNUAL = "annual", "Annual owners' meeting"
    SPECIAL = "special", "Special owners' meeting"
    EXECUTIVE = "executive", "Executive session (board only)"
    COMMITTEE = "committee", "Committee meeting"


class MeetingStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    HELD = "held", "Held"
    CANCELLED = "cancelled", "Cancelled"


class MeetingQuerySet(models.QuerySet):
    def for_user(self, user):
        from core import policy

        if not policy.can(user, "meeting.view"):
            return self.none()
        if policy.can(user, "document.view_board"):
            return self
        return self.exclude(kind=MeetingKind.EXECUTIVE)


class Meeting(models.Model):
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=16, choices=MeetingKind.choices)
    starts_at = models.DateTimeField()
    location = models.CharField(max_length=200, blank=True)
    agenda = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=MeetingStatus.choices, default=MeetingStatus.SCHEDULED
    )
    notice_sent_at = models.DateTimeField(null=True, blank=True)
    minutes_document = models.ForeignKey(
        "documents.Document", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = MeetingQuerySet.as_manager()

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Meetings are cancelled, not deleted.")


class Attendance(models.Model):
    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="attendance")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    present = models.BooleanField(default=True)
    by_proxy = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["meeting", "person"], name="attendance_once")
        ]

    def __str__(self) -> str:
        return f"attendance #{self.pk}"


# --- Resolutions and policies (append-only register) -----------------------------


class ResolutionKind(models.TextChoices):
    RESOLUTION = "resolution", "Board resolution"
    POLICY = "policy", "Policy or rule"


class Resolution(AppendOnlyModel):
    """Adopted resolutions and policies. Never edited: a change is a new
    resolution that ``supersedes`` the old one."""

    number = models.CharField(max_length=20, unique=True)
    kind = models.CharField(max_length=16, choices=ResolutionKind.choices)
    title = models.CharField(max_length=200)
    text = models.TextField()
    adopted_on = models.DateField()
    meeting = models.ForeignKey(
        Meeting, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    votes_for = models.PositiveSmallIntegerField(default=0)
    votes_against = models.PositiveSmallIntegerField(default=0)
    abstentions = models.PositiveSmallIntegerField(default=0)
    supersedes = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by"
    )
    citation = models.CharField(max_length=200, blank=True, help_text="Authority, e.g. Bylaws §4.1")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-adopted_on", "-number"]
        constraints = [
            # A resolution is superseded at most once (review D10).
            models.UniqueConstraint(
                fields=["supersedes"],
                condition=models.Q(supersedes__isnull=False),
                name="resolution_superseded_once",
            )
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"


# --- Records requests (workflow) ------------------------------------------------------


class RecordsRequest(ScopedModel):
    requester = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    description = models.TextField()
    state = models.CharField(max_length=24, default="submitted")
    state_changed_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(default=timezone.now)
    due_on = models.DateField(null=True, blank=True)

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "requester"

    class Meta:
        ordering = ["-submitted_at"]

    def __str__(self) -> str:
        return f"Records request #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Records requests are kept.")


# --- Votes ---------------------------------------------------------------------------


class VoteKind(models.TextChoices):
    INFORMAL = "informal", "Informal poll (non-binding, one per owner)"
    FORMAL = "formal", "Formal vote (one per lot)"


class VoteStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


class Vote(models.Model):
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    kind = models.CharField(max_length=16, choices=VoteKind.choices)
    secret = models.BooleanField(default=False, help_text="Hide who chose what (counts only).")
    status = models.CharField(max_length=16, choices=VoteStatus.choices, default=VoteStatus.DRAFT)
    closes_at = models.DateTimeField(null=True, blank=True)
    # Formal votes: rule values and citations are frozen when the vote opens.
    quorum_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    pass_percent = models.PositiveSmallIntegerField(null=True, blank=True)
    rules_citation = models.CharField(max_length=400, blank=True)
    eligible_lots = models.PositiveSmallIntegerField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Votes are kept.")


class VoteOption(models.Model):
    vote = models.ForeignKey(Vote, on_delete=models.PROTECT, related_name="options")
    label = models.CharField(max_length=200)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.label


class Ballot(AppendOnlyModel):
    """One cast ballot. Never changed. Formal: one per lot; informal: one per person."""

    vote = models.ForeignKey(Vote, on_delete=models.PROTECT, related_name="ballots")
    option = models.ForeignKey(VoteOption, on_delete=models.PROTECT, related_name="+")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    lot = models.ForeignKey(
        "community.Lot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    cast_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    cast_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["vote", "lot"],
                condition=models.Q(lot__isnull=False),
                name="ballot_one_per_lot",
            ),
            models.UniqueConstraint(
                fields=["vote", "person"],
                condition=models.Q(lot__isnull=True),
                name="ballot_one_per_person",
            ),
        ]

    def __str__(self) -> str:
        return f"ballot #{self.pk}"
