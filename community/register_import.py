"""The home and owner register: reading, checking and loading it (D54).

Used by the ``import_register`` management command (a file on the server)
and by the board's "Import homes and owners" page (an uploaded file). Both
read the same columns, find the same problems and write through the same
services, so the audit trail is the usual one (``lot.recorded``,
``person.recorded``, ``ownership.recorded``, then ``register.imported``).

Columns (header row required; one row per owner, repeat the lot for joint
owners)::

    lot,street_address,owner_name,owner_email,owner_since,ownership_type
    [owner_phone,mailing_street,mailing_city,mailing_state,mailing_zip]

The file holds real personal data. Nothing from it is printed, logged or
audited except row numbers, column names and counts.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from community.models import Lot, Ownership, OwnershipType, Person, RegisterImport
from core.scoping import system_access

COLUMNS = ["lot", "street_address", "owner_name", "owner_email", "owner_since", "ownership_type"]
REQUIRED = COLUMNS[:5]
CONTACT_COLUMNS = ["owner_phone", "mailing_street", "mailing_city", "mailing_state", "mailing_zip"]
MAILING = CONTACT_COLUMNS[1:]
ALL_COLUMNS = COLUMNS + CONTACT_COLUMNS
MAX_ROWS = 2000
MAX_COLUMNS = 40
MAX_BYTES = 2_000_000
MAX_CELL = 500
# A real register workbook unpacks to well under a megabyte; these stop a
# small file that expands into gigabytes (a zip bomb) before it is opened.
XLSX_MAX_UNPACKED = 50 * 1024 * 1024
XLSX_MAX_MEMBERS = 200
XLSX_MAX_RATIO = 100
EXCEL_ERRORS = {"#N/A", "#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NUM!", "#NULL!", "#GETTING_DATA"}
EXAMPLE = "example"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class RegisterError(ValueError):
    """The file as a whole cannot be read. The message is safe to show."""


@dataclass(frozen=True)
class Problem:
    """One thing to fix: a row (``row``, the spreadsheet's row number) or a
    lot (``lot``). The message names no person, email or address."""

    message: str
    row: int | None = None
    column: str = ""
    lot: int | None = None

    def __str__(self) -> str:
        where = f"row {self.row}" if self.row is not None else f"lot {self.lot}"
        return f"{where}: {self.message}"


@dataclass
class Sheet:
    header: list[str]
    rows: list[dict]  # each with "_row": the row number people see
    unknown_columns: list[str] = field(default_factory=list)


@dataclass
class Check:
    """The result of reading and checking a register: what would happen."""

    rows: int
    lots: dict  # number -> {"address", "owners": [...]}
    problems: list[Problem]
    unknown_columns: list[str] = field(default_factory=list)
    notes: list[Problem] = field(default_factory=list)
    new_lots: list[int] = field(default_factory=list)
    fill: dict = field(default_factory=dict)  # number -> owners to record
    skip: list[int] = field(default_factory=list)  # lots that already have owners
    own_lots: list[int] = field(default_factory=list)  # lots listing the importer
    people_new: int = 0
    people_matched: int = 0

    @property
    def owners(self) -> int:
        return sum(len(v) for v in self.fill.values())

    @property
    def ok(self) -> bool:
        return not self.problems

    def counts(self) -> dict:
        return {
            "rows": self.rows,
            "lots_created": len(self.new_lots),
            "lots_filled": len(self.fill),
            "lots_left_alone": len(self.skip) + len(self.own_lots),
            "owners": self.owners,
            "people_created": self.people_new,
            "people_matched": self.people_matched,
        }

    def fingerprint(self) -> str:
        """What the board confirmed, so a register that changed between the
        check and the import is checked again rather than written."""
        c = self.counts()
        return f"{c['lots_created']}-{c['lots_filled']}-{c['owners']}-{c['people_created']}"


# --- Reading -----------------------------------------------------------------------------


def _header(raw) -> list[str]:
    return [("" if h is None else str(h)).strip().lower() for h in raw]


def _check_header(header: list[str]) -> list[str]:
    if len(header) > MAX_COLUMNS:
        raise RegisterError(f"Too many columns (at most {MAX_COLUMNS}).")
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise RegisterError(f"Missing column(s): {', '.join(missing)}.")
    return [h[:40] for h in dict.fromkeys(header) if h and h not in ALL_COLUMNS][:10]


def read_csv(data: bytes) -> Sheet:
    """UTF-8 (with or without a byte-order mark), comma separated."""
    if len(data) > MAX_BYTES:
        raise RegisterError("File too large for a register.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RegisterError(
            "The file is not UTF-8 text. Save it as 'CSV UTF-8' and upload it again."
        ) from exc
    if "\x00" in text:
        raise RegisterError("The file is not a CSV text file.")
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = _header(next(reader, []))
        unknown = _check_header(header)
        rows = []
        for raw in reader:
            if not any((v or "").strip() for v in raw):
                continue
            row = {
                header[i]: (v or "").strip()[:MAX_CELL]
                for i, v in enumerate(raw)
                if i < len(header) and header[i]
            }
            row["_row"] = reader.line_num
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise RegisterError("Too many rows.")
    except csv.Error as exc:
        raise RegisterError("The CSV file could not be read (check its quotes).") from exc
    return Sheet(header, rows, unknown)


def _check_archive(data: bytes) -> None:
    """Refuse a workbook that would unpack too large, before opening it.
    Python's zip reader never returns more than a member's declared size, so
    the declared sizes checked here bound what openpyxl can read."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise RegisterError("The file is not an Excel workbook (.xlsx).") from exc
    if len(infos) > XLSX_MAX_MEMBERS:
        raise RegisterError("The workbook has too many parts to be a register.")
    total = 0
    for info in infos:
        total += info.file_size
        if total > XLSX_MAX_UNPACKED or (
            info.compress_size and info.file_size / info.compress_size > XLSX_MAX_RATIO
        ):
            raise RegisterError("The workbook is too large when unpacked to be a register.")
        name = info.filename.lower()
        if "vbaproject" in name or name.endswith(".bin"):
            raise RegisterError("Workbooks with macros are not accepted.")


def _cell(value, column: str) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and column == "mailing_zip" and 0 < value < 100000:
        return f"{value:05d}"  # Excel drops a ZIP code's leading zero
    return str(value).strip()[:MAX_CELL]


def read_xlsx(data: bytes) -> Sheet:
    """The first sheet; row 1 is the header. Read-only, cached values only
    (no formulas are evaluated), links and macros never loaded."""
    if len(data) > MAX_BYTES:
        raise RegisterError("File too large for a register.")
    _check_archive(data)
    import openpyxl

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True, keep_links=False, keep_vba=False
        )
    except Exception as exc:  # any damaged or unusual workbook
        raise RegisterError("The workbook could not be read. Save it again as .xlsx.") from exc
    try:
        if not workbook.worksheets:
            raise RegisterError("The workbook has no sheets.")
        sheet = workbook.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=True, max_col=MAX_COLUMNS + 1)
        try:
            header = _header(next(rows_iter, ()) or ())
        except Exception as exc:
            raise RegisterError("The workbook could not be read.") from exc
        while header and header[-1] == "":
            header.pop()
        unknown = _check_header(header)
        rows: list[dict] = []
        number = 1
        try:
            for raw in rows_iter:
                number += 1
                values = [_cell(v, header[i] if i < len(header) else "") for i, v in enumerate(raw)]
                if not any(values):
                    continue
                row = {header[i]: v for i, v in enumerate(values) if i < len(header) and header[i]}
                row["_row"] = number
                rows.append(row)
                if len(rows) > MAX_ROWS:
                    raise RegisterError("Too many rows.")
        except RegisterError:
            raise
        except Exception as exc:
            raise RegisterError("The workbook could not be read.") from exc
    finally:
        workbook.close()
    return Sheet(header, rows, unknown)


