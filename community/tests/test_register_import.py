"""Import homes and owners in the portal (D54): templates, CSV and Excel
reading, checks, the malware-scan gate, the dry run, the import, permissions,
the private file and its removal. All data here is fictional."""

from __future__ import annotations

import datetime
import io
import zipfile
from io import StringIO

import openpyxl
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from community import register_import as ri
from community.models import (
    Account,
    ContactMethod,
    ContactProfile,
    Lot,
    Ownership,
    Person,
    RegisterImport,
    Role,
)
from community.tests.factories import LotFactory, UserFactory, enroll_totp, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from documents.models import ScanResult

pytestmark = pytest.mark.django_db

HEADER = "lot,street_address,owner_name,owner_email,owner_since,ownership_type"
GOOD = (
    HEADER + "\n"
    "1,100 Fictional Lane,Pat Imaginary,pat.imaginary@example.com,2019-06-14,\n"
    "2,102 Fictional Lane,Sam Madeup,sam.madeup@example.com,2021-03-02,\n"
    "2,102 Fictional Lane,Alex Madeup,,2021-03-02,\n"
)


@pytest.fixture(autouse=True)
def clean_scan(monkeypatch):
    """The scanner says clean unless a test says otherwise."""
    monkeypatch.setattr(ri, "scan", lambda f: (ScanResult.CLEAN, "test"))


@pytest.fixture
def board(client):
    user = make_member(board=True)
    client.force_login(user)
    return user


def _csv_file(text: str, name: str = "register.csv", bom: bool = False) -> SimpleUploadedFile:
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    return SimpleUploadedFile(name, data, content_type="text/csv")


def _xlsx_bytes(rows, extra_members=None, sheet_first=None) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    if sheet_first is not None:
        sheet.title = "Other"
        sheet.append(sheet_first)
        sheet = workbook.create_sheet("Second")
        workbook.move_sheet(sheet, offset=-1)
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    data = out.getvalue()
    if extra_members:
        buffer = io.BytesIO(data)
        with zipfile.ZipFile(buffer, "a") as archive:
            for name, content in extra_members.items():
                archive.writestr(name, content)
        data = buffer.getvalue()
    return data


def _xlsx_file(rows, name="register.xlsx", **kw) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, _xlsx_bytes(rows, **kw))


def _upload(client, upload):
    return client.post(reverse("register_import"), {"file": upload})


def _record() -> RegisterImport:
    return RegisterImport.objects.latest("id")


def _check_page(client, record=None):
    record = record or _record()
    return client.get(reverse("register_import_detail", args=[record.pk]))


def _import(client, record=None):
    record = record or _record()
    page = _check_page(client, record)
    expect = page.context["fingerprint"]
    return client.post(reverse("register_import_run", args=[record.pk]), {"expect": expect})


# --- Templates ---------------------------------------------------------------------------


def test_csv_template_header_and_example_rows(client, board):
    response = client.get(reverse("register_template", args=["csv"]))
    assert response.status_code == 200
    assert response["Content-Disposition"].endswith('register-template.csv"')
    assert response["Cache-Control"] == "private, no-store"
    text = response.content.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0].split(",") == ri.ALL_COLUMNS
    assert len(lines) == 3 and all("EXAMPLE" in line for line in lines[1:])


def test_xlsx_template_opens_and_has_instructions(client, board):
    response = client.get(reverse("register_template", args=["xlsx"]))
    assert response.status_code == 200
    workbook = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
    assert workbook.sheetnames == ["Register", "Instructions"]
    rows = list(workbook["Register"].iter_rows(values_only=True))
    assert list(rows[0]) == ri.ALL_COLUMNS
    assert all(str(r[2]).startswith("EXAMPLE") for r in rows[1:])
    instructions = " ".join(
        str(c) for r in workbook["Instructions"].iter_rows(values_only=True) for c in r if c
    )
    for column in ri.ALL_COLUMNS:
        assert column in instructions
    assert "EXAMPLE" in instructions


