"""Attach validated, scanned files to any object; serve them only after the
caller has fetched the owning object through its scoped manager."""

from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404

from audit.services import record_event
from documents.models import Attachment, ScanResult
from documents.scanning import detail_of, scan
from documents.services import DocumentError, scan_allows_download
from documents.validation import UploadRejected, check_upload, download_filename, safe_filename

IMAGES_AND_PDF = {"pdf", "png", "jpg", "jpeg"}
logger = logging.getLogger(__name__)


def _owner_key(obj) -> tuple[str, str]:
    return obj._meta.label_lower, str(obj.pk)


def attach(
    obj,
    uploaded,
    *,
    actor,
    label: str = "",
    allowed: set[str] | None = None,
    request=None,
    label_max: int | None = None,
) -> Attachment:
    """``label_max``: most files with this label the object may hold (e.g. an
    owner's own allowance), checked under the same lock as the overall cap so
    parallel uploads cannot pass it (review R2-006)."""
    try:
        kind = check_upload(uploaded, allowed=allowed or IMAGES_AND_PDF)
    except UploadRejected as exc:
        raise DocumentError(str(exc)) from exc
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    outcome = scan(uploaded)
    result = outcome[0]
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
        scan_detail=detail_of(outcome),
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
        if label_max is not None and attachments_for(obj).filter(label=label[:200]).count() >= (
            label_max
        ):
            raise DocumentError(
                f"No more files like this can be added (limit {label_max}). Send the board a"
                " message if there's more to show."
            )
        attachment.file.save("upload", uploaded, save=False)
        attachment.save()
        if result == ScanResult.ERROR:
            # Scanner unavailable at upload: try again shortly (D57).
            from documents import scan_queue

            scan_queue.queue_soon(scan_queue.ATTACHMENT, attachment.pk)
    record_event(
        "attachment.added",
        actor=actor,
        request=request,
        target=obj,
        metadata={"owner_type": owner_type, "scan": result},
    )
    return attachment


MAX_FILES_PER_UPLOAD = 10


def attach_many(
    request,
    obj,
    uploads,
    *,
    actor,
    label: str = "",
    allowed: set[str] | None = None,
    label_max: int | None = None,
) -> int:
    """Attach several files chosen at once (``<input type=file multiple>``).
    The caller has already checked who may add files to ``obj``. Each file is
    checked on its own; the page is told how many were added and why any was
    refused. Returns the number added."""
    from django.contrib import messages

    uploads = list(uploads)
    added, problems = 0, []
    if len(uploads) > MAX_FILES_PER_UPLOAD:
        problems.append(f"Please add at most {MAX_FILES_PER_UPLOAD} files at a time.")
        uploads = uploads[:MAX_FILES_PER_UPLOAD]
    for uploaded in uploads:
        try:
            attach(
                obj,
                uploaded,
                actor=actor,
                label=label,
                allowed=allowed,
                request=request,
                label_max=label_max,
            )
        except DocumentError as exc:
            name = safe_filename(uploaded.name or "file", "x").rsplit(".", 1)[0][:80]
            problems.append(f"{name}: {exc}")
        else:
            added += 1
    if added:
        messages.success(request, "File added." if added == 1 else f"{added} files added.")
    for problem in problems:
        messages.error(request, problem)
    return added


def attachments_for(obj):
    owner_type, owner_id = _owner_key(obj)
    return Attachment.objects.filter(owner_type=owner_type, owner_id=owner_id)


def current_scan(attachment: Attachment) -> str:
    latest = attachment.scans.first()
    return latest.result if latest else attachment.scan_result


def serve(request, obj, attachment_id: int, *, exclude_labels=()):
    """``obj`` must already have been fetched through a scoped manager (or, for
    a single-use sign-off link, the link must already have been checked)."""
    files = attachments_for(obj)
    if exclude_labels:
        files = files.exclude(label__in=list(exclude_labels))
    attachment = files.filter(pk=attachment_id).first()
    if attachment is None or not scan_allows_download(current_scan(attachment)):
        raise Http404
    # Open the stored file before logging the download: a file the store no
    # longer holds (lost, restored without media, disposed) is a 404 page with
    # its links, never a bare Server Error, and no download is audited (WT-001).
    try:
        stream = attachment.file.open("rb")
    except (FileNotFoundError, OSError):
        logger.warning(
            "documents.attachment_file_missing",
            extra={"attachment_id": attachment.pk, "owner_type": attachment.owner_type},
        )
        raise Http404 from None
    record_event(
        "attachment.downloaded",
        request=request,
        actor=request.user if request.user.is_authenticated else None,
        target=obj,
        metadata={
            "owner_type": attachment.owner_type,
            "scan": current_scan(attachment),
            # Small previews on a page are logged too, but marked, so the
            # audit log tells a glance from a download (owner's choice).
            "view": "thumbnail" if request.GET.get("view") == "thumb" else "file",
        },
    )
    response = FileResponse(
        stream,
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


__all__ = ["Http404", "attach", "attach_many", "attachments_for", "pdf_response", "serve"]
