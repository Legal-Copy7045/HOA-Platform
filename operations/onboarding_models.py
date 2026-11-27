"""Vendor onboarding records (decision D79). Imported by operations/models.py.

A vendor is invited by the board or manager, fills in the public onboarding
form through a single emailed link, and becomes active only after two
different board members approve the submission. The vendor's uploads are
encrypted at the application layer (operations/vendor_crypto.py). Every
submission, decision, scan, requirement change and terms version is an
append-only record; the profile and the round in progress are the current
state, like an invoice's state.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel

__all__ = [
    "OPEN_ROUND_STATES",
    "VENDOR_EDITABLE_STATES",
    "Decision",
    "DisposalStep",
    "EntityType",
    "LinkEnd",
    "OnboardingDecision",
    "OnboardingLink",
    "OnboardingRound",
    "OnboardingSubmission",
    "ProfileStatus",
    "RoundMode",
    "RoundStatus",
    "VendorConflict",
    "VendorFile",
    "VendorFileDisposal",
    "VendorFileScan",
    "VendorProfile",
    "VendorRequirement",
    "VendorTerms",
    "VendorType",
]


class VendorType(models.TextChoices):
    CONTRACTOR = "contractor", "Contractor or trades (work on site)"
    HOME_IMPROVEMENT = "home_improvement", "Home improvement contractor"
    LANDSCAPING = "landscaping", "Landscaping, snow and grounds"
    PROFESSIONAL = "professional", "Professional services (accountant, engineer, reserve study)"
    ATTORNEY = "attorney", "Attorney or law firm"
    SUPPLIER = "supplier", "Supplier (goods only, no work on site)"
    UTILITY = "utility", "Utility or government body"
    OTHER = "other", "Other services"


class EntityType(models.TextChoices):
    INDIVIDUAL = "individual", "Individual or sole proprietor"
    SMLLC = "smllc", "Single-member LLC"
    PARTNERSHIP = "partnership", "Partnership"
    C_CORP = "c_corp", "C corporation"
    S_CORP = "s_corp", "S corporation"
    LLC_C = "llc_c", "LLC taxed as a C corporation"
    LLC_S = "llc_s", "LLC taxed as an S corporation"
    LLC_P = "llc_p", "LLC taxed as a partnership"
    TRUST = "trust", "Trust or estate"
    EXEMPT = "exempt", "Tax-exempt organization or government"
    OTHER = "other", "Other"


class ProfileStatus(models.TextChoices):
    INVITED = "invited", "Invited"
    SUBMITTED = "submitted", "Waiting for board review"
    RETURNED = "returned", "Returned to the vendor"
    DECLINED = "declined", "Declined"
    ACTIVE = "active", "Active"


class VendorProfile(models.Model):
    """The vendor's onboarding state and the facts its approved submission
    established (no tax identification number, ever)."""

    vendor = models.OneToOneField(
        "operations.Vendor", on_delete=models.PROTECT, related_name="onboarding_profile"
    )
    status = models.CharField(max_length=12, choices=ProfileStatus.choices)
    vendor_type = models.CharField(max_length=20, choices=VendorType.choices)
    entity_type = models.CharField(max_length=12, choices=EntityType.choices, blank=True)
    legal_name = models.CharField(max_length=200, blank=True)
    dba = models.CharField(max_length=200, blank=True)
    remit_address = models.TextField(blank=True)
    w9_received_on = models.DateField(null=True, blank=True)
    w9_revision = models.CharField(max_length=40, blank=True)
    w9_file = models.ForeignKey(
        "operations.VendorFile", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    coi_expires_on = models.DateField(null=True, blank=True)
    hic_registration = models.CharField(max_length=40, blank=True)
    licences = models.TextField(blank=True)
    conflict_disclosed = models.BooleanField(default=False)
    # Every answer of the approved submission, for the vendor file summary.
    approved_answers = models.JSONField(default=dict, blank=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    status_changed_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return f"onboarding profile #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Vendor onboarding profiles are kept.")


class RoundMode(models.TextChoices):
    FULL = "full", "Full onboarding"
    UPDATE = "update", "Update of some documents"


class RoundStatus(models.TextChoices):
    DRAFT = "draft", "Waiting for the vendor"
    SUBMITTED = "submitted", "Waiting for board review"
    RETURNED = "returned", "Returned to the vendor"
    DECLINED = "declined", "Declined"
    APPROVED = "approved", "Approved"
    CANCELLED = "cancelled", "Cancelled"


OPEN_ROUND_STATES = (RoundStatus.DRAFT, RoundStatus.SUBMITTED, RoundStatus.RETURNED)
VENDOR_EDITABLE_STATES = (RoundStatus.DRAFT, RoundStatus.RETURNED)


class OnboardingRound(models.Model):
    """One request for information from a vendor: the full onboarding, or an
    update of named items (an expired insurance certificate, a new W-9).
    ``answers`` is the vendor's draft; submissions snapshot it."""

    vendor = models.ForeignKey(
        "operations.Vendor", on_delete=models.PROTECT, related_name="onboarding_rounds"
    )
    mode = models.CharField(max_length=8, choices=RoundMode.choices, default=RoundMode.FULL)
    # Update mode: the items asked for (keys of vendor_onboarding.ITEMS).
    items = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=RoundStatus.choices, default=RoundStatus.DRAFT)
    answers = models.JSONField(default=dict, blank=True)
    return_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    status_changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"onboarding round #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Onboarding rounds are cancelled, not deleted.")


