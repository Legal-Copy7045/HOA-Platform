"""Import homes and owners (D54): the board uploads the register (CSV or
Excel), the file is checked and scanned, a dry run shows what would happen,
and the board confirms the import. Board only (``ownership.manage``): the
manager does not record ownerships and a sysadmin sees no owner data.

Nothing from the file is logged or audited except counts.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_POST

from community import register_import as ri
from community.models import Lot, Ownership, Person, RegisterImport
from community.services import CommunityError
from core import policy
from core.scoping import system_access
from core.uploads import limit_uploads

PERMISSION = "ownership.manage"


def _record(request, pk) -> RegisterImport:
    policy.require(request.user, PERMISSION)
    return get_object_or_404(RegisterImport.objects.select_related("uploaded_by"), pk=pk)


def _page(request, *, error: str = "", status: int = 200):
    from django.conf import settings

    with system_access("register import: empty register"):
        homes = Lot.objects.count()
    return render(
        request,
        "community/register_import.html",
        {
            "history": RegisterImport.objects.select_related("uploaded_by", "imported_by")[:20],
            "error": error,
            "max_mb": ri.MAX_BYTES // 1_000_000,
            "keep_days": settings.REGISTER_IMPORT_FILE_DAYS,
            "homes": homes,
            "columns": ri.INSTRUCTIONS,
        },
        status=status,
    )


@csrf_exempt  # so the size limit is in place before the body is read; checked below
@login_required
def register_import(request):
    """Step 1: the upload form (and earlier imports)."""
    policy.require(request.user, PERMISSION)
    if request.method == "POST":
        if not limit_uploads(request, ri.MAX_BYTES):
            return _too_large(request)
        return _upload(request)
    return _page(request)


def _too_large(request):
    return _page(
        request,
        error=f"A register file must be {ri.MAX_BYTES // 1_000_000} MB or smaller.",
        status=413,
    )


@csrf_protect
def _upload(request):
    uploaded = request.FILES.get("file")
    if getattr(request, "upload_too_large", False):
        return _too_large(request)
    try:
        record = ri.upload(uploaded, actor=request.user, request=request)
    except ri.UploadError as exc:
        return _page(request, error=str(exc), status=400)
    return redirect("register_import_detail", pk=record.pk)


def _preview_rows(result: ri.Check) -> list[dict]:
    """One row per home in the file, for the dry-run table."""
    emails = {
        o["email"].lower() for lot in result.lots.values() for o in lot["owners"] if o["email"]
    }
    with system_access("register import: preview"):
        known = {
            (p.email or "").lower()
            for p in Person.objects.exclude(email="").only("email")
            if (p.email or "").lower() in emails
        }
        current = {}
        for o in (
            Ownership.objects.active()
            .filter(lot__number__in=list(result.lots))
            .select_related("person", "lot")
        ):
            current.setdefault(o.lot.number, []).append(o.person.display_name)
    rows = []
    for number, lot in sorted(result.lots.items()):
        if number in result.skip:
            status, tone = "Already has owners: left alone", "neutral"
        elif number in result.own_lots:
            status, tone = "Lists you: left for another board member", "warning"
        elif number in result.new_lots:
            status, tone = "New home", "success"
        else:
            status, tone = "Existing home: owners added", "info"
        owners = [
            {
                "name": o["name"],
                "email": o["email"],
                "matched": bool(o["email"]) and o["email"].lower() in known,
                "since": o["since"],
                "kind": o["kind"],
                "extra": bool(o.get("phone") or o.get("mailing")),
            }
            for o in lot["owners"]
        ]
        rows.append(
            {
                "number": number,
                "address": lot["address"],
                "owners": owners,
                "current": current.get(number, []),
                "status": status,
                "tone": tone,
            }
        )
    rows.sort(key=lambda r: (r["address"] or "").lower())
    return rows


@login_required
@require_GET
def register_import_detail(request, pk):
    """Step 2 (the check, nothing written) or, once imported, the summary."""
    record = _record(request, pk)
    context = {
        "record": record,
        "scan": ri.latest_scan(record),
        "scan_ok": ri.scan_ok(record),
        "keep_days": _keep_days(),
    }
    if record.imported_at is not None:
        return render(request, "community/register_import_done.html", context)
    if record.has_file and context["scan_ok"]:
        try:
            result = ri.check(ri.load(record), actor=request.user)
        except ri.RegisterError as exc:
            context["read_error"] = str(exc)
        else:
            context.update(
                result=result,
                counts=result.counts(),
                rows=_preview_rows(result) if result.ok else [],
                fingerprint=result.fingerprint(),
                nothing_to_do=result.ok and not result.fill and not result.new_lots,
            )
    return render(request, "community/register_import_check.html", context)


def _keep_days() -> int:
    from django.conf import settings

    return settings.REGISTER_IMPORT_FILE_DAYS


@login_required
@require_POST
def register_import_run(request, pk):
    """Step 3: write it, as the board member pressing the button."""
    record = _record(request, pk)
    try:
        counts = ri.run_import(
            record, expect=request.POST.get("expect", ""), actor=request.user, request=request
        )
    except (ri.UploadError, ri.RegisterError) as exc:
        messages.error(request, str(exc))
    except CommunityError as exc:
        messages.error(request, f"Nothing was imported: {exc}")
    else:
        messages.success(
            request,
            f"Imported {counts['lots_created']} new home(s) and {counts['owners']} owner(s).",
        )
    return redirect("register_import_detail", pk=record.pk)


@login_required
@require_POST
def register_import_rescan(request, pk):
    record = _record(request, pk)
    try:
        ri.rescan(record, actor=request.user, request=request)
    except ri.UploadError as exc:
        messages.error(request, str(exc))
    return redirect("register_import_detail", pk=record.pk)


@login_required
@require_POST
def register_import_remove(request, pk):
    record = _record(request, pk)
    ri.remove_file(record, actor=request.user, request=request)
    messages.success(request, "The file was deleted. The record of the import is kept.")
    if request.POST.get("next") == "list":
        return redirect("register_import")
    return redirect("register_import_detail", pk=record.pk)


@login_required
@require_GET
def register_import_file(request, pk):
    """The stored file, to the board only, after the scan check."""
    from audit.services import record_event
    from documents.validation import download_filename

    record = _record(request, pk)
    if not record.has_file or not ri.scan_ok(record):
        raise Http404
    record_event(
        "register.file_downloaded",
        actor=request.user,
        request=request,
        target=record,
        metadata={"kind": record.kind},
    )
    content_type = "text/csv" if record.kind == "csv" else ri.XLSX_TYPE
    response = FileResponse(
        record.file.open("rb"),
        as_attachment=True,
        filename=download_filename(record.original_filename, content_type),
        content_type=content_type,
    )
    _private(response)
    return response


def _private(response):
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"


@login_required
@require_GET
def register_template(request, fmt):
    """The blank register, made on the fly (two EXAMPLE rows and, in the
    workbook, an Instructions sheet). No personal data."""
    policy.require(request.user, PERMISSION)
    if fmt == "xlsx":
        body, content_type = ri.template_xlsx(), ri.XLSX_TYPE
    elif fmt == "csv":
        body, content_type = ri.template_csv(), "text/csv; charset=utf-8"
    else:
        raise Http404
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="register-template.{fmt}"'
    _private(response)
    return response
