"""Paper mail: mail merge for letters, envelopes and labels (D55). Synthetic data only."""

import datetime
import re
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditEvent
from comms import papermail
from comms.models import ImbSerial, Mailing, MailingEvent, MailPiece, PaperMailSettings
from community import services as community
from community.models import Account, ContactKind, ContactProfile, Ownership, Person, Role
from community.tests.factories import LotFactory, PersonFactory, days_ago, make_member
from core import pdf
from core.append_only import AppendOnlyError
from core.policy import SYSTEM
from core.scoping import system_access
from core.tests.pdftext import pdf_text

pytestmark = pytest.mark.django_db


def _own(person, lot, kind="sole"):
    return community.record_ownership(
        person=person, lot=lot, start_date=days_ago(100), ownership_type=kind, actor=SYSTEM
    )


def _profile(person, **fields):
    with system_access("test"):
        profile, _ = ContactProfile.objects.get_or_create(person=person)
        for k, v in fields.items():
            setattr(profile, k, v)
        profile.save()
    return profile


@pytest.fixture
def w():
    board = make_member(board=True)
    manager = make_member(roles=[Role.MANAGER])
    lots = [
        LotFactory(street_address=f"{n} Example Lane\nAnytown, PA 19380") for n in (10, 20, 30, 40)
    ]
    # Home 10: two owners, same address (one letter); neither consented.
    a1, a2 = PersonFactory(display_name="Pat Alpha"), PersonFactory(display_name="Sam Alpha")
    _own(a1, lots[0], "joint")
    _own(a2, lots[0], "joint")
    # Home 20: co-owners with different mailing addresses (two letters).
    b1, b2 = PersonFactory(display_name="Lee Bravo"), PersonFactory(display_name="Kim Bravo")
    _own(b1, lots[1], "joint")
    _own(b2, lots[1], "joint")
    _profile(
        b2,
        mail_to_home=False,
        mail_street="9 Faraway Road",
        mail_city="Elsewhere",
        mail_state="NJ",
        mail_zip="08001-1234",
        consent_changed_at=timezone.now(),
    )
    # Home 30: consented to email (no paper); a trust owns home 40 and
    # withdrew its consent.
    c1 = PersonFactory(display_name="Chris Charlie")
    _own(c1, lots[2])
    _profile(c1, electronic_notice_consent=True, consent_changed_at=timezone.now())
    trust = PersonFactory(display_name="Delta Family Trust")
    _own(trust, lots[3], "trust")
    _profile(trust, electronic_notice_consent=False, consent_changed_at=timezone.now())
    # A former owner of home 30 and a tenant there never get mail.
    former = PersonFactory(display_name="Old Former")
    own = _own(former, lots[2])
    with mock.patch("community.tasks.notify_board.defer"):
        community.end_ownership(own, end_date=days_ago(1), actor=SYSTEM)
    tenant = PersonFactory(display_name="Terry Tenant")
    with system_access("t"):
        account = Account.objects.filter(lot=lots[2], closed_on__isnull=True).first()
    community.add_account_contact(account, person=tenant, kind=ContactKind.TENANT, actor=board)
    owner = make_member(lots=[LotFactory(street_address="99 Owner Way")])
    _profile(
        Person.objects.unscoped("t").get(user=owner),
        electronic_notice_consent=True,
        consent_changed_at=timezone.now(),
    )
    return {"board": board, "manager": manager, "lots": lots, "owner": owner, "trust": trust}


def _names(groups):
    return sorted(g["owner_names"] for g in groups)


def _mailing(w, **kw):
    args = {
        "actor": w["board"],
        "defaults": papermail.source_defaults(),
        "subject": "Pool opening",
        "body": "Hello {owner_names} at {address}.\n\nAccount {account_number}, {date}.",
    }
    args.update(kw)
    return papermail.create_mailing(**args)


# --- Recipients -------------------------------------------------------------------------