def test_the_templates_own_examples_are_refused(client, board):
    for fmt in ("csv", "xlsx"):
        body = client.get(reverse("register_template", args=[fmt])).content
        sheet = ri.read(body, fmt)
        result = ri.check(sheet)
        assert len(result.problems) == 2
        assert all("example row" in p.message for p in result.problems)


def test_docs_example_matches_the_generated_template():
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "docs/examples/register-template.csv"
    assert path.read_bytes() == ri.template_csv()


# --- Reading and checking ----------------------------------------------------------------


def test_csv_happy_path_through_the_portal(client, board):
    response = _upload(client, _csv_file(GOOD))
    record = _record()
    assert response.status_code == 302
    assert response["Location"] == reverse("register_import_detail", args=[record.pk])
    page = _check_page(client, record)
    assert page.status_code == 200
    counts = page.context["counts"]
    assert counts["lots_created"] == 2 and counts["owners"] == 3
    assert counts["people_created"] == 3
    with system_access("t"):
        assert not Lot.objects.exists()  # the check writes nothing
    assert b"Import 2 homes and 3 owners" in page.content
    response = _import(client, record)
    assert response.status_code == 302
    with system_access("t"):
        assert Lot.objects.count() == 2
        lot2 = Ownership.objects.active().filter(lot__number=2)
        assert {o.ownership_type for o in lot2} == {"joint"}
        assert Ownership.objects.get(lot__number=1).ownership_type == "sole"
        assert Ownership.objects.get(lot__number=1).recorded_by == board
    record.refresh_from_db()
    assert record.imported_by == board and record.counts["owners"] == 3
    done = _check_page(client, record)
    assert b"Register imported" in done.content
    # Straight to the board's Sign-ups, not through the invitations redirect.
    assert (reverse("lot_list") + "?view=signups").encode() in done.content
    assert ('href="' + reverse("invitations") + '"').encode() not in done.content
    assert reverse("lot_list").encode() in done.content


def test_csv_with_a_byte_order_mark(client, board):
    _upload(client, _csv_file(GOOD, bom=True))
    page = _check_page(client)
    assert page.context["result"].ok
    assert page.context["counts"]["owners"] == 3


def test_xlsx_happy_path_with_excel_date_cells(client, board):
    rows = [
        ri.COLUMNS,
        [1, "100 Fictional Lane", "Pat Imaginary", "pat@example.com", datetime.date(2019, 6, 14)],
        [2, "102 Fictional Lane", "Sam Madeup", "", datetime.datetime(2021, 3, 2, 0, 0), "trust"],
    ]
    _upload(client, _xlsx_file(rows))
    record = _record()
    assert record.kind == "xlsx"
    _import(client, record)
    with system_access("t"):
        assert Ownership.objects.get(lot__number=1).start_date == datetime.date(2019, 6, 14)
        assert Ownership.objects.get(lot__number=2).ownership_type == "trust"


def test_xlsx_reads_only_the_first_sheet():
    data = _xlsx_bytes(
        [ri.COLUMNS, [3, "104 Fictional Lane", "Kim Notreal", "", "2020-01-01"]],
        sheet_first=["not", "a", "register"],
    )
    sheet = ri.read_xlsx(data)
    assert [r["owner_name"] for r in sheet.rows] == ["Kim Notreal"]


def test_xlsx_cached_spreadsheet_errors_are_reported_by_row():
    data = _xlsx_bytes([ri.COLUMNS, [3, "104 Fictional Lane", "#REF!", "", "2020-01-01"]])
    result = ri.check(ri.read_xlsx(data))
    assert any(p.row == 2 and "#REF!" in p.message for p in result.problems)


MAIL_NJ = ["1 Box St", "Town", "NJ", 8001]


