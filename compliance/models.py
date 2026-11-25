"""Covenant violations: report, notice, owner response, hearing, outcome.

No fines are assessed here (decision: tracking only until PA law is reviewed).
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet


class ViolationCategory(models.TextChoices):
    LANDSCAPING = "landscaping", "Lawn and landscaping"
    TRASH = "trash", "Trash and recycling"
    PARKING = "parking", "Parking and vehicles"
    STRUCTURE = "structure", "Structures and exterior"
    UNAPPROVED = "unapproved", "Unapproved architectural change"
    PETS = "pets", "Pets"
    NUISANCE = "nuisance", "Noise or nuisance"
    OTHER = "other", "Other"


class ViolationState(models.TextChoices):
    OPEN = "open", "Reported"
    NOTICED = "noticed", "Notice sent"
    RESPONDED = "responded", "Owner responded"
    HEARING = "hearing", "Hearing scheduled"
    CURED = "cured", "Cured"
    UPHELD = "upheld", "Upheld"
    DISMISSED = "dismissed", "Dismissed"
    CLOSED = "closed", "Closed"


class Violation(ScopedModel):
    lot = models.ForeignKey("community.Lot", on_delete=models.PROTECT, related_name="+")
    category = models.CharField(max_length=16, choices=ViolationCategory.choices)
    description = models.TextField()
    observed_on = models.DateField(default=timezone.localdate)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(
        max_length=16, choices=ViolationState.choices, default=ViolationState.OPEN
    )
    state_changed_at = models.DateTimeField(default=timezone.now)
    noticed_at = models.DateTimeField(null=True, blank=True)
    cure_by = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = "lot"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners see a violation once notice is sent (review A7), and only
        those from their own time owning the lot, plus any still unresolved
        (the new owner inherits the condition to cure) (review A4)."""
        from core.scoping import owner_era_q

        unresolved = models.Q(
            lot_id__in=roles.account_lot_ids,
            state__in=[ViolationState.NOTICED, ViolationState.RESPONDED, ViolationState.HEARING],
        )
        # Only violations a notice was actually sent for (one dismissed
        # before notice stays internal).
        return queryset.filter(noticed_at__isnull=False).filter(owner_era_q(roles) | unresolved)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Violation #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Violations are dismissed or closed, never deleted.")


class OwnerResponse(AppendOnlyModel):
    violation = models.ForeignKey(Violation, on_delete=models.PROTECT, related_name="responses")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"response #{self.pk}"