def test_who_gets_paper_follows_consent_and_groups_by_address(w):
    groups = papermail.who_gets_paper()
    assert _names(groups) == [
        "Delta Family Trust",
        "Kim Bravo",
        "Lee Bravo",
        "Pat Alpha & Sam Alpha",
    ]
    assert papermail.counts(groups) == {"letters": 4, "homes": 3, "owners": 5, "no_address": 0}
    kim = next(g for g in groups if g["owner_names"] == "Kim Bravo")
    assert kim["address"].startswith("9 Faraway Road") and "08001-1234" in kim["address"]
    everyone = papermail.who_gets_paper(everyone=True)
    assert "Chris Charlie" in _names(everyone)
    assert not {"Old Former", "Terry Tenant"} & set(_names(everyone))


def test_consent_withdrawn_or_without_email_means_paper(w):
    c1 = Person.objects.unscoped("t").get(display_name="Chris Charlie")
    _profile(c1, electronic_notice_consent=False)
    assert "Chris Charlie" in _names(papermail.who_gets_paper())
    _profile(c1, electronic_notice_consent=True)
    Person.objects.unscoped("t").filter(pk=c1.pk).update(email="")
    assert "Chris Charlie" in _names(papermail.who_gets_paper())


def test_one_home_only(w):
    groups = papermail.who_gets_paper(lot_id=w["lots"][1].pk)
    assert _names(groups) == ["Kim Bravo", "Lee Bravo"]
    assert _names(papermail.who_gets_paper(everyone=True, lot_id=w["lots"][2].pk)) == [
        "Chris Charlie"
    ]


# --- Wording ------------------------------------------------------------------------------


def test_placeholders_fill_per_letter(w):
    mailing = _mailing(w)
    letters = papermail._letters(mailing)
    alpha = next(lt for lt in letters if lt["owner_names"] == "Pat Alpha & Sam Alpha")
    values = papermail.letter_values(alpha, mailing)
    assert values["salutation"] == "Pat Alpha and Sam Alpha"
    assert values["address"] == w["lots"][0].legal_label
    assert values["mailing_address"] == "10 Example Lane, Anytown, PA 19380"
    assert values["date"] == papermail._long(timezone.localdate())
    text = papermail.fill(mailing.body, values)
    assert "{" not in text and w["lots"][0].legal_label in text
    assert papermail.fill("{unknown} {date}", values).startswith("{unknown} ")


def test_meeting_notice_is_prefilled_with_join_link(w):
    from governance import services as governance

    meeting = governance.create_meeting(
        title="Annual meeting",
        kind="annual",
        location="Clubhouse",
        join_url="https://meet.example.com/j/123",
        starts_at=timezone.now() + datetime.timedelta(days=20),
        actor=w["board"],
    )
    defaults = papermail.source_defaults(meeting=meeting)
    assert (
        defaults["values"]["meeting_where"]
        == "Clubhouse, or online: https://meet.example.com/j/123"
    )
    assert f"/meetings/{meeting.pk}/" in defaults["values"]["agenda_link"]
    mailing = _mailing(w, defaults=defaults, subject=defaults["subject"], body=defaults["body"])
    data, _pages = papermail.letters_pdf(mailing, duplex=False)
    text = pdf_text(data)
    assert "RE: Notice of annual owners' meeting: Annual meeting" in text
    assert "https://meet.example.com/j/123" in text
    executive = governance.create_meeting(
        title="Exec",
        kind="executive",
        location="Room",
        starts_at=timezone.now() + datetime.timedelta(days=2),
        actor=w["board"],
    )
    with pytest.raises(papermail.PaperMailError):
        papermail.source_defaults(meeting=executive)


def test_budget_and_template_sources(w):
    from ledger import services as ledger

    budget = ledger.create_budget(fiscal_year=2031, actor=w["board"])
    defaults = papermail.source_defaults(budget=budget, stage="proposed")
    assert defaults["values"]["budget"] == "FY2031 budget"
    template = papermail.source_defaults(template="first")
    assert "{amount}" in template["body"]
    mailing = _mailing(w, defaults=template, subject=template["subject"], body=template["body"])
    text = pdf_text(papermail.letters_pdf(mailing, duplex=False)[0])
    assert "$0.00" in text and "{amount}" not in text
    with pytest.raises(papermail.PaperMailError):
        papermail.source_defaults(template="statement")


