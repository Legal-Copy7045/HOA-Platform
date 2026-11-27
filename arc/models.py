"""Architectural review requests (CLAUDE.md "ARC workflow")."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet


class ArcState(models.TextChoices):
    DRAFT = "draft", "Draft"
    NEIGHBORS = "neighbors", "Awaiting neighbor acknowledgements"
    SUBMITTED = "submitted", "Submitted (completeness check)"
    IN_REVIEW = "in_review", "In review"
    APPROVED = "approved", "Approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions", "Approved with conditions"
    DENIED = "denied", "Denied"
    APPEAL = "appeal", "Under appeal"
    APPEAL_DECIDED = "appeal_decided", "Appeal decided"
    COMPLETED = "completed", "Completed"
    WITHDRAWN = "withdrawn", "Withdrawn"


DECIDED = {ArcState.APPROVED, ArcState.APPROVED_WITH_CONDITIONS, ArcState.DENIED}


class ArcRequestQuerySet(ScopedQuerySet):
    def for_user(self, user):
        from core import policy

        if policy.can(user, "arc.view_all"):
            clone = self._clone()
            clone._scope_ok = True
            return clone
        return super().for_user(user)


class ArcRequest(ScopedModel):
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    applicant = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    title = models.CharField(max_length=200)
    description = models.TextField()
    project_type = models.CharField(max_length=60, blank=True)
    planned_start = models.DateField(null=True, blank=True)
    # From the association's alteration request form (Milestone 4).
    planned_completion = models.DateField(null=True, blank=True)
    location = models.CharField(
        max_length=8,
        blank=True,
        choices=[("front", "Front"), ("side", "Side"), ("rear", "Rear"), ("other", "Other")],
        help_text="Where the alteration is, with respect to the nearest unit.",
    )
    location_detail = models.CharField(max_length=200, blank=True)
    materials = models.TextField(
        blank=True, help_text="Materials (or shrubs) installed or removed, quantities, contractors."
    )
    affected_lots = models.ManyToManyField(
        "community.Lot",
        blank=True,
        related_name="+",
        help_text="Neighboring lots affected (recusal).",
    )
    state = models.CharField(max_length=32, choices=ArcState.choices, default=ArcState.DRAFT)
    state_changed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    submitted_at = models.DateTimeField(null=True, blank=True)
    complete_at = models.DateTimeField(null=True, blank=True)
    review_due_on = models.DateField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    appeal_due_on = models.DateField(null=True, blank=True)

    objects = ArcRequestQuerySet.as_manager()
    scope_lot_path = "lot"
    # Own matters (D65): the applicant, and the recusal rule below.
    conflict_person_fields = ("applicant",)
    conflict_noun = "architectural request"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        from core.scoping import owner_era_q

        # The applicant keeps their own request only while they still own the lot.
        mine = (
            models.Q(applicant_id=roles.person_id, lot_id__in=roles.owned_lot_ids)
            if roles.person_id
            else models.Q(pk__in=[])
        )
        return queryset.filter(owner_era_q(roles) | mine)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"ARC #{self.pk}: {self.title}"

    def conflict_of_interest(self, user) -> bool:
        """Own matters (D65) include the ARC recusal rule: an owner of an
        affected or neighboring home takes no staff step on it either."""
        from arc.services import is_recused

        return is_recused(user, self)

    def conflict_message(self, user) -> str:
        """The recusal wording ARC has always used, saying why."""
        from core import conflicts

        if conflicts.own_matter(user, self, hooks=False):
            return (
                "You are recused from this request: it is your own home's, so another board"
                " member handles it."
            )
        return "You are recused from this request: your home is affected by it."

    def delete(self, *args, **kwargs):
        raise PermissionError("ARC requests are kept; withdraw instead.")


class ArcRequestRevision(AppendOnlyModel):
    """What a draft said before its owner edited it. Every edit keeps the
    earlier wording, so a request returned as incomplete still shows what was
    first submitted. Never changed."""

    request = models.ForeignKey(ArcRequest, on_delete=models.PROTECT, related_name="revisions")
    title = models.CharField(max_length=200)
    description = models.TextField()
    project_type = models.CharField(max_length=60, blank=True)
    planned_start = models.DateField(null=True, blank=True)
    planned_completion = models.DateField(null=True, blank=True)
    location = models.CharField(max_length=8, blank=True)
    location_detail = models.CharField(max_length=200, blank=True)
    materials = models.TextField(blank=True)
    was_submitted = models.BooleanField(
        default=False, help_text="This wording had been submitted to the committee."
    )
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"ARC revision #{self.pk}"


class Position(models.TextChoices):
    APPROVE = "approve", "Approve"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions", "Approve with conditions"
    DENY = "deny", "Deny"


class ReviewerVote(AppendOnlyModel):
    """A reviewer's position (deliberation). Hidden from the applicant and from
    recused reviewers. A changed mind is a new row; the latest counts."""

    request = models.ForeignKey(ArcRequest, on_delete=models.PROTECT, related_name="+")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    position = models.CharField(max_length=32, choices=Position.choices)
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"vote #{self.pk}"


class Decision(AppendOnlyModel):
    """The ARC (or, on appeal, the board) decision. Never changed."""

    request = models.ForeignKey(ArcRequest, on_delete=models.PROTECT, related_name="decisions")
    outcome = models.CharField(max_length=32, choices=ArcState.choices)
    conditions = models.TextField(blank=True)
    reasons = models.TextField(blank=True)
    citation = models.CharField(max_length=200, blank=True)
    is_appeal = models.BooleanField(default=False)
    deemed = models.BooleanField(default=False, help_text="Approved because the clock ran out.")
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["decided_at", "id"]

    def __str__(self) -> str:
        return f"decision #{self.pk}"


class ArcProjectType(models.Model):
    """A kind of project owners choose when they apply (fence, shed, paint...).
    Board-configured; what each type needs comes from the Declaration or the
    ARC guidelines, cited here."""

    name = models.CharField(max_length=60, unique=True)
    guidance = models.TextField(
        blank=True, help_text="What to include: drawings, materials, colors, a plot plan..."
    )
    citation = models.CharField(max_length=200, blank=True)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError("Project types are retired, not deleted (requests refer to them).")


# --- Neighbor sign-off (Milestone 3 phase 7) ------------------------------------------


class NeighborSide(models.TextChoices):
    LEFT = "left", "Left"
    RIGHT = "right", "Right"
    OTHER = "other", "Also affected"


class LotNeighbor(models.Model):
    """For each lot, the board records its left and right neighbors (from the
    development plan) and any other lots a project there affects. They are
    asked to sign off on that lot's ARC requests, and are treated as affected
    lots (their owners are recused from reviewing). Defined per lot: a corner
    lot's neighbors need not mirror."""

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    neighbor = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    side = models.CharField(max_length=8, choices=NeighborSide.choices, default=NeighborSide.OTHER)

    class Meta:
        ordering = ["lot", "side", "neighbor"]
        constraints = [
            models.UniqueConstraint(fields=["lot", "neighbor"], name="lot_neighbor_once"),
            models.CheckConstraint(
                condition=~models.Q(lot=models.F("neighbor")), name="lot_not_own_neighbor"
            ),
            models.UniqueConstraint(
                fields=["lot", "side"],
                condition=models.Q(side__in=["left", "right"]),
                name="lot_one_left_one_right",
            ),
        ]

    def __str__(self) -> str:
        return f"neighbor #{self.pk}"


