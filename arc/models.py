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

        mine = models.Q(applicant_id=roles.person_id) if roles.person_id else models.Q(pk__in=[])
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