def read(data: bytes, kind: str) -> Sheet:
    if kind == "xlsx":
        return read_xlsx(data)
    if kind == "csv":
        return read_csv(data)
    raise RegisterError("Only CSV and Excel (.xlsx) files are accepted.")


# --- Checking ----------------------------------------------------------------------------


def _clean(value: str) -> str:
    return " ".join((value or "").split())


def _valid_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 7 <= len(digits) <= 15 and not re.search(r"[^0-9+().\- ]", value)


def _is_example(row: dict) -> bool:
    return any(
        _clean(row.get(col, "")).lower().startswith(EXAMPLE) for col in ("lot", "owner_name")
    )


def validate(sheet: Sheet) -> tuple[dict, list[Problem]]:
    """Check every row. Returns the lots (number -> address and owners) and
    the problems; the messages are the ones the command has always printed."""
    from community.services import US_STATES

    problems: list[Problem] = []
    lots: dict[int, dict] = defaultdict(lambda: {"address": None, "owners": [], "rows": []})
    seen: dict[tuple, int] = {}
    for row in sheet.rows:
        i = row["_row"]

        def problem(message, column="", _i=i):
            problems.append(Problem(message, row=_i, column=column))

        for column, value in row.items():
            if column != "_row" and value in EXCEL_ERRORS:
                problem(f"{column} holds a spreadsheet error ({value})", column)
        if _is_example(row):
            problem("this is an example row from the template; delete it", "owner_name")
            continue
        try:
            number = int(_clean(row.get("lot", "")))
            if not 1 <= number <= 9999:
                raise ValueError
        except ValueError:
            problem("lot must be a whole number", "lot")
            continue
        address = _clean(row.get("street_address", ""))[:200]
        name = _clean(row.get("owner_name", ""))[:150]
        email = row.get("owner_email", "").strip()
        kind = _clean(row.get("ownership_type") or "").lower()
        key = (number, address.lower(), name.lower(), email.lower())
        if key in seen:
            problem(f"repeats row {seen[key]}; delete one of them", "owner_name")
            continue
        seen[key] = i
        if not address:
            problem("street_address is empty", "street_address")
        elif lots[number]["address"] not in (None, address):
            problem(f"lot {number} has two different addresses", "street_address")
        lots[number]["address"] = lots[number]["address"] or address
        if not name:
            problem("owner_name is empty", "owner_name")
        if email:
            try:
                validate_email(email)
                if len(email) > 254:
                    raise ValidationError("too long")
            except ValidationError:
                problem("owner_email is not a valid address", "owner_email")
        try:
            since = datetime.date.fromisoformat(row.get("owner_since", ""))
        except ValueError:
            problem("owner_since must be YYYY-MM-DD", "owner_since")
            continue
        if since > timezone.localdate():
            problem("owner_since is in the future", "owner_since")
        if kind and kind not in OwnershipType.values:
            problem(f"ownership_type must be one of {OwnershipType.values}", "ownership_type")
        phone = _clean(row.get("owner_phone", ""))
        if phone and not _valid_phone(phone):
            problem("owner_phone is not a phone number (7 to 15 digits)", "owner_phone")
        mailing = None
        given = {c: _clean(row.get(c, "")) for c in MAILING}
        if any(given.values()):
            state = given["mailing_state"].upper()
            zip_code = given["mailing_zip"]
            bad = [c for c in ("mailing_street", "mailing_city") if not given[c]]
            if state not in US_STATES:
                bad.append("mailing_state")
            if not re.fullmatch(r"[0-9]{5}(-?[0-9]{4})?", zip_code):
                bad.append("mailing_zip")
            if bad:
                problem(
                    "the mailing address needs a street, city, two-letter state and ZIP code "
                    f"(check {', '.join(bad)})",
                    bad[0],
                )
            else:
                if len(zip_code) == 9:
                    zip_code = f"{zip_code[:5]}-{zip_code[5:]}"
                mailing = {
                    "street": given["mailing_street"],
                    "city": given["mailing_city"],
                    "state": state,
                    "zip": zip_code,
                }
        lots[number]["owners"].append(
            {
                "name": name,
                "email": email,
                "since": since,
                "kind": kind,
                "phone": phone,
                "mailing": mailing,
                "row": i,
            }
        )
    for number, lot in lots.items():
        emails = [o["email"].lower() for o in lot["owners"] if o["email"]]
        if len(emails) != len(set(emails)):
            problems.append(Problem("the same email is listed twice", lot=number))
    return dict(lots), problems