# --- PDFs ---------------------------------------------------------------------------------


def _pages(data: bytes) -> int:
    return len(re.findall(rb"/Type /Page ", data))


def test_letters_one_per_group_with_duplex_padding(w):
    mailing = _mailing(w)
    data, pages = papermail.letters_pdf(mailing, duplex=False)
    assert pages == _pages(data) == 4
    data, pages = papermail.letters_pdf(mailing, duplex=True)
    assert pages == _pages(data) == 8  # a blank back after each one-page letter
    _first, pages = papermail.letters_pdf(mailing, duplex=False, first_only=True)
    assert pages == 1
    text = pdf_text(data)
    assert "Dear Pat Alpha and Sam Alpha:" in text and "9 Faraway Road" in text
    long = _mailing(w, body="\n\n".join(["A long paragraph of words " * 40] * 6))
    data, pages = papermail.letters_pdf(long, duplex=True)
    assert pages % 2 == 0 and pages > 8


def test_recipient_block_position_is_configurable(w):
    mailing = _mailing(w, lot_id=w["lots"][0].pk)

    def name_position(data):
        m = re.search(rb"BT\n([\d.]+) ([\d.]+) Td\n/F1 10.5 Tf\n\(Pat Alpha & Sam Alpha\) Tj", data)
        return float(m.group(1)), float(m.group(2))

    x, y = name_position(papermail.letters_pdf(mailing, duplex=False)[0])
    assert x == 72.0 and abs((792 - y) - (2.13 * 72 + 9.5)) < 0.5
    papermail.save_settings(actor=w["manager"], address_left_in="1.25", address_top_in="2.50")
    x2, y2 = name_position(papermail.letters_pdf(mailing, duplex=False)[0])
    assert x2 == 90.0 and round(y - y2, 2) == round(0.37 * 72, 2)
    with pytest.raises(papermail.PaperMailError):
        papermail.save_settings(actor=w["board"], address_top_in="9")


def test_double_window_prints_the_return_address(w):
    from community.models import CommunityProfile

    profile = CommunityProfile.get()
    profile.letterhead = (
        "Example Farms HOA\nc/o Example Management\n1 Office Park\ninfo@example.com"
    )
    profile.save()
    papermail.save_settings(actor=w["board"], window="double")
    mailing = _mailing(w, lot_id=w["lots"][0].pk)
    text = pdf_text(papermail.letters_pdf(mailing, duplex=False)[0])
    assert "c/o Example Management" in text and "info@example.com" not in text


def test_envelopes_and_labels(w):
    mailing = _mailing(w)
    data, pages = papermail.envelopes_pdf(mailing)
    assert pages == _pages(data) == 4
    assert b"/MediaBox [0 0 684 297]" in data
    data, pages = papermail.labels_pdf(mailing, start=29)
    assert pages == _pages(data) == 2  # labels 29 and 30, then two on a new sheet
    assert pdf_text(data).count("Example Lane") + pdf_text(data).count("Faraway") == 4
    with pytest.raises(papermail.PaperMailError):
        papermail.labels_pdf(mailing, start=31)
    for kind in ("letter", "envelope", "labels"):
        assert papermail.test_pdf(kind).startswith(b"%PDF")


def test_hostile_names_and_addresses_are_escaped(w):
    # Non-Latin-1 text and a right-to-left override (written as chr() so the
    # source itself holds no bidirectional control character).
    evil = PersonFactory(display_name="Eve (x) Tj ) \\ ET 中文 " + chr(0x202E))
    lot = LotFactory(street_address="1 Inject) Tj ET BT (Road\nTown, PA 19380")
    _own(evil, lot)
    mailing = _mailing(w, lot_id=lot.pk, body="Body with ) and ( and \\ and emoji \U0001f600.")
    for data in (
        papermail.letters_pdf(mailing, duplex=False)[0],
        papermail.envelopes_pdf(mailing)[0],
        papermail.labels_pdf(mailing)[0],
    ):
        raw = data.decode("latin-1")
        assert "Eve \\(x\\) Tj \\) \\\\ ET ?? ?" in raw
        assert "(Road" not in raw.replace("\\(Road", "")
        text = pdf_text(data)
        assert "1 Inject) Tj ET BT (Road" in text
    assert "emoji ?." in pdf_text(papermail.letters_pdf(mailing, duplex=False)[0])