def test_xlsx_zip_code_keeps_its_leading_zero():
    data = _xlsx_bytes(
        [
            ri.ALL_COLUMNS,
            [4, "106 Fictional Lane", "Lee Pretend", "", "2020-01-01", "", "", *MAIL_NJ],
        ]
    )
    result = ri.check(ri.read_xlsx(data))
    assert result.ok
    assert result.lots[4]["owners"][0]["mailing"]["zip"] == "08001"


@pytest.mark.parametrize(
    "row, column, words",
    [
        ("x,100 Fictional Lane,Pat,,2019-06-14,", "lot", "whole number"),
        ("1,,Pat,,2019-06-14,", "street_address", "street_address is empty"),
        ("1,100 Fictional Lane,,,2019-06-14,", "owner_name", "owner_name is empty"),
        ("1,100 Fictional Lane,Pat,not-an-email,2019-06-14,", "owner_email", "not a valid"),
        ("1,100 Fictional Lane,Pat,,2019-13-01,", "owner_since", "YYYY-MM-DD"),
        ("1,100 Fictional Lane,Pat,,14/06/2019,", "owner_since", "YYYY-MM-DD"),
        ("1,100 Fictional Lane,Pat,,2999-01-01,", "owner_since", "in the future"),
        ("1,100 Fictional Lane,Pat,,2019-06-14,castle", "ownership_type", "must be one of"),
        ("EXAMPLE 1,100 Fictional Lane,Pat,,2019-06-14,", "owner_name", "example row"),
        (
            "1,100 Fictional Lane,example \u2013 delete: Pat,,2019-06-14,",
            "owner_name",
            "example row",
        ),
    ],
)
def test_each_kind_of_problem_names_row_and_column(row, column, words):
    result = ri.check(ri.read_csv(f"{HEADER}\n{row}\n".encode()))
    assert len(result.problems) == 1
    problem = result.problems[0]
    assert problem.row == 2 and problem.column == column and words in problem.message


def test_contact_columns_are_checked():
    header = HEADER + ",owner_phone,mailing_street,mailing_city,mailing_state,mailing_zip"
    text = (
        f"{header}\n"
        "1,100 Fictional Lane,Pat,,2019-06-14,,12,,,,\n"
        "2,102 Fictional Lane,Sam,,2019-06-14,,,1 Box St,,ZZ,123\n"
    )
    problems = ri.check(ri.read_csv(text.encode())).problems
    assert [(p.row, p.column) for p in problems] == [(2, "owner_phone"), (3, "mailing_city")]
    assert "mailing_state" in problems[1].message and "mailing_zip" in problems[1].message


def test_duplicate_rows_two_addresses_and_repeated_emails():
    text = (
        f"{HEADER}\n"
        "1,100 Fictional Lane,Pat,pat@example.com,2019-06-14,\n"
        "1,100 Fictional Lane,Pat,pat@example.com,2019-06-14,\n"
        "1,999 Other Road,Kim,,2019-06-14,\n"
        "2,102 Fictional Lane,Sam,sam@example.com,2019-06-14,\n"
        "2,102 Fictional Lane,Sammy,SAM@example.com,2019-06-14,\n"
    )
    problems = [str(p) for p in ri.check(ri.read_csv(text.encode())).problems]
    assert "row 3: repeats row 2; delete one of them" in problems
    assert "row 4: lot 1 has two different addresses" in problems
    assert "lot 2: the same email is listed twice" in problems


def test_unknown_columns_are_reported_and_ignored():
    text = HEADER + ",favourite_colour\n1,100 Fictional Lane,Pat,,2019-06-14,,green\n"
    result = ri.check(ri.read_csv(text.encode()))
    assert result.ok and result.unknown_columns == ["favourite_colour"]


def test_missing_columns_and_too_many_rows():
    with pytest.raises(ri.RegisterError, match="Missing column"):
        ri.read_csv(b"lot,street_address\n1,x\n")
    rows = "".join(f"{n},{n} Fictional Lane,P{n},,2019-06-14,\n" for n in range(1, 2003))
    with pytest.raises(ri.RegisterError, match="Too many rows"):
        ri.read_csv((HEADER + "\n" + rows).encode())