def _person_of(actor) -> int | None:
    if actor is None or not getattr(actor, "is_authenticated", False):
        return None
    with system_access("register import: importer"):
        return Person.objects.filter(user=actor).values_list("pk", flat=True).first()


def check(sheet: Sheet, *, actor=None) -> Check:
    """Read-only: what an import of ``sheet`` would do now. ``actor`` is the
    board member importing in the portal; lots listing them are left for
    another board member (nobody records their own ownership)."""
    lots, problems = validate(sheet)
    result = Check(
        rows=len(sheet.rows),
        lots=lots,
        problems=problems,
        unknown_columns=sheet.unknown_columns,
    )
    if problems:
        return result
    me = _person_of(actor)
    emails = {o["email"].lower() for lot in lots.values() for o in lot["owners"] if o["email"]}
    with system_access("register import: check"):
        existing = {lot.number: lot for lot in Lot.objects.filter(number__in=list(lots))}
        known: dict[str, int] = {}
        for pk, email in Person.objects.filter(email__isnull=False).values_list("pk", "email"):
            if email and email.lower() in emails:
                known.setdefault(email.lower(), pk)
        owned = set(
            Ownership.objects.active()
            .filter(lot__number__in=list(lots))
            .values_list("lot__number", flat=True)
        )
    planned_new: set[str] = set()
    for number, lot in sorted(lots.items()):
        current = existing.get(number)
        if current is None:
            result.new_lots.append(number)
        elif current.street_address.strip() and _clean(current.street_address) != lot["address"]:
            result.notes.append(
                Problem(
                    "the address on record differs from the file; the recorded one is kept",
                    lot=number,
                )
            )
        if current is not None and number in owned:
            result.skip.append(number)
            continue
        if me is not None and any(
            known.get(o["email"].lower()) == me for o in lot["owners"] if o["email"]
        ):
            result.own_lots.append(number)
            continue
        result.fill[number] = lot["owners"]
        for owner in lot["owners"]:
            email = owner["email"].lower()
            if email and email in known:
                result.people_matched += 1
            elif email and email in planned_new:
                result.people_matched += 1  # the same new person on another lot
            else:
                result.people_new += 1
                if email:
                    planned_new.add(email)
    return result


