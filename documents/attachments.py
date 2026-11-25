"""Attach validated, scanned files to any object; serve them only after the
caller has fetched the owning object through its scoped manager."""

from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404

from audit.services import record_event
from documents.models import Attachment, ScanResult
from documents.scanning import scan
from documents.services import DocumentError, scan_allows_download
from documents.validation import UploadRejected, check_upload, download_filename, safe_filename

IMAGES_AND_PDF = {"pdf", "png", "jpg", "jpeg"}


def _owner_key(obj) -> tuple[str, str]:
    return obj._meta.label_lower, str(obj.pk)


def attach(
    obj, uploaded, *, actor, label: str = "", allowed: set[str] | None = None, request=None
) -> Attachment:
    try:
        kind = check_upload(uploaded, allowed=allowed or IMAGES_AND_PDF)
    except UploadRejected as exc:
        raise DocumentError(str(exc)) from exc
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    result, _ = scan(uploaded)
    if result == ScanResult.INFECTED:
        raise DocumentError("The file was flagged by the malware scanner and was not stored.")
    owner_type, owner_id = _owner_key(obj)
    attachment = Attachment(
        owner_type=owner_type,
        owner_id=owner_id,
        original_filename=safe_filename(uploaded.name, kind.ext),
        content_type=kind.content_type,
        size=uploaded.size,
        sha256=digest.hexdigest(),
        scan_result=result,
        label=label[:200],
        uploaded_by=actor,
    )
    with transaction.atomic():
        # Lock the owning row so two uploads at once cannot both pass the cap.
        type(obj)._base_manager.select_for_update().filter(pk=obj.pk).exists()
        if attachments_for(obj).count() >= settings.ATTACHMENTS_PER_OBJECT_MAX:
            raise DocumentError(
                "This item already has the maximum number of files. Ask the board if you need more."
            )
        attachment.file.save("upload", uploaded, save=False)
        attachment.save()
    record_event(
        "attachment.added",
        actor=actor,
        request=request,
        target=obj,
        metadata={"owner_type": owner_type, "scan": result},
    )
    return attachment


def attachments_for(obj):
    owner_type, owner_id = _owner_key(obj)
    return Attachment.objects.filter(owner_type=owner_type, owner_id=owner_id)


def current_scan(attachment: Attachment) -> str:
    latest = attachment.scans.first()
    return latest.result if latest else attachment.scan_result


def serve(request, obj, attachment_id: int):
    """``obj`` must already have been fetched through a scoped manager."""
    attachment = attachments_for(obj).filter(pk=attachment_id).first()
    if attachment is None or not scan_allows_download(current_scan(attachment)):
        raise Http404
    record_event(
        "attachment.downloaded",
        request=request,
        actor=request.user,
        target=obj,
        metadata={"owner_type": attachment.owner_type, "scan": current_scan(attachment)},
    )
    response = FileResponse(
        attachment.file.open("rb"),
        as_attachment=True,
        filename=download_filename(attachment.original_filename, attachment.content_type),
        content_type=attachment.content_type,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


def pdf_response(data: bytes, filename: str):
    from django.http import HttpResponse

    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


__all__ = ["Http404", "attach", "attachments_for", "pdf_response", "serve"]