def test_not_utf8_csv_is_refused():
    with pytest.raises(ri.RegisterError, match="UTF-8"):
        ri.read_csv((HEADER + "\n1,100 Caf\xe9 Lane,P,,2019-06-14,\n").encode("latin-1"))


def test_joint_owners_and_contact_details_are_recorded(client, board):
    header = HEADER + ",owner_phone,mailing_street,mailing_city,mailing_state,mailing_zip"
    text = (
        f"{header}\n"
        "5,110 Fictional Lane,Robin Unreal,robin@example.com,2018-01-05,,215-555-0100,"
        "PO Box 9,Exampleton,pa,19000\n"
        "5,110 Fictional Lane,Jo Unreal,,2018-01-05,,,,,,\n"
    )
    _upload(client, _csv_file(text))
    _import(client)
    with system_access("t"):
        robin = Person.objects.get(email="robin@example.com")
        assert ContactMethod.objects.filter(person=robin, value="215-555-0100").exists()
        profile = ContactProfile.objects.get(person=robin)
        assert profile.phone == "215-555-0100"
        assert (profile.mail_to_home, profile.mail_state, profile.mail_zip) == (
            False,
            "PA",
            "19000",
        )
        assert Ownership.objects.active().filter(lot__number=5).count() == 2


# --- Existing records and re-imports ------------------------------------------------------


def test_lots_with_current_owners_are_left_alone(client, board):
    lot = LotFactory(number=1)
    make_member(lots=[lot])
    _upload(client, _csv_file(GOOD))
    page = _check_page(client)
    assert page.context["result"].skip == [1]
    assert b"Already has owners: left alone" in page.content
    _import(client)
    with system_access("t"):
        assert Ownership.objects.active().filter(lot=lot).count() == 1


def test_existing_people_are_matched_by_email(client, board):
    with system_access("t"):
        known = Person.objects.create(
            display_name="Pat Imaginary", email="Pat.Imaginary@example.com"
        )
    _upload(client, _csv_file(GOOD))
    page = _check_page(client)
    assert page.context["counts"]["people_matched"] == 1
    assert b"Already in the portal" in page.content
    _import(client)
    with system_access("t"):
        assert Ownership.objects.get(lot__number=1).person == known
        assert Person.objects.filter(email__iexact="pat.imaginary@example.com").count() == 1


def test_reimporting_the_same_file_changes_nothing(client, board):
    _upload(client, _csv_file(GOOD))
    _import(client)
    with system_access("t"):
        before = (Lot.objects.count(), Person.objects.count(), Ownership.objects.count())
    _upload(client, _csv_file(GOOD))
    page = _check_page(client)
    assert page.context["nothing_to_do"]
    assert b"Nothing to import" in page.content
    with system_access("t"):
        after = (Lot.objects.count(), Person.objects.count(), Ownership.objects.count())
    assert before == after


def test_an_upload_imports_once(client, board):
    _upload(client, _csv_file(GOOD))
    record = _record()
    expect = _check_page(client, record).context["fingerprint"]
    client.post(reverse("register_import_run", args=[record.pk]), {"expect": expect})
    response = client.post(
        reverse("register_import_run", args=[record.pk]), {"expect": expect}, follow=True
    )
    assert b"already been imported" in response.content
    with system_access("t"):
        assert Ownership.objects.count() == 3


def test_a_register_that_changed_since_the_check_is_not_written(client, board):
    _upload(client, _csv_file(GOOD))
    record = _record()
    expect = _check_page(client, record).context["fingerprint"]
    with system_access("t"):
        Lot.objects.create(number=1, street_address="100 Fictional Lane")
    response = client.post(
        reverse("register_import_run", args=[record.pk]), {"expect": expect}, follow=True
    )
    assert b"changed since it was checked" in response.content
    with system_access("t"):
        assert not Ownership.objects.exists()