# --- Writing -----------------------------------------------------------------------------


def write(result: Check, *, actor, request=None) -> dict:
    """Write a checked register in one transaction through the community
    services, as ``actor`` (``core.policy.SYSTEM`` from the command, the
    board member in the portal). Returns the counts written."""
    from community import services

    if result.problems:
        raise RegisterError("The register has problems; nothing was written.")
    created_people = matched_people = created_lots = 0
    with transaction.atomic():
        for number, owners in result.fill.items():
            with system_access("register import: lot"):
                lot = Lot.objects.filter(number=number).first()
            if lot is None:
                lot = services.record_lot(
                    number=number,
                    street_address=result.lots[number]["address"],
                    actor=actor,
                    request=request,
                )
                created_lots += 1
            for owner in owners:
                person = None
                if owner["email"]:
                    with system_access("register import: match person"):
                        person = Person.objects.filter(email__iexact=owner["email"]).first()
                if person is None:
                    person = services.record_person(
                        display_name=owner["name"],
                        email=owner["email"],
                        actor=actor,
                        request=request,
                    )
                    created_people += 1
                    if owner.get("phone") or owner.get("mailing"):
                        services.record_imported_contact(
                            person,
                            phone=owner.get("phone", ""),
                            mailing=owner.get("mailing"),
                            actor=actor,
                            request=request,
                        )
                else:
                    matched_people += 1
                kind = owner["kind"] or (
                    OwnershipType.JOINT if len(owners) > 1 else OwnershipType.SOLE
                )
                services.record_ownership(
                    person=person,
                    lot=lot,
                    start_date=owner["since"],
                    ownership_type=kind,
                    actor=actor,
                    request=request,
                )
        # Homes listing the importer: the home is added, the owners are left
        # for another board member.
        for number in result.own_lots:
            with system_access("register import: lot"):
                exists = Lot.objects.filter(number=number).exists()
            if not exists:
                services.record_lot(
                    number=number,
                    street_address=result.lots[number]["address"],
                    actor=actor,
                    request=request,
                )
                created_lots += 1
    counts = result.counts()
    counts.update(
        lots_created=created_lots, people_created=created_people, people_matched=matched_people
    )
    return counts