def test_pdf_placed_items_refuse_bad_numbers():
    with pytest.raises(ValueError):
        pdf.Text("72", 10, "x")
    with pytest.raises(ValueError):
        pdf.Text(float("nan"), 10, "x")
    with pytest.raises(ValueError):
        pdf.render_placed([[pdf.Text(5000, 10, "x")]])
    with pytest.raises(ValueError):
        pdf.Bars(0, 0, "FADT" * 16)
    assert pdf.safe_text("caf\u00e9 \u2013 ok \u4e2d") == "café \x96 ok ?"


# --- Intelligent Mail barcode ------------------------------------------------------------


def test_barcode_only_when_switched_on_with_a_mailer_id(w):
    mailing = _mailing(w)
    data, _ = papermail.letters_pdf(mailing, duplex=False)
    assert b"0 0 0 rg" not in data and not MailPiece.objects.unscoped("t").exists()
    with pytest.raises(papermail.PaperMailError):
        papermail.save_settings(actor=w["board"], imb_enabled=True)
    with pytest.raises(papermail.PaperMailError):
        papermail.save_settings(actor=w["board"], mailer_id="12345")
    papermail.save_settings(actor=w["board"], mailer_id="123456", imb_enabled=True)
    data, _ = papermail.letters_pdf(mailing, duplex=False)
    # 65 bars per letter, drawn inside the address block above the name.
    assert data.count(b" re\n") >= 4 * 65
    pieces = list(MailPiece.objects.unscoped("t").filter(mailing=mailing).order_by("group"))
    assert [p.serial for p in pieces] == [1, 2, 3, 4]
    assert {p.routing for p in pieces} == {"19380", "080011234"}
    assert pieces[0].tracking == "00310123456000000001"
    # Reprinting (letters or envelopes) reuses the same numbers.
    papermail.envelopes_pdf(mailing)
    assert MailPiece.objects.unscoped("t").filter(mailing=mailing).count() == 4
    second = _mailing(w)
    papermail.letters_pdf(second, duplex=False)
    serials = list(MailPiece.objects.unscoped("t").values_list("serial", flat=True))
    assert len(serials) == len(set(serials)) == 8
    assert ImbSerial.objects.get(mailer_id="123456").next_serial == 9
    # Labels never carry it (too long for the label).
    assert b"0 0 0 rg" not in papermail.labels_pdf(second)[0]
    with pytest.raises(AppendOnlyError):
        MailPiece.objects.unscoped("t").update(serial=99)


def test_barcode_sits_in_the_address_block(w):
    papermail.save_settings(actor=w["board"], mailer_id="123456789", imb_enabled=True)
    mailing = _mailing(w, lot_id=w["lots"][0].pk)
    data, _ = papermail.letters_pdf(mailing, duplex=False)
    bars = [tuple(map(float, m)) for m in re.findall(rb"([\d.]+) ([\d.]+) 1.44 ([\d.]+) re", data)]
    assert len(bars) == 65
    xs, ys = [b[0] for b in bars], [b[1] for b in bars]
    top = 792 - 2.13 * 72
    assert min(xs) >= 72 and max(xs) <= 72 + 3 * 72
    assert top - 0.25 * 72 <= min(ys) and max(ys) <= top
    assert MailPiece.objects.unscoped("t").get().tracking.endswith("000001")


# --- Record, audit, meeting page, My items ----------------------------------------------