def test_a_file_with_problems_cannot_be_imported(client, board):
    _upload(client, _csv_file(HEADER + "\nx,100 Fictional Lane,Pat,,2019-06-14,\n"))
    record = _record()
    page = _check_page(client, record)
    assert b"1 problem to fix" in page.content
    assert reverse("register_import_run", args=[record.pk]).encode() not in page.content
    client.post(reverse("register_import_run", args=[record.pk]), {"expect": "0-0-0-0"})
    record.refresh_from_db()
    assert record.imported_at is None
    with system_access("t"):
        assert not Lot.objects.exists()


def test_the_importers_own_home_is_left_for_another_board_member(client):
    importer = make_member(board=True)
    client.force_login(importer)
    with system_access("t"):
        mine = Person.objects.get(user=importer)
    text = f"{HEADER}\n9,118 Fictional Lane,Me Myself,{mine.email},2018-01-05,\n"
    _upload(client, _csv_file(text))
    page = _check_page(client)
    assert page.context["result"].own_lots == [9]
    assert b"left for another board member" in page.content
    _import(client)
    with system_access("t"):
        assert Lot.objects.filter(number=9).exists()
        assert not Ownership.objects.filter(lot__number=9).exists()
    # Another board member then imports the same file: the owner is recorded.
    other = make_member(board=True)
    client.force_login(other)
    _upload(client, _csv_file(text))
    _import(client)
    with system_access("t"):
        assert Ownership.objects.active().get(lot__number=9).person == mine


# --- Refused files ---------------------------------------------------------------------


def test_wrong_type_is_refused(client, board):
    response = _upload(client, SimpleUploadedFile("register.pdf", b"%PDF-1.4 x"))
    assert response.status_code == 400
    assert b"Only these file types are accepted: csv, xlsx" in response.content
    assert not RegisterImport.objects.exists()


def test_a_renamed_binary_is_refused(client, board):
    response = _upload(client, SimpleUploadedFile("register.xlsx", b"MZ\x90\x00" + b"\x00" * 50))
    assert response.status_code == 400
    assert not RegisterImport.objects.exists()


@pytest.mark.parametrize("name", ["register.xlsm", "register.xls", "register.xlsb"])
def test_macro_workbook_extensions_are_refused(client, board, name):
    data = _xlsx_bytes([ri.COLUMNS])
    response = _upload(client, SimpleUploadedFile(name, data))
    assert response.status_code == 400
    assert b"macros" in response.content
    assert not RegisterImport.objects.exists()


def test_xlsx_carrying_macros_is_refused(client, board):
    data = _xlsx_bytes([ri.COLUMNS], extra_members={"xl/vbaProject.bin": b"\x00" * 100})
    response = _upload(client, SimpleUploadedFile("register.xlsx", data))
    assert response.status_code == 400
    assert not RegisterImport.objects.exists()


def test_zip_bomb_is_refused_before_opening(client, board):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", b"\x00" * (60 * 1024 * 1024))
    data = buffer.getvalue()
    assert len(data) < ri.MAX_BYTES
    with pytest.raises(ri.RegisterError, match="too large when unpacked"):
        ri.read_xlsx(data)
    response = _upload(client, SimpleUploadedFile("register.xlsx", data))
    assert response.status_code == 400
    assert not RegisterImport.objects.exists()


def test_too_many_parts_is_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for n in range(ri.XLSX_MAX_MEMBERS + 1):
            archive.writestr(f"part{n}.xml", "x")
    with pytest.raises(ri.RegisterError, match="too many parts"):
        ri.read_xlsx(buffer.getvalue())


def test_oversize_file_is_refused_before_it_is_stored(client, board):
    big = (HEADER + "\n").encode() + b"1,a,b,,2019-06-14,\n" * 120_000  # ~2.3 MB
    assert ri.MAX_BYTES < len(big) < ri.MAX_BYTES + 1024 * 1024
    response = _upload(client, SimpleUploadedFile("register.csv", big))
    assert response.status_code == 413
    assert b"2 MB or smaller" in response.content
    assert not RegisterImport.objects.exists()


