"""Document library (CLAUDE.md "File uploads").

A ``Document`` is the catalogue entry (title, category, who may see it).
Each upload is a ``DocumentVersion``: append-only, stored under a random key,
never overwritten. The document points at its current version; owners see
only that, the board sees every version. Malware-scan results are their own
append-only rows.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.append_only import AppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet


class Category(models.TextChoices):
    GOVERNING = "governing", "Governing documents"
    POLICIES = "policies", "Rules and policies"
    MINUTES = "minutes", "Meeting minutes"
    FINANCIAL = "financial", "Financial"
    FORMS = "forms", "Forms"
    ARC = "arc", "Architectural guidelines"
    LOT = "lot", "Lot-specific"
    OTHER = "other", "Other"


class Visibility(models.TextChoices):
    OWNERS = "owners", "All owners"
    BOARD = "board", "Board, manager and auditor only"
    LOT = "lot", "Owners of one lot only"


class DocumentQuerySet(ScopedQuerySet):
    def for_user(self, user):
        from core import policy

        if not policy.can(user, "document.view_owner"):
            return self._clone().unscoped("no access").none()
        return super().for_user(user)


class Document(ScopedModel):
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=16, choices=Category.choices)
    visibility = models.CharField(
        max_length=8, choices=Visibility.choices, default=Visibility.OWNERS
    )
    lot = models.ForeignKey(
        "community.Lot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    description = models.TextField(blank=True)
    current_version = models.ForeignKey(
        "DocumentVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = DocumentQuerySet.as_manager()

    class Meta:
        ordering = ["category", "title"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(visibility="lot") & models.Q(lot__isnull=False))
                | (~models.Q(visibility="lot") & models.Q(lot__isnull=True)),
                name="document_lot_matches_visibility",
            )
        ]

    def __str__(self) -> str:
        return self.title

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners: current owner-wide documents, plus documents for lots they own."""
        visible = models.Q(visibility=Visibility.OWNERS) | models.Q(
            visibility=Visibility.LOT, lot_id__in=roles.account_lot_ids
        )
        return queryset.filter(visible, archived=False, current_version__isnull=False)

    def delete(self, *args, **kwargs):
        raise PermissionError("Documents are archived, never deleted.")


def _storage_key(instance, filename):
    return f"documents/{uuid.uuid4().hex}"


class DocumentVersion(AppendOnlyModel):
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="versions")
    number = models.PositiveIntegerField()
    file = models.FileField(upload_to=_storage_key, max_length=200)
    original_filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(default=timezone.now)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "number"], name="document_version_number_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.document_id} v{self.number}"


class ScanResult(models.TextChoices):
    CLEAN = "clean", "Clean"
    INFECTED = "infected", "Infected"
    UNSCANNED = "unscanned", "Not scanned (no scanner configured)"
    ERROR = "error", "Scanner error"


class VersionScan(AppendOnlyModel):
    version = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, related_name="scans")
    result = models.CharField(max_length=16, choices=ScanResult.choices)
    scanner = models.CharField(max_length=50)
    scanned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-scanned_at", "-id"]

    def __str__(self) -> str:
        return f"scan {self.result}"


def _attachment_key(instance, filename):
    return f"attachments/{uuid.uuid4().hex}"


class Attachment(AppendOnlyModel):
    """A file attached to a workflow object (ARC plans, violation photos,
    invoices). Never changed or removed: every submitted version is kept.
    Access is always decided by the owning object's scoped lookup."""

    owner_type = models.CharField(max_length=50)
    owner_id = models.CharField(max_length=32)
    file = models.FileField(upload_to=_attachment_key, max_length=200)
    original_filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    size = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    scan_result = models.CharField(max_length=16, choices=ScanResult.choices)
    label = models.CharField(max_length=200, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["uploaded_at", "id"]
        indexes = [models.Index(fields=["owner_type", "owner_id"])]

    def __str__(self) -> str:
        return self.original_filename


class AttachmentScan(AppendOnlyModel):
    """A later scan of an attachment (rescan after signatures update). The
    newest row wins over ``Attachment.scan_result``; nothing is ever edited."""

    attachment = models.ForeignKey(Attachment, on_delete=models.PROTECT, related_name="scans")
    result = models.CharField(max_length=16, choices=ScanResult.choices)
    scanner = models.CharField(max_length=50)
    scanned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-scanned_at", "-id"]

    def __str__(self) -> str:
        return f"scan {self.result}"