class LinkEnd(models.TextChoices):
    SUBMITTED = "submitted", "Used: the vendor submitted"
    REPLACED = "replaced", "Replaced by a newer link"
    CANCELLED = "cancelled", "Cancelled"
    EXPIRED = "expired", "Expired"


class OnboardingLink(models.Model):
    """The vendor's emailed link. Only a keyed hash of the token is stored;
    the token itself exists only in the email (minted by the worker)."""

    round = models.ForeignKey(OnboardingRound, on_delete=models.PROTECT, related_name="links")
    token_hash = models.CharField(max_length=64, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=10, choices=LinkEnd.choices, blank=True)
    opened_count = models.PositiveIntegerField(default=0)
    last_opened_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"onboarding link #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Onboarding links are ended, not deleted.")

    @property
    def is_open(self) -> bool:
        return self.ended_at is None and self.expires_at > timezone.now()


def _vendor_file_key(instance, filename):
    return f"vendor-files/{instance.storage_name}"


class VendorFile(AppendOnlyModel):
    """An uploaded vendor document, stored encrypted (D79). A newer upload of
    the same kind in the same round replaces it for the round; this row and
    its blob are kept until disposal."""

    round = models.ForeignKey(OnboardingRound, on_delete=models.PROTECT, related_name="files")
    kind = models.CharField(max_length=20)
    storage_name = models.CharField(max_length=32, unique=True, default="")
    file = models.FileField(upload_to=_vendor_file_key, max_length=200)
    original_filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100, default="application/pdf")
    size = models.PositiveBigIntegerField()  # of the plaintext
    sha256 = models.CharField(max_length=64)  # of the plaintext
    scan_result = models.CharField(max_length=16)
    scan_detail = models.CharField(max_length=20, blank=True, default="")
    encrypted = models.BooleanField(default=True)
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["uploaded_at", "id"]

    def __str__(self) -> str:
        return f"vendor file #{self.pk}"

    def save(self, *args, **kwargs):
        if not self.storage_name:
            self.storage_name = uuid.uuid4().hex
        super().save(*args, **kwargs)


class VendorFileScan(AppendOnlyModel):
    file = models.ForeignKey(VendorFile, on_delete=models.PROTECT, related_name="scans")
    result = models.CharField(max_length=16)
    scanner = models.CharField(max_length=50)
    detail = models.CharField(max_length=20, blank=True, default="")
    scanned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-scanned_at", "-id"]

    def __str__(self) -> str:
        return f"vendor file scan {self.result}"


class OnboardingSubmission(AppendOnlyModel):
    """What the vendor sent, frozen: every answer, every document version
    (ids and checksums), and the terms they accepted."""

    round = models.ForeignKey(OnboardingRound, on_delete=models.PROTECT, related_name="submissions")
    number = models.PositiveIntegerField()
    answers = models.JSONField(default=dict)
    files = models.JSONField(default=list)
    terms_version = models.PositiveIntegerField(default=0)
    terms_sha256 = models.CharField(max_length=64, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-submitted_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["round", "number"], name="vendor_submission_number"),
        ]

    def __str__(self) -> str:
        return f"vendor submission #{self.pk}"


class Decision(models.TextChoices):
    APPROVE = "approve", "Approved"
    RETURN = "return", "Returned to the vendor"
    DECLINE = "decline", "Declined"


class OnboardingDecision(AppendOnlyModel):
    submission = models.ForeignKey(
        OnboardingSubmission, on_delete=models.PROTECT, related_name="decisions"
    )
    decision = models.CharField(max_length=8, choices=Decision.choices)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "decided_by"],
                condition=models.Q(decision="approve"),
                name="vendor_approval_once_per_person",
            ),
        ]

    def __str__(self) -> str:
        return f"vendor decision #{self.pk}"


class VendorConflict(AppendOnlyModel):
    """A portal Person the board linked to a vendor's conflict-of-interest
    disclosure (D79, D65): that person may not approve the vendor or its
    invoices. ``removed`` rows end an earlier link; the newest row wins."""

    vendor = models.ForeignKey(
        "operations.Vendor", on_delete=models.PROTECT, related_name="conflict_links"
    )
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    removed = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"vendor conflict #{self.pk}"


class VendorRequirement(AppendOnlyModel):
    """The board's choice for one item and vendor type, with its citation.
    The newest row wins; without one the default table applies."""

    vendor_type = models.CharField(max_length=20, choices=VendorType.choices)
    item = models.CharField(max_length=20)
    required = models.BooleanField()
    citation = models.CharField(max_length=200)
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    set_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["set_at", "id"]

    def __str__(self) -> str:
        return f"{self.vendor_type}/{self.item}"


class VendorTerms(AppendOnlyModel):
    """The association's vendor terms; each change is a new version."""

    version = models.PositiveIntegerField(unique=True)
    text = models.TextField()
    citation = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-version"]

    def __str__(self) -> str:
        return f"vendor terms v{self.version}"


class DisposalStep(models.TextChoices):
    PROPOSE = "propose", "Proposed"
    CONFIRM = "confirm", "Confirmed and removed"


class VendorFileDisposal(AppendOnlyModel):
    """Two-person disposal of a vendor document past its retention: one board
    member proposes, a different one confirms; then the stored bytes are
    removed and this record stays."""

    file = models.ForeignKey(VendorFile, on_delete=models.PROTECT, related_name="disposals")
    step = models.CharField(max_length=8, choices=DisposalStep.choices)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    reason = models.CharField(max_length=200)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["file", "step"], name="vendor_file_disposal_step"),
        ]

    def __str__(self) -> str:
        return f"vendor file disposal #{self.pk}"