def test_declared_oversize_request_is_refused_before_reading(client, board):
    response = client.post(
        reverse("register_import"),
        data=b"x" * 10,
        content_type="multipart/form-data; boundary=zzz",
        CONTENT_LENGTH=str(ri.MAX_BYTES + 2 * 1024 * 1024),
    )
    assert response.status_code == 413
    assert not RegisterImport.objects.exists()


def test_no_file_chosen(client, board):
    response = client.post(reverse("register_import"), {})
    assert response.status_code == 400
    assert b"Choose the register file" in response.content


# --- The malware-scan gate ----------------------------------------------------------------


def test_infected_file_is_refused_audited_and_not_stored(client, board, monkeypatch):
    monkeypatch.setattr(ri, "scan", lambda f: (ScanResult.INFECTED, "test"))
    response = _upload(client, _csv_file(GOOD))
    assert response.status_code == 400
    assert b"flagged by the malware scanner" in response.content
    assert not RegisterImport.objects.exists()
    event = AuditEvent.objects.get(action="register.upload_blocked")
    assert event.metadata == {"reason": "malware"} and event.actor == board


def test_scanner_unavailable_blocks_the_import_until_checked_again(client, board, monkeypatch):
    monkeypatch.setattr(ri, "scan", lambda f: (ScanResult.ERROR, "clamd"))
    _upload(client, _csv_file(GOOD))
    record = _record()
    page = _check_page(client, record)
    assert b"Checking the file for malware" in page.content
    assert b"could not check the file" in page.content
    assert "fingerprint" not in page.context
    response = client.post(
        reverse("register_import_run", args=[record.pk]), {"expect": "2-2-3-3"}, follow=True
    )
    assert b"has not passed the malware check" in response.content
    with system_access("t"):
        assert not Lot.objects.exists()
    assert client.get(reverse("register_import_file", args=[record.pk])).status_code == 404
    # The scanner is back: Check again lets the import go ahead.
    monkeypatch.setattr(ri, "scan", lambda f: (ScanResult.CLEAN, "clamd"))
    client.post(reverse("register_import_rescan", args=[record.pk]))
    assert AuditEvent.objects.get(action="register.file_rescanned").metadata == {"scan": "clean"}
    _import(client, record)
    with system_access("t"):
        assert Lot.objects.count() == 2


@override_settings(DOCUMENT_REQUIRE_CLEAN_SCAN=True)
def test_unscanned_files_wait_when_a_clean_scan_is_required(client, board, monkeypatch):
    monkeypatch.setattr(ri, "scan", lambda f: (ScanResult.UNSCANNED, "none"))
    _upload(client, _csv_file(GOOD))
    page = _check_page(client)
    assert b"not been confirmed clean" in page.content
    assert "fingerprint" not in page.context


# --- Who may use it ---------------------------------------------------------------------


def _manager():
    return make_member(roles=[Role.MANAGER])


def _sysadmin():
    return make_member(roles=[Role.SYSADMIN])


def _owner():
    return make_member(lots=[LotFactory()])


def _tenant():
    from community import services
    from community.models import ContactKind

    lot = LotFactory()
    make_member(lots=[lot])
    with system_access("t"):
        account = Account.objects.get(lot=lot, closed_on__isnull=True)
    user = UserFactory()
    person = services.record_person(display_name="Tenant person", actor=SYSTEM)
    with system_access("t"):
        Person.objects.filter(pk=person.pk).update(user=user)
    enroll_totp(user)
    services.add_account_contact(account, person=person, kind=ContactKind.TENANT, actor=SYSTEM)
    return user


def _stored(board_user) -> RegisterImport:
    return ri.upload(_csv_file(GOOD), actor=board_user)


