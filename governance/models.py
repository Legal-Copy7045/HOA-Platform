"""Meetings, the resolution/policy register, records requests and votes."""

import uuid

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
    """One cast ballot. Never changed. Formal: one per lot; informal: one per person.

    On a secret vote the ballot records only that this person/lot voted
    (``option`` is empty); the choice is a separate, unlinkable
    ``SecretChoice`` row (decision D21)."""

    vote = models.ForeignKey(Vote, on_delete=models.PROTECT, related_name="ballots")
    option = models.ForeignKey(
        VoteOption, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
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


class SecretChoice(AppendOnlyModel):
    """A secret ballot's choice, stored apart from who cast it: no voter, no
    lot, no timestamp, and a random id so the insertion order of rows cannot
    be read from their ids. Counted for results; never shown per voter."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vote = models.ForeignKey(Vote, on_delete=models.PROTECT, related_name="+")
    option = models.ForeignKey(VoteOption, on_delete=models.PROTECT, related_name="+")

    def __str__(self) -> str:
        return "secret choice"


# --- Committees (Milestone 3) -----------------------------------------------------------

from community.models import NeverDeleteMixin  # noqa: E402


class Committee(models.Model):
    """A board-created committee (social, landscaping, ARC...) with a space of
    its own: discussion topics, a document folder, tasks and events."""

    name = models.CharField(max_length=120, unique=True)
    purpose = models.TextField(blank=True)
    # The board resolution or Bylaws section that created it.
    citation = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError("Committees are retired, not deleted.")


class CommitteeRole(models.TextChoices):
    CHAIR = "chair", "Chair"
    MEMBER = "member", "Member"


class CommitteeMembershipQuerySet(ScopedQuerySet):
    def active(self, on=None):
        on = on or timezone.localdate()
        return self.filter(start_date__lte=on).filter(
            models.Q(end_date__isnull=True) | models.Q(end_date__gt=on)
        )


class CommitteeMembership(NeverDeleteMixin, ScopedModel):
    committee = models.ForeignKey(Committee, on_delete=models.PROTECT, related_name="memberships")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    role = models.CharField(
        max_length=8, choices=CommitteeRole.choices, default=CommitteeRole.MEMBER
    )
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    appointed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    objects = CommitteeMembershipQuerySet.as_manager()
    scope_person_path = "person"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Your own memberships and the members of your committees."""
        mine = models.Q(person_id=roles.person_id) if roles.person_id else models.Q(pk__in=[])
        return queryset.filter(mine | models.Q(committee_id__in=roles.committee_ids))

    class Meta:
        ordering = ["committee", "role", "start_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["committee", "person"],
                condition=models.Q(end_date__isnull=True),
                name="committee_one_open_membership",
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="committee_membership_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"membership #{self.pk}"


class CommitteeTopic(models.Model):
    """A discussion in a committee's space; its messages are a thread."""

    committee = models.ForeignKey(Committee, on_delete=models.PROTECT, related_name="topics")
    title = models.CharField(max_length=200)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    closed = models.BooleanField(default=False)

    class Meta:
        ordering = ["closed", "-created_at"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Topics are closed, not deleted.")


# --- Elections, proxies and written consent (Milestone 4) -------------------------------


class ElectionState(models.TextChoices):
    NOMINATIONS = "nominations", "Taking nominations"
    VOTING = "voting", "Voting open"
    CLOSED = "closed", "Voting closed"
    CERTIFIED = "certified", "Results certified"


class Election(models.Model):
    """Electing owners to the board, usually at the annual meeting. One ballot
    per lot, choosing up to ``seats`` candidates. Ballots are stored like
    secret votes: who voted apart from the choices (decision D21)."""

    title = models.CharField(max_length=200)
    meeting = models.ForeignKey(
        Meeting, null=True, blank=True, on_delete=models.PROTECT, related_name="elections"
    )
    seats = models.PositiveSmallIntegerField()
    term_years = models.PositiveSmallIntegerField(default=2)
    citation = models.CharField(max_length=200, blank=True)  # e.g. Bylaws §3.2
    state = models.CharField(
        max_length=12, choices=ElectionState.choices, default=ElectionState.NOMINATIONS
    )
    eligible_lots = models.PositiveSmallIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    certified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    certified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Elections are kept.")


class Candidate(models.Model):
    election = models.ForeignKey(Election, on_delete=models.PROTECT, related_name="candidates")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    statement = models.TextField(blank=True)
    nominated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    accepted = models.BooleanField(default=False)  # the nominee agreed to stand
    withdrawn = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["person__display_name"]
        constraints = [
            models.UniqueConstraint(fields=["election", "person"], name="candidate_once")
        ]

    def __str__(self) -> str:
        return f"candidate #{self.pk}"


class ElectionParticipation(AppendOnlyModel):
    """That a lot voted (and who cast it, in person or by proxy). No choices."""

    election = models.ForeignKey(Election, on_delete=models.PROTECT, related_name="+")
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    cast_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    by_proxy = models.BooleanField(default=False)
    cast_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["election", "lot"], name="election_one_ballot_per_lot")
        ]

    def __str__(self) -> str:
        return f"participation #{self.pk}"


class ElectionChoice(AppendOnlyModel):
    """One selected candidate on some ballot. No voter, lot, time or sequence."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    election = models.ForeignKey(Election, on_delete=models.PROTECT, related_name="+")
    candidate = models.ForeignKey(Candidate, on_delete=models.PROTECT, related_name="+")

    def __str__(self) -> str:
        return "election choice"


class ElectionResult(AppendOnlyModel):
    """The certified count for one candidate. Never changed."""

    election = models.ForeignKey(Election, on_delete=models.PROTECT, related_name="results")
    candidate = models.ForeignKey(Candidate, on_delete=models.PROTECT, related_name="+")
    votes = models.PositiveIntegerField()
    elected = models.BooleanField()
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-votes", "candidate__person__display_name"]

    def __str__(self) -> str:
        return f"result #{self.pk}"


class Proxy(ScopedModel):
    """An owner appoints someone to vote for their lot at a meeting (PA allows
    proxies; the Bylaws may set conditions). Revocable until it is used."""

    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="proxies")
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    grantor = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    holder = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = "lot"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Proxies for my lots, and proxies naming me as holder."""
        mine = models.Q(lot_id__in=roles.owned_lot_ids)
        if roles.person_id:
            mine |= models.Q(holder_id=roles.person_id)
        return queryset.filter(mine)

    class Meta:
        ordering = ["meeting", "lot__number"]
        constraints = [
            models.UniqueConstraint(
                fields=["meeting", "lot"],
                condition=models.Q(revoked_at__isnull=True),
                name="proxy_one_live_per_lot_meeting",
            )
        ]

    def __str__(self) -> str:
        return f"proxy #{self.pk}"


class ConsentState(models.TextChoices):
    OPEN = "open", "Waiting for signatures"
    ADOPTED = "adopted", "Adopted"
    FAILED = "failed", "Not adopted"
    WITHDRAWN = "withdrawn", "Withdrawn"


class ConsentRequest(models.Model):
    """A board resolution by unanimous written consent, without a meeting.
    Every board member serving when it is proposed must approve."""

    title = models.CharField(max_length=200)
    text = models.TextField()
    citation = models.CharField(max_length=200, blank=True)
    signers = models.ManyToManyField("community.Person", related_name="+")
    state = models.CharField(max_length=12, choices=ConsentState.choices, default=ConsentState.OPEN)
    resolution = models.OneToOneField(
        Resolution, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Consent requests are kept.")


class ConsentSignature(AppendOnlyModel):
    consent = models.ForeignKey(ConsentRequest, on_delete=models.PROTECT, related_name="signatures")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approve = models.BooleanField()
    signed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["consent", "person"], name="consent_sign_once")
        ]

    def __str__(self) -> str:
        return f"signature #{self.pk}"
