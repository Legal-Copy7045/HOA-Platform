"""Scan every stored file again and record the result (security review C2).

    python manage.py rescan_files --actor you@example.org --reason "signatures updated"

Run after enabling a scanner or after its signatures update. Each result is a
new append-only scan record; the newest one decides whether the file can be
downloaded, so a file found infected later is blocked immediately. Audited as
a break-glass command (shown on the board's sysadmin activity page).
"""

from core.breakglass import BreakGlassCommand
from core.scoping import system_access
from documents.models import Attachment, AttachmentScan, DocumentVersion, ScanResult, VersionScan
from documents.scanning import scan


class Command(BreakGlassCommand):
    help = "Re-scan all documents and attachments with the configured scanner."
    command_name = "rescan_files"

    def handle(self, *args, **opts):
        actor = self.resolve_actor(opts)
        counts = dict.fromkeys(ScanResult.values, 0)
        with system_access("rescan files"):
            versions = list(DocumentVersion.objects.all())
            attachments = list(Attachment.objects.all())
        for version in versions:
            with version.file.open("rb") as fh:
                result, scanner = scan(fh)
            VersionScan.objects.create(version=version, result=result, scanner=scanner)
            counts[result] += 1
        for attachment in attachments:
            with attachment.file.open("rb") as fh:
                result, scanner = scan(fh)
            AttachmentScan.objects.create(attachment=attachment, result=result, scanner=scanner)
            counts[result] += 1
        self.audit(actor, opts, target_id=f"{len(versions)}+{len(attachments)}")
        self.stdout.write(", ".join(f"{k}: {v}" for k, v in counts.items()))
        if counts[ScanResult.INFECTED]:
            self.stdout.write("Infected files are now blocked from download.")
