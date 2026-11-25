"""Attach validated, scanned files to any object; serve them only after the
caller has fetched the owning object through its scoped manager."""

from __future__ import annotations

import hashlib

from django.http import FileResponse, Http404

from audit.services import record_event
from documents.models import Attachment, ScanResult
from documents.scanning import scan
from documents.services import DocumentError, _safe_name
from documents.validation import UploadRejected, check_upload

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
        original_filename=_safe_name(uploaded.name),
        content_type=kind.content_type,
        size=uploaded.size,
        sha256=digest.hexdigest(),
        scan_result=result,
        label=label[:200],
        uploaded_by=actor,
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


def serve(request, obj, attachment_id: int):
    """``obj`` must already have been fetched through a scoped manager."""
    attachment = attachments_for(obj).filter(pk=attachment_id).first()
    if attachment is None or attachment.scan_result in (ScanResult.INFECTED, ScanResult.ERROR):
        raise Http404
    record_event(
        "attachment.downloaded",
        request=request,
        actor=request.user,
        target=obj,
        metadata={"owner_type": attachment.owner_type, "scan": attachment.scan_result},
    )
    response = FileResponse(
        attachment.file.open("rb"),
        as_attachment=True,
        filename=attachment.original_filename,
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