# --- Uploaded files (the portal) -------------------------------------------------------


UPLOAD_TYPES = {"csv", "xlsx"}


class UploadError(ValueError):
    """An upload that was refused. The message is safe to show."""


def scan(fileobj):  # a seam for tests, like documents.services.scan
    from documents.scanning import scan as run

    return run(fileobj)


def latest_scan(record: RegisterImport) -> str:
    newest = record.scans.first()
    return newest.result if newest else record.scan_result


def scan_ok(record: RegisterImport) -> bool:
    from documents.services import scan_allows_download

    return scan_allows_download(latest_scan(record))


def upload(uploaded, *, actor, request=None) -> RegisterImport:
    """Check, scan and store an uploaded register privately. Board only
    (``ownership.manage``). Nothing is imported here."""
    from audit.services import record_event
    from core.policy import require
    from documents.models import ScanResult
    from documents.validation import UploadRejected, check_upload, safe_filename

    require(actor, "ownership.manage")
    if uploaded is None:
        raise UploadError("Choose the register file (CSV or Excel .xlsx).")
    name = (uploaded.name or "").lower()
    if name.endswith((".xlsm", ".xlsb", ".xls", ".xltm")):
        raise UploadError(
            "Only CSV and Excel .xlsx files are accepted. Save the workbook as "
            "'Excel Workbook (.xlsx)': workbooks with macros are never accepted."
        )
    if uploaded.size is not None and uploaded.size > MAX_BYTES:
        raise UploadError(f"A register file must be {MAX_BYTES // 1_000_000} MB or smaller.")
    try:
        kind = check_upload(uploaded, allowed=UPLOAD_TYPES)
    except UploadRejected as exc:
        raise UploadError(str(exc)) from exc
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    result, _scanner = scan(uploaded)
    if result == ScanResult.INFECTED:
        record_event(
            "register.upload_blocked", actor=actor, request=request, metadata={"reason": "malware"}
        )
        raise UploadError("The file was flagged by the malware scanner and was not stored.")
    record = RegisterImport(
        kind=kind.ext,
        original_filename=safe_filename(uploaded.name, kind.ext),
        size=uploaded.size,
        sha256=digest.hexdigest(),
        scan_result=result,
        uploaded_by=actor,
    )
    with transaction.atomic():
        record.file.save("upload", uploaded, save=False)
        record.save()
        record_event(
            "register.file_uploaded",
            actor=actor,
            request=request,
            target=record,
            metadata={"kind": kind.ext, "scan": result, "size": uploaded.size},
        )
    return record


