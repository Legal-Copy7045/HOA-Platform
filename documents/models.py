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
    LOT = "lot", "About one home"
    OTHER = "other", "Other"


class Visibility(models.TextChoices):
    OWNERS = "owners", "All owners"
    BOARD = "board", "Board, manager and auditor only"
    LOT = "lot", "Owners of one home only"
    COMMITTEE = "committee", "One committee (and the board)"


class Folder(models.Model):
    """A board-managed folder. Its visibility applies to every document filed
    in it (owners, board only, or one committee). No public folders."""

    name = models.CharField(max_length=120)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="children"
    )
    visibility = models.CharField(
        max_length=10,
        choices=[(v, label) for v, label in Visibility.choices if v != "lot"],
        default=Visibility.OWNERS,
    )
    committee = models.ForeignKey(
        "governance.Committee", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(visibility="committee") & models.Q(committee__isnull=False))
                | (~models.Q(visibility="committee") & models.Q(committee__isnull=True)),
                name="folder_committee_matches_visibility",
            ),
            models.UniqueConstraint(fields=["parent", "name"], name="folder_unique_name"),
        ]

    def __str__(self) -> str:
        return self.name

    def delete(self, *args, **kwargs):
        raise PermissionError("Folders are kept; move documents instead.")


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
        max_length=10, choices=Visibility.choices, default=Visibility.OWNERS
    )
    lot = models.ForeignKey(
        "community.Lot", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.PROTECT, related_name="documents"
    )
    committee = models.ForeignKey(
        "governance.Committee", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
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
            ),
            models.CheckConstraint(
                condition=(models.Q(visibility="committee") & models.Q(committee__isnull=False))
                | (~models.Q(visibility="committee") & models.Q(committee__isnull=True)),
                name="document_committee_matches_visibility",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners: current owner-wide documents, documents for lots on their
        account, and documents of committees they sit on."""
        visible = (
            models.Q(visibility=Visibility.OWNERS)
            | models.Q(visibility=Visibility.LOT, lot_id__in=roles.account_lot_ids)
            | models.Q(visibility=Visibility.COMMITTEE, committee_id__in=roles.committee_ids)
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
    # Why the scan gave no answer (documents.scanning: unreachable, timeout,
    # bad_reply, not_configured); empty for a clean or infected result (D57).
    detail = models.CharField(max_length=20, blank=True, default="", db_default="")

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
    # Why the first scan gave no answer (see VersionScan.detail).
    scan_detail = models.CharField(max_length=20, blank=True, default="", db_default="")
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
    detail = models.CharField(max_length=20, blank=True, default="", db_default="")

    class Meta:
        ordering = ["-scanned_at", "-id"]

    def __str__(self) -> str:
        return f"scan {self.result}"
