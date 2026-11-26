"""What the monthly charge is called (D42): one setting, used on every page,
email, letter and new ledger entry; old entries keep their wording."""

import datetime
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.template import Context, Template
from django.urls import reverse
from django.utils import timezone

from community import services
from community.models import Account
from community.rules import REGISTRY, rule_origin, rules_needing_attention
from community.terms import charge_term, parse, pluralize, term_text
from community.tests.factories import LotFactory, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from ledger import allocation
from ledger import services as ledger
from ledger.models import EntryKind, LedgerEntry

pytestmark = pytest.mark.django_db
TODAY = timezone.localdate()
DASH = "\N{EN DASH}"


def _set_term(value: str, citation="Declaration §6.1 (QA)"):
    services.set_rule(key="charge.term", value=value, citation=citation, actor=SYSTEM)


def test_default_is_assessment_from_the_act_without_needing_attention():
    term = charge_term()
    assert (term.singular, term.plural) == ("Assessment", "Assessments")
    assert rule_origin("charge.term") == "pa_law"
    assert "charge.term" not in {s.key for s in rules_needing_attention()}


@pytest.mark.parametrize(
    ("raw", "singular", "plural", "lower"),
    [
        ("Assessment", "Assessment", "Assessments", "assessment"),
        ("Dues", "Dues", "Dues", "dues"),
        ("Fee", "Fee", "Fees", "fee"),
        ("Levy", "Levy", "Levies", "levy"),
        ("Charge / Chargez", "Charge", "Chargez", "charge"),
        ("HOA fee", "HOA fee", "HOA fees", "HOA fee"),
        ("", "Assessment", "Assessments", "assessment"),
    ],
    ids=["assessment", "dues", "fee", "levy", "slash", "acronym", "blank"],
)
def test_parse_and_plural(raw, singular, plural, lower):
    term = parse(raw)
    assert (term.singular, term.plural) == (singular, plural)
    assert term.variants()["term"] == lower


def test_pluralize_endings():
    assert pluralize("Box") == "Boxes" and pluralize("Day") == "Days"


def test_invalid_term_is_refused():
    with pytest.raises(services.CommunityError):
        _set_term("<script>")
    with pytest.raises(services.CommunityError):
        _set_term("x" * 80)


def test_changing_the_term_changes_labels_and_new_entries_not_old_ones(client):
    board = make_member(board=True)
    services.set_rule(key="dues.monthly_cents", value="4500", citation="Decl §6", actor=SYSTEM)
    lot = LotFactory()
    owner = make_member(lots=[lot])
    prev = (TODAY.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
    ledger.post_monthly_assessments(year=prev.year, month=prev.month, actor=SYSTEM)
    with system_access("t"):
        old = LedgerEntry.objects.get(lot=lot, kind=EntryKind.ASSESSMENT)
    assert old.description == f"Assessment {DASH} {prev:%B %Y}"  # WT-003: one name

    _set_term("Dues")
    ledger.post_monthly_assessments(year=TODAY.year, month=TODAY.month, actor=SYSTEM)
    with system_access("t"):
        old.refresh_from_db()
        new = LedgerEntry.objects.get(lot=lot, kind=EntryKind.ASSESSMENT, period=f"{TODAY:%Y-%m}")
    assert new.description == f"Dues {DASH} {TODAY:%B %Y}"
    assert old.description == f"Assessment {DASH} {prev:%B %Y}"  # append-only, untouched
    assert allocation.charge_title(new) == f"Dues {DASH} {TODAY:%B %Y}"

    client.force_login(board)
    page = client.get(reverse("community_settings")).content.decode()
    assert "Monthly dues per home" in page and "<summary><h2>Dues</h2>" in page
    assert "Monthly assessment per home" not in page
    client.force_login(owner)
    page = client.get(reverse("my_account")).content.decode()
    assert "Dues are charged on the 1st" in page

    _set_term("Fee")
    client.force_login(board)
    page = client.get(reverse("community_settings")).content.decode()
    assert "Monthly fee per home" in page and "<summary><h2>Fees</h2>" in page
    assert REGISTRY["dues.monthly_cents"].shown_label == "Monthly fee per home"
    page = client.get(reverse("ledger_home")).content.decode()
    assert "Post the monthly fee" in page


def test_default_wording_on_pages_and_upcoming_charges(client):
    services.set_rule(key="dues.monthly_cents", value="4500", citation="Decl §6", actor=SYSTEM)
    lot = LotFactory()
    owner = make_member(lots=[lot])
    client.force_login(owner)
    page = client.get(reverse("my_account")).content.decode()
    assert "Assessments are charged on the 1st" in page
    upcoming = ledger.upcoming_charges(0, lot_id=lot.pk)
    assert upcoming and upcoming[0].description.startswith(f"Assessment {DASH} ")
    with system_access("t"):
        assert Account.objects.filter(lot=lot).exists()


def test_letters_and_statement_use_the_term():
    from ledger import letters

    _set_term("Dues")
    assert letters.template("second").subject.startswith("DELINQUENT ASSOCIATION DUES")
    assert "monthly dues" in letters.template("first").body
    _set_term("Assessment")
    assert "ASSESSMENTS" in letters.template("second").subject


def test_template_tags():
    _set_term("Levy")
    out = Template(
        '{% load portal %}{% term %}|{% term "terms" %}|{% with_term "Pay the {term} now" %}'
    ).render(Context({}))
    assert out == "Levy|levies|Pay the levy now"
    assert term_text("No placeholders") == "No placeholders"


# --- Guard: no hard-coded "dues" in what people see ------------------------------------

# Templates that may name "dues" literally (none today). Add a path only with
# a reason: everything people read takes the board's word (D42).
ALLOWED: set[str] = set()
_TAGS = re.compile(r"{%.*?%}|{{.*?}}|{#.*?#}", re.S)
_WORD = re.compile(r"(?<![\w.-])dues(?![\w-])", re.I)


def test_no_template_hard_codes_dues():
    root = Path(settings.BASE_DIR) / "templates"
    found = []
    for path in root.rglob("*.html"):
        rel = str(path.relative_to(root))
        if rel in ALLOWED:
            continue
        text = _TAGS.sub("", path.read_text())
        if _WORD.search(text):
            found.append(rel)
    assert not found, f"Use {{% term %}} instead of 'dues' in: {found}"