def rescan(record: RegisterImport, *, actor, request=None) -> str:
    from audit.services import record_event
    from community.models import RegisterImportScan
    from core.policy import require

    require(actor, "ownership.manage")
    if not record.has_file:
        raise UploadError("The file has been removed.")
    with record.file.open("rb") as fh:
        result, scanner = scan(fh)
    RegisterImportScan.objects.create(register_import=record, result=result, scanner=scanner)
    record_event(
        "register.file_rescanned",
        actor=actor,
        request=request,
        target=record,
        metadata={"scan": result},
    )
    return result


def load(record: RegisterImport) -> Sheet:
    """Read a stored register; only once its scan allows it."""
    if not record.has_file:
        raise RegisterError("The file has been removed.")
    if not scan_ok(record):
        raise RegisterError("The file has not passed the malware check.")
    with record.file.open("rb") as fh:
        data = fh.read(MAX_BYTES + 1)
    return read(data, record.kind)


def run_import(record: RegisterImport, *, expect: str, actor, request=None) -> dict:
    """Import a stored, checked register as the board member ``actor``."""
    from audit.services import record_event
    from core.policy import require

    require(actor, "ownership.manage")
    require(actor, "person.manage")
    with transaction.atomic():
        locked = RegisterImport.objects.select_for_update().get(pk=record.pk)
        if locked.imported_at is not None:
            raise UploadError("This file has already been imported.")
        result = check(load(locked), actor=actor)
        if result.problems:
            raise UploadError("The file has problems; fix them and upload it again.")
        if result.fingerprint() != expect:
            raise UploadError(
                "The register changed since it was checked. Check the new figures and import again."
            )
        counts = write(result, actor=actor, request=request)
        locked.imported_at = timezone.now()
        locked.imported_by = actor
        locked.counts = counts
        locked.save(update_fields=["imported_at", "imported_by", "counts"])
        record_event(
            "register.imported",
            actor=actor,
            request=request,
            target=locked,
            metadata={
                "lots": counts["lots_filled"],
                "owners": counts["owners"],
                "people_created": counts["people_created"],
                "lots_created": counts["lots_created"],
                "lots_left_alone": counts["lots_left_alone"],
                "people_matched": counts["people_matched"],
                "rows": counts["rows"],
            },
        )
    return counts


def remove_file(record: RegisterImport, *, actor, request=None, reason: str = "board") -> None:
    """Delete the stored file (it holds personal data); the record stays."""
    from audit.services import record_event
    from core.policy import SYSTEM, require

    if actor is not SYSTEM:
        require(actor, "ownership.manage")
    with transaction.atomic():
        locked = RegisterImport.objects.select_for_update().get(pk=record.pk)
        if not locked.has_file:
            return
        # The file goes first: if the bucket refuses, the record still
        # shows the file as kept and the board (or tonight's run) tries again.
        locked.file.storage.delete(locked.file.name)
        locked.file = ""
        locked.file_removed_at = timezone.now()
        locked.file_removed_by = None if actor is SYSTEM else actor
        locked.save(update_fields=["file", "file_removed_at", "file_removed_by"])
        record_event(
            "register.file_removed",
            actor=None if actor is SYSTEM else actor,
            request=request,
            target=locked,
            metadata={"reason": reason},
        )


def purge_old_files(now=None) -> int:
    """Remove register files older than ``REGISTER_IMPORT_FILE_DAYS`` (the
    nightly task). Audited per file with reason "retention"."""
    from core.policy import SYSTEM

    now = now or timezone.now()
    cutoff = now - datetime.timedelta(days=settings.REGISTER_IMPORT_FILE_DAYS)
    old = RegisterImport.objects.filter(uploaded_at__lt=cutoff, file_removed_at__isnull=True)
    count = 0
    for record in old.exclude(file=""):
        remove_file(record, actor=SYSTEM, reason="retention")
        count += 1
    return count


# --- Templates ---------------------------------------------------------------------------

EXAMPLE_ROWS = [
    [
        "1",
        "100 Example Lane",
        "EXAMPLE \u2013 delete: Pat Sample",
        "pat.sample@example.com",
        "2019-06-14",
        "sole",
        "215-555-0100",
        "",
        "",
        "",
        "",
    ],
    [
        "2",
        "102 Example Lane",
        "EXAMPLE \u2013 delete: Sam Sample",
        "",
        "2021-03-02",
        "joint",
        "",
        "PO Box 100",
        "Exampleton",
        "PA",
        "19000",
    ],
]