def test_mailing_is_append_only_and_audited_with_counts_only(w):
    mailing = _mailing(w)
    with pytest.raises(AppendOnlyError):
        mailing.save()
    with pytest.raises(AppendOnlyError):
        Mailing.objects.unscoped("t").filter(pk=mailing.pk).update(subject="x")
    event = AuditEvent.objects.get(action="mailing.created")
    assert event.metadata == {
        "kind": "letter",
        "audience": "paper",
        "letters": 4,
        "homes": 3,
        "consent_version": mailing.consent_version,
    }
    papermail.record_mailed(mailing, actor=w["manager"], mailed_on=timezone.localdate())
    with pytest.raises(papermail.PaperMailError):
        papermail.record_mailed(mailing, actor=w["board"], mailed_on=timezone.localdate())
    with pytest.raises(AppendOnlyError):
        MailingEvent.objects.unscoped("t").update(mailed_on=None)
    everything = str(list(AuditEvent.objects.filter(action__startswith="mailing.").values()))
    for secret in ("Alpha", "Bravo", "Example Lane", "Faraway", "Trust"):
        assert secret not in everything


def test_mailing_date_rules(w):
    mailing = _mailing(w)
    with pytest.raises(papermail.PaperMailError):
        papermail.record_mailed(
            mailing, actor=w["board"], mailed_on=timezone.localdate() + datetime.timedelta(days=1)
        )
    with pytest.raises(papermail.PaperMailError):
        papermail.record_mailed(mailing, actor=w["board"])


def test_meeting_page_and_my_items_until_marked(client, w):
    from core import my_items
    from governance import services as governance

    meeting = governance.create_meeting(
        title="Fall meeting",
        kind="board",
        location="Clubhouse",
        starts_at=timezone.now() + datetime.timedelta(days=15),
        actor=w["board"],
    )
    client.force_login(w["board"])
    page = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert "3 homes need a paper notice" in page and "Prepare paper notices" in page
    defaults = papermail.source_defaults(meeting=meeting)
    mailing = _mailing(w, defaults=defaults, subject=defaults["subject"], body=defaults["body"])
    titles = [i.title for i in my_items.items_for(w["manager"])]
    assert "Paper notices for Fall meeting not marked as mailed" in titles
    page = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert "not marked as mailed" in page
    day = timezone.localdate()
    response = client.post(reverse("paper_mail_mark", args=[mailing.pk]), {"mailed_on": day})
    assert response.status_code == 302
    page = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert f"Paper notices mailed {day:%b} {day.day}, {day.year} to 3 homes" in page
    assert not [i for i in my_items.items_for(w["board"]) if i.area == "Paper mail"]
    # Owners never see the paper notice panel.
    client.force_login(w["owner"])
    page = client.get(reverse("meeting_detail", args=[meeting.pk])).content.decode()
    assert 'data-testid="paper-notices"' not in page


# --- Pages and permissions ---------------------------------------------------------------


def test_board_flow_through_the_pages(client, w):
    client.force_login(w["manager"])
    page = client.get(reverse("paper_mail")).content.decode()
    assert "<h1>Paper mail</h1>" in page and "Pat Alpha &amp; Sam Alpha" in page
    form = {"subject": "Hello {owner_names}", "body": "Dear neighbors.", "audience": "paper"}
    response = client.post(reverse("paper_mail_new"), {**form, "step": "preview"})
    assert response.status_code == 200 and b"letter-preview" in response.content
    assert not Mailing.objects.unscoped("t").exists()
    response = client.post(reverse("paper_mail_new"), {**form, "step": "create"})
    mailing = Mailing.objects.unscoped("t").get()
    assert response["Location"] == reverse("paper_mail_detail", args=[mailing.pk])
    assert client.get(response["Location"]).status_code == 200
    for name, query in (
        ("paper_mail_letters", "?duplex=1"),
        ("paper_mail_envelopes", ""),
        ("paper_mail_labels", "?start=3"),
    ):
        response = client.get(reverse(name, args=[mailing.pk]) + query)
        assert response["Content-Type"] == "application/pdf"
        assert response["Cache-Control"] == "private, no-store"
    printed = AuditEvent.objects.filter(action="mailing.printed").values_list("metadata", flat=True)
    assert {m["format"] for m in printed} == {"letters", "envelopes", "labels"}
    response = client.post(reverse("paper_mail_new"), {**form, "subject": "", "step": "create"})
    assert response.status_code == 400 and b"form-errors" in response.content
    assert client.get(reverse("paper_mail_settings")).status_code == 200
    response = client.post(
        reverse("paper_mail_settings"),
        {
            "window": "single",
            "address_left_in": "1.00",
            "address_top_in": "2.13",
            "return_left_in": "0.50",
            "return_top_in": "0.63",
            "envelope_shift_x_in": "0",
            "envelope_shift_y_in": "0",
            "label_shift_x_in": "0",
            "label_shift_y_in": "0",
            "mailer_id": "",
            "barcode_id": "00",
            "service_type": "040",
            "imb_enabled": "on",
        },
    )
    assert response.status_code == 400 and b"Mailer ID" in response.content
    assert PaperMailSettings.get().imb_enabled is False
    assert client.get(reverse("paper_mail_test") + "?kind=envelope").status_code == 200