@pytest.mark.parametrize("who", [_manager, _sysadmin, _owner, _tenant])
def test_only_the_board_may_import_or_see_the_file(client, who):
    record = _stored(make_member(board=True))
    client.force_login(who())
    for name, method in [
        ("register_import", "get"),
        ("register_import", "post"),
        ("register_template", "get"),
        ("register_import_detail", "get"),
        ("register_import_file", "get"),
        ("register_import_run", "post"),
        ("register_import_rescan", "post"),
        ("register_import_remove", "post"),
    ]:
        if name == "register_template":
            url = reverse(name, args=["xlsx"])
        elif name == "register_import":
            url = reverse(name)
        else:
            url = reverse(name, args=[record.pk])
        response = getattr(client, method)(url, {"expect": "x"} if method == "post" else None)
        assert response.status_code == 403, (who.__name__, name)
    record.refresh_from_db()
    assert record.has_file and record.imported_at is None
    with system_access("t"):
        assert not Lot.objects.filter(street_address="100 Fictional Lane").exists()


def test_anonymous_visitors_go_to_sign_in(client):
    record = _stored(make_member(board=True))
    for url in (
        reverse("register_import"),
        reverse("register_template", args=["csv"]),
        reverse("register_import_detail", args=[record.pk]),
        reverse("register_import_file", args=[record.pk]),
    ):
        response = client.get(url)
        assert response.status_code == 302
        assert response["Location"].startswith(reverse("account_login"))
    response = client.post(reverse("register_import"), {"file": _csv_file(GOOD)})
    assert response.status_code == 302 and RegisterImport.objects.count() == 1


def test_the_board_downloads_the_file_privately(client, board):
    _upload(client, _csv_file(GOOD))
    record = _record()
    response = client.get(reverse("register_import_file", args=[record.pk]))
    assert response.status_code == 200
    assert b"".join(response.streaming_content).decode() == GOOD
    assert response["Cache-Control"] == "private, no-store"
    assert response["Content-Disposition"].startswith("attachment")
    assert response["Content-Disposition"].rstrip('"').endswith(".csv")
    assert record.file.name.startswith("register-imports/")
    assert "register" not in record.file.name.split("/", 1)[1]  # random key


def test_the_menu_and_empty_state_lead_to_the_import(client, board):
    page = client.get(reverse("lot_list")).content.decode()
    assert reverse("register_import") in page and "Import homes and owners" in page
    page = client.get(reverse("setup_step", args=["people"])).content.decode()
    assert reverse("register_import") in page


def test_owners_get_no_import_link_on_homes_and_owners(client):
    user = make_member(roles=[Role.MANAGER])
    client.force_login(user)
    page = client.get(reverse("lot_list")).content.decode()
    assert reverse("register_import") not in page


def test_rescan_files_covers_kept_register_files(client, board):
    from unittest import mock

    _upload(client, _csv_file(GOOD))
    record = _record()
    sysadmin = make_member(roles=[Role.SYSADMIN])
    with mock.patch(
        "documents.management.commands.rescan_files.scan", return_value=("infected", "clamd")
    ):
        call_command(
            "rescan_files", actor=sysadmin.email, reason="signatures updated", stdout=StringIO()
        )
    assert ri.latest_scan(record) == "infected"
    assert client.get(reverse("register_import_file", args=[record.pk])).status_code == 404
    assert "fingerprint" not in _check_page(client, record).context


# --- Deleting and purging the file ----------------------------------------------------------


def test_board_deletes_the_file_and_the_record_stays(client, board):
    _upload(client, _csv_file(GOOD))
    record = _record()
    _import(client, record)
    name = record.file.name
    storage = record.file.storage
    assert storage.exists(name)
    client.post(reverse("register_import_remove", args=[record.pk]))
    record.refresh_from_db()
    assert not record.has_file and record.file_removed_by == board
    assert not storage.exists(name)
    assert record.counts["owners"] == 3
    assert client.get(reverse("register_import_file", args=[record.pk])).status_code == 404
    event = AuditEvent.objects.get(action="register.file_removed")
    assert event.metadata == {"reason": "board"}
    with pytest.raises(PermissionError):
        record.delete()