INSTRUCTIONS = [
    ("lot", "Required", "The lot number on the plat, a whole number (1, 2, 3 ...)."),
    (
        "street_address",
        "Required",
        "The home's street address, e.g. 100 Example Lane. People know homes by this.",
    ),
    (
        "owner_name",
        "Required",
        "The owner's name as on the deed. One row per owner: repeat the lot and address "
        "on a second row for joint owners. A trust or company is one owner.",
    ),
    (
        "owner_email",
        "Optional",
        "Their email, used for the invitation to sign in. Someone already in the portal "
        "with this email is matched, not added twice.",
    ),
    (
        "owner_since",
        "Required",
        "The deed date, YYYY-MM-DD (e.g. 2019-06-14). An Excel date cell works too.",
    ),
    (
        "ownership_type",
        "Optional",
        "sole, joint, trust or entity. Blank: sole, or joint when a lot has several owners.",
    ),
    ("owner_phone", "Optional", "A phone number, e.g. 215-555-0123."),
    (
        "mailing_street",
        "Optional",
        "Only when post goes somewhere other than the home: the street or PO box.",
    ),
    ("mailing_city", "Optional", "With mailing_street: the city or town."),
    ("mailing_state", "Optional", "With mailing_street: the two-letter state, e.g. PA."),
    ("mailing_zip", "Optional", "With mailing_street: the 5-digit ZIP code."),
]

INSTRUCTION_NOTES = [
    "Delete the two EXAMPLE rows before importing: rows whose lot or owner name starts "
    "with EXAMPLE are refused.",
    "Keep the header row (row 1) exactly as it is. Other columns are ignored.",
    "Homes that already have current owners in the portal are left alone; a change of "
    "owner is a transfer on the home's page.",
    "Importing the same file twice is safe: nothing is added twice.",
    "This file will hold owners' names and emails. Keep it private and delete your copy "
    "after the import.",
]


def template_csv() -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(ALL_COLUMNS)
    writer.writerows(EXAMPLE_ROWS)
    return ("﻿" + out.getvalue()).encode("utf-8")


def template_xlsx() -> bytes:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Register"
    sheet.append(ALL_COLUMNS)
    bold = Font(bold=True)
    for cell in sheet[1]:
        cell.font = bold
    grey = PatternFill("solid", fgColor="EEEEEE")
    for values in EXAMPLE_ROWS:
        row = list(values)
        row[0] = int(row[0])
        row[4] = datetime.date.fromisoformat(row[4])
        sheet.append(row)
    for row in sheet.iter_rows(min_row=2, max_row=1 + len(EXAMPLE_ROWS)):
        for cell in row:
            cell.fill = grey
        row[4].number_format = "yyyy-mm-dd"
        for cell in (row[6], row[10]):  # phone and ZIP stay text
            cell.number_format = "@"
    widths = [6, 26, 32, 28, 12, 15, 15, 22, 16, 13, 11]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[openpyxl.utils.get_column_letter(index)].width = width
    sheet.freeze_panes = "A2"

    notes = workbook.create_sheet("Instructions")
    notes.append(["Home and owner register: how to fill it in"])
    notes["A1"].font = Font(bold=True, size=13)
    notes.append([])
    notes.append(["Column", "Required?", "What to enter"])
    for cell in notes[3]:
        cell.font = bold
    for row in INSTRUCTIONS:
        notes.append(list(row))
    notes.append([])
    for note in INSTRUCTION_NOTES:
        notes.append([note])
    notes.column_dimensions["A"].width = 18
    notes.column_dimensions["B"].width = 11
    notes.column_dimensions["C"].width = 90
    for row in notes.iter_rows(min_row=4):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()