def _urls(mailing):
    return [
        ("get", reverse("paper_mail")),
        ("get", reverse("paper_mail_new")),
        ("post", reverse("paper_mail_new")),
        ("get", reverse("paper_mail_settings")),
        ("get", reverse("paper_mail_test")),
        ("get", reverse("paper_mail_detail", args=[mailing.pk])),
        ("get", reverse("paper_mail_letters", args=[mailing.pk])),
        ("get", reverse("paper_mail_envelopes", args=[mailing.pk])),
        ("get", reverse("paper_mail_labels", args=[mailing.pk])),
        ("post", reverse("paper_mail_mark", args=[mailing.pk])),
    ]


@pytest.mark.parametrize("who", ["owner", "tenant", "contact", "sysadmin", "auditor", "arc"])
def test_nobody_else_reaches_any_paper_mail_url(client, w, who):
    mailing = _mailing(w)
    if who == "owner":
        user = w["owner"]
    elif who in ("tenant", "contact"):
        user = make_member()
        person = Person.objects.unscoped("t").get(user=user)
        with system_access("t"):
            account = Account.objects.filter(lot=w["lots"][0], closed_on__isnull=True).first()
        community.add_account_contact(
            account,
            person=person,
            kind=ContactKind.TENANT if who == "tenant" else ContactKind.AUTHORIZED,
            actor=w["board"],
        )
    else:
        user = make_member(
            roles=[
                {"sysadmin": Role.SYSADMIN, "auditor": Role.AUDITOR, "arc": Role.ARC_REVIEWER}[who]
            ]
        )
    client.force_login(user)
    for method, url in _urls(mailing):
        response = getattr(client, method)(
            url, {"mailed_on": "2026-01-01"} if method == "post" else {}
        )
        assert response.status_code in (403, 404), (who, url, response.status_code)
    assert not MailingEvent.objects.unscoped("t").exists()
    assert not AuditEvent.objects.filter(action="mailing.printed").exists()


def test_anonymous_visitors_are_sent_to_sign_in(client, w):
    mailing = _mailing(w)
    for method, url in _urls(mailing):
        response = getattr(client, method)(url)
        assert response.status_code == 302 and "/accounts/login/" in response["Location"]


def test_owner_scope_never_returns_mailings(w):
    _mailing(w)
    assert not Mailing.objects.for_user(w["owner"]).exists()
    assert Mailing.objects.for_user(w["manager"]).count() == 1
    with pytest.raises(Exception):  # noqa: B017 - the scoping refusal
        list(Mailing.objects.all())


def test_ownership_ended_after_preparing_drops_nobody_silently(w):
    """The letters are for the people chosen when the mailing was made."""
    mailing = _mailing(w, lot_id=w["lots"][0].pk)
    with system_access("t"):
        own = Ownership.objects.filter(lot=w["lots"][0]).first()
    with mock.patch("community.tasks.notify_board.defer"):
        community.end_ownership(own, actor=SYSTEM)
    assert len(papermail._letters(mailing)) == 1
