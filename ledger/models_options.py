"""Records of the finance options (D80-D85) that belong to the ledger app.

Collections legal tracking (D82): every step of a collection case once it is
with counsel (referral, demand letter, lien, lawsuit, hearing, judgment,
costs, money recovered, release of the lien) is one append-only
``LegalEvent`` on the case. Corrections are new events that say so. Legal
matters are board-only records: owners never see these rows (they see only
the charges on their account and the letters sent to them).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import ScopedAppendOnlyModel


class LegalKind(models.TextChoices):
    REFERRAL = "referral", "Referred to an attorney"
    DEMAND = "demand", "Demand letter sent by counsel"
    LIEN = "lien", "Lien recorded"
    LAWSUIT = "lawsuit", "Lawsuit filed"
    HEARING = "hearing", "Hearing scheduled"
    JUDGMENT = "judgment", "Judgment entered"
    COST = "cost", "Cost or attorney fee incurred"
    COST_POSTED = "cost_posted", "Cost charged to the owner's account"
    RECOVERY = "recovery", "Payment recovered"
    RELEASE = "release", "Lien satisfied or released"
    NOTE = "note", "Note or correction"


class LienFiling(models.TextChoices):
    # 68 Pa.C.S. § 5315: the lien arises without any filing; what the
    # association records is what, if anything, was filed to perfect or
    # enforce it.
    NONE = "none", "Nothing filed (statutory lien only)"
    STATEMENT = "statement", "Notice or statement of lien filed"
    JUDGMENT = "judgment", "Judgment entered (lien on the record)"


class Court(models.TextChoices):
    MAGISTERIAL = "magisterial", "Magisterial district court"
    COMMON_PLEAS = "common_pleas", "Court of common pleas"
    OTHER = "other", "Other"


class LegalEvent(ScopedAppendOnlyModel):
    scope_lot_path = "case__lot"
    conflict_noun = "collection case"  # own matters (D65)

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        # Legal matters are board-only records (D82).
        return queryset.none()

    case = models.ForeignKey(
        "ledger.CollectionCase", on_delete=models.PROTECT, related_name="legal_events"
    )
    kind = models.CharField(max_length=12, choices=LegalKind.choices)
    occurred_on = models.DateField()
    firm = models.CharField(max_length=200, blank=True)
    attorney = models.CharField(max_length=120, blank=True)
    reference = models.CharField(max_length=80, blank=True, help_text="Counsel's or docket number")
    fee_arrangement = models.CharField(max_length=200, blank=True)
    court = models.CharField(max_length=14, choices=Court.choices, blank=True)
    lien_filing = models.CharField(max_length=10, choices=LienFiling.choices, blank=True)
    amount_cents = models.BigIntegerField(null=True, blank=True)
    # Costs: whether the Declaration lets the association charge it to the owner.
    chargeable = models.BooleanField(null=True, blank=True)
    # A date to come back to (hearing date): a My item reminds the board.
    due_on = models.DateField(null=True, blank=True)
    detail = models.TextField(blank=True)
    # COST_POSTED: the cost event charged, and the ledger entry it created.
    cost = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="posted_as"
    )
    ledger_entry = models.OneToOneField(
        "ledger.LedgerEntry", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["occurred_on", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_cents__isnull=True) | models.Q(amount_cents__gte=0),
                name="legal_event_amount_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"Legal event #{self.pk}"