class SignoffState(models.TextChoices):
    PENDING = "pending", "Waiting"
    APPROVED = "approved", "Acknowledged, no objection"
    OBJECTED = "objected", "Acknowledged, objects"
    WITHDRAWN = "withdrawn", "No longer asked"


# Request states in which a neighbor can still view and answer a waiting request.
NEIGHBOR_VISIBLE_STATES = (
    ArcState.NEIGHBORS,
    ArcState.SUBMITTED,
    ArcState.IN_REVIEW,
    ArcState.APPEAL,
)


class SignoffRequest(ScopedModel):
    """One neighbor (a lot, or an extra address) asked to sign off on an ARC
    request. Their answer is an append-only SignoffResponse; ``state`` mirrors
    the latest one for listing. A neighbor without a portal login gets a
    single-use, expiring emailed link (only its hash is stored)."""

    arc_request = models.ForeignKey(ArcRequest, on_delete=models.PROTECT, related_name="signoffs")
    lot = models.ForeignKey(
        "community.Lot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Extra addresses (not a lot in the community, or a specific person).
    name = models.CharField(max_length=200, blank=True)
    address = models.CharField(max_length=300, blank=True)
    email = models.EmailField(blank=True)
    state = models.CharField(
        max_length=12, choices=SignoffState.choices, default=SignoffState.PENDING
    )
    token_hash = models.CharField(max_length=64, blank=True)
    token_expires_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    # From the lot's neighbor list, or added by the board: the homeowner
    # cannot remove it; the board can.
    locked = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = "lot"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners see the acknowledgements asked of their own lot (their
        neighbors' projects) once they were actually asked: never a draft, and
        only while the request is still open (BUG-002, BUG-008, SEC-001), and
        only those sent since they own the lot: a buyer never sees what the
        seller was asked or answered (SEC-001); and those on their own ARC
        requests."""
        asked = models.Q(pk__in=[])
        for lot_id, since in roles.owned_since:
            if lot_id in roles.owned_lot_ids:
                asked |= models.Q(lot_id=lot_id, sent_at__date__gte=since)
        still_open = models.Q(arc_request__state__in=NEIGHBOR_VISIBLE_STATES)
        return queryset.filter(
            (asked & still_open)
            | models.Q(
                arc_request__applicant_id=roles.person_id,
                arc_request__lot_id__in=roles.owned_lot_ids,
            )
        )

    class Meta:
        ordering = ["arc_request", "lot__number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["arc_request", "lot"],
                condition=models.Q(lot__isnull=False),
                name="signoff_once_per_lot",
            ),
            models.CheckConstraint(
                condition=models.Q(lot__isnull=False) | ~models.Q(address=""),
                name="signoff_lot_or_address",
            ),
        ]

    def __str__(self) -> str:
        return f"sign-off #{self.pk}"

    @property
    def label(self) -> str:
        names = getattr(self, "owner_names", "")
        if self.lot_id:
            who = f"{names}, " if names else ""
            return f"{who}{self.lot.label}"
        return f"{self.name}, {self.address}" if self.name else self.address


class SignoffChannel(models.TextChoices):
    PORTAL = "portal", "In the portal"
    LINK = "link", "By emailed review link"
    PAPER = "paper", "Signed paper form (entered by staff)"
    AI = "ai", "Signed paper form (read automatically)"


class SignoffResponse(AppendOnlyModel):
    """A neighbor's answer. Never changed; a change of mind is a new row."""

    signoff = models.ForeignKey(SignoffRequest, on_delete=models.PROTECT, related_name="responses")
    position = models.CharField(
        max_length=12,
        choices=[
            (SignoffState.APPROVED, "Acknowledged, no objection"),
            (SignoffState.OBJECTED, "Acknowledged, objects"),
        ],
    )
    signer_name = models.CharField(max_length=200)
    comment = models.TextField(blank=True)
    signed_on = models.DateField(null=True, blank=True)
    channel = models.CharField(max_length=8, choices=SignoffChannel.choices)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # The scanned form this came from, if any (an attachment on the ARC request).
    evidence_attachment_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"sign-off response #{self.pk}"


class ScanReading(models.Model):
    """One automatic reading of an uploaded signed form. Its proposals wait
    for a person to confirm, unless the board switched on automatic confirming."""

    arc_request = models.ForeignKey(ArcRequest, on_delete=models.PROTECT, related_name="+")
    attachment_id = models.PositiveBigIntegerField()
    state = models.CharField(max_length=12, default="queued")  # queued, done, failed
    error = models.CharField(max_length=200, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"scan reading #{self.pk}"


class SignoffProposal(models.Model):
    """What the reader thinks one signature says. Never trusted on its own."""

    reading = models.ForeignKey(ScanReading, on_delete=models.PROTECT, related_name="proposals")
    signoff = models.ForeignKey(
        SignoffRequest, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    signer_name = models.CharField(max_length=200, blank=True)
    position = models.CharField(max_length=12, blank=True)
    signed_on = models.DateField(null=True, blank=True)
    confidence = models.PositiveSmallIntegerField(default=0)  # 0-100
    state = models.CharField(max_length=12, default="pending")  # pending, confirmed, rejected
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    def __str__(self) -> str:
        return f"proposal #{self.pk}"