def test_old_files_are_purged_nightly(board):
    record = _stored(board)
    fresh = _stored(board)
    RegisterImport.objects.filter(pk=record.pk).update(
        uploaded_at=timezone.now() - datetime.timedelta(days=31)
    )
    from community.tasks import register_import_files

    assert register_import_files(timestamp=0) == 1
    record.refresh_from_db()
    fresh.refresh_from_db()
    assert not record.has_file and fresh.has_file
    event = AuditEvent.objects.get(action="register.file_removed")
    assert event.metadata == {"reason": "retention"} and event.actor is None


# --- Audit ------------------------------------------------------------------------------


def test_audit_metadata_is_counts_only(client, board):
    header = HEADER + ",owner_phone,mailing_street,mailing_city,mailing_state,mailing_zip"
    text = (
        f"{header}\n"
        "1,100 Fictional Lane,Pat Imaginary,pat.imaginary@example.com,2019-06-14,,"
        "215-555-0100,PO Box 9,Exampleton,PA,19000\n"
    )
    _upload(client, _csv_file(text))
    _import(client)
    imported = AuditEvent.objects.get(action="register.imported")
    assert set(imported.metadata) == {
        "lots",
        "owners",
        "people_created",
        "lots_created",
        "lots_left_alone",
        "people_matched",
        "rows",
    }
    assert all(isinstance(v, int) for v in imported.metadata.values())
    assert imported.actor == board
    uploaded = AuditEvent.objects.get(action="register.file_uploaded")
    assert set(uploaded.metadata) == {"kind", "scan", "size"}
    everything = str(list(AuditEvent.objects.values_list("metadata", flat=True)))
    for secret in ("Pat Imaginary", "pat.imaginary", "Fictional", "Exampleton", "555-0100"):
        assert secret not in everything
    # The usual per-record trail, as the board member.
    for action in ("lot.recorded", "person.recorded", "ownership.recorded"):
        assert AuditEvent.objects.filter(action=action, actor=board).exists(), action


# --- The management command ----------------------------------------------------------------


def test_command_prints_the_same_summary(tmp_path):
    lot = LotFactory(number=1)
    make_member(lots=[lot])
    path = tmp_path / "register.csv"
    path.write_text(GOOD, encoding="utf-8")
    out = StringIO()
    call_command("import_register", str(path), stdout=out)
    assert out.getvalue().splitlines() == [
        "Lots to create: 1. Lots to fill: 1. Lots skipped (already have owners): 1. "
        "Owners to record: 2.",
        "  lot 1: already has current owners; left as is",
        "Check only. Run again with --commit to write.",
    ]
    out = StringIO()
    call_command("import_register", str(path), "--commit", stdout=out)
    assert out.getvalue().splitlines()[-1] == "Register loaded."
    event = AuditEvent.objects.get(action="register.imported")
    assert event.metadata == {"lots": 1, "owners": 2, "people_created": 2}
    with system_access("t"):
        assert Ownership.objects.active().filter(lot__number=2).count() == 2


def test_command_reads_xlsx_too(tmp_path):
    path = tmp_path / "register.xlsx"
    path.write_bytes(
        _xlsx_bytes([ri.COLUMNS, [7, "112 Fictional Lane", "Kim Notreal", "", "2020-02-02"]])
    )
    call_command("import_register", str(path), "--commit", stdout=StringIO())
    with system_access("t"):
        assert Lot.objects.get(number=7).street_address == "112 Fictional Lane"


def test_the_upload_still_needs_the_csrf_token():
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(make_member(board=True))
    response = csrf_client.post(reverse("register_import"), {"file": _csv_file(GOOD)})
    assert response.status_code == 403
    assert not RegisterImport.objects.exists()
