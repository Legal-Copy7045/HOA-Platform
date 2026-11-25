"""Architectural review requests (CLAUDE.md "ARC workflow")."""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet


class ArcState(models.TextChoices):
    DRAFT = "draft", "Draft"
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
    affected_lots = models.ManyToManyField(
        "community.Lot",
        blank=True,
        related_name="+",
        help_text="Neighbouring lots affected (recusal).",
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

    def delete(self, *args, **kwargs):
        raise PermissionError("ARC requests are kept; withdraw instead.")


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
        blank=True, help_text="What to include: drawings, materials, colours, a plot plan..."
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


# --- Neighbour sign-off (Milestone 3 phase 7) ------------------------------------------


class LotNeighbour(models.Model):
    """Adjacency from the development plan, recorded once by the board. Stored
    both ways (a pair of rows) so lookups are simple."""

    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    neighbour = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lot", "neighbour"], name="lot_neighbour_once"),
            models.CheckConstraint(
                condition=~models.Q(lot=models.F("neighbour")), name="lot_not_own_neighbour"
            ),
        ]

    def __str__(self) -> str:
        return f"neighbour #{self.pk}"


class SignoffState(models.TextChoices):
    PENDING = "pending", "Waiting"
    APPROVED = "approved", "Approves"
    OBJECTED = "objected", "Objects"
    WITHDRAWN = "withdrawn", "No longer asked"


class SignoffRequest(ScopedModel):
    """One neighbour (a lot, or an extra address) asked to sign off on an ARC
    request. Their answer is an append-only SignoffResponse; ``state`` mirrors
    the latest one for listing. A neighbour without a portal login gets a
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
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = "lot"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners see the sign-offs asked of their own lot (their neighbours'
        projects), and those on their own ARC requests."""
        return queryset.filter(
            models.Q(lot_id__in=roles.owned_lot_ids)
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
        if self.lot_id:
            return f"Lot {self.lot.number}, {self.lot.street_address}"
        return f"{self.name}, {self.address}" if self.name else self.address


class SignoffChannel(models.TextChoices):
    PORTAL = "portal", "Signed in the portal"
    LINK = "link", "Signed by emailed link"
    PAPER = "paper", "Signed paper form (entered by staff)"
    AI = "ai", "Signed paper form (read automatically)"


class SignoffResponse(AppendOnlyModel):
    """A neighbour's answer. Never changed; a change of mind is a new row."""

    signoff = models.ForeignKey(SignoffRequest, on_delete=models.PROTECT, related_name="responses")
    position = models.CharField(
        max_length=12,
        choices=[(SignoffState.APPROVED, "Approves"), (SignoffState.OBJECTED, "Objects")],
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
