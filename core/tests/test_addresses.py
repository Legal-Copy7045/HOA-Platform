"""Homes are named by their address, not their lot number (decision D40).

Pages, emails and lists show the address only. A home's heading may carry the
plat lot number as a muted subtitle (``class="lot-ref"``); formal documents
(resale certificate, statements and delinquency letters, ARC decision letters,
violation notices, the printed neighbor acknowledgement form) show
"<address> (Lot N)".
"""

import re
from html.parser import HTMLParser

import pytest
from django.urls import NoReverseMatch, get_resolver, reverse

from community.models import Lot, home_sort_key
from community.tests.factories import LotFactory, make_member
from core.tests import test_url_matrix as matrix
from core.tests.pdftext import pdf_text
from core.tests.test_url_matrix import PROTECTED, _url

pytestmark = pytest.mark.django_db

world = matrix.world  # the cross-owner fixture: two homes and records on each

LOT_N = re.compile(r"\bLot\s+#?\d+")


class _Visible(HTMLParser):
    """Visible text plus confirm texts, without scripts, styles and the
    ``.lot-ref`` subtitle (the one place a page may name the lot number)."""

    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "col", "area"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[bool] = []  # per open element: hidden?

    def _hidden(self) -> bool:
        return any(self.stack)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        hidden = tag in ("script", "style") or "lot-ref" in classes
        if not self._hidden() and not hidden:
            for name in ("data-confirm", "placeholder", "title"):
                if attrs.get(name):
                    self.out.append(attrs[name])
        if tag not in self.VOID:
            self.stack.append(hidden)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if not self._hidden():
            self.out.append(data)


def visible_text(html: str) -> str:
    parser = _Visible()
    parser.feed(html)
    return " ".join(" ".join(parser.out).split())


def _offending(html: str) -> list[str]:
    text = visible_text(html)
    return [text[max(0, m.start() - 40) : m.end() + 20] for m in LOT_N.finditer(text)]


def _no_arg_get_urls() -> list[str]:
    names = []
    for name in get_resolver().reverse_dict:
        if not isinstance(name, str):
            continue
        try:
            reverse(name)
        except NoReverseMatch:
            continue
        names.append(name)
    return sorted(set(names))


SKIP = {
    # Not pages: files, logout, health, the service worker, preview switch.
    "account_logout",
    "healthz",
    "pwa_manifest",
    "pwa_service_worker",
    "owner_preview_stop",
    "login_background_image",
}


def _pages_for(client, user, urls) -> dict[str, str]:
    client.force_login(user)
    pages = {}
    for url in urls:
        response = client.get(url)
        kind = response.get("Content-Type", "")
        if response.status_code == 200 and kind.startswith("text/html"):
            pages[url] = response.content.decode()
    return pages


def test_lot_labels():
    lot = Lot(number=30, street_address="165 Imaginary Orchard Way")
    assert str(lot) == lot.label == "165 Imaginary Orchard Way"
    assert lot.legal_label == "165 Imaginary Orchard Way (Lot 30)"
    assert lot.lot_ref == "Lot 30"
    two_lines = Lot(number=4, street_address="12  Elm Street\nNewtown, PA")
    assert two_lines.label == "12 Elm Street"
    blank = Lot(number=7, street_address="  ")
    assert blank.label == "Home 7 (no address recorded)" and not blank.has_address


def test_homes_sort_by_street_then_house_number():
    lots = [
        Lot(number=1, street_address="102 Elm Street"),
        Lot(number=2, street_address="98 Elm Street"),
        Lot(number=3, street_address="5 Ash Court"),
    ]
    ordered = [lot.label for lot in sorted(lots, key=home_sort_key)]
    assert ordered == ["5 Ash Court", "98 Elm Street", "102 Elm Street"]


def test_no_page_names_a_home_by_lot_number(client, world):
    """Board, owner and owner-with-board views of a broad set of pages: the
    address shows, "Lot N" never does outside the .lot-ref subtitle."""
    urls = [
        reverse(name)
        for name in _no_arg_get_urls()
        if name not in SKIP and not name.startswith(("account_", "mfa_"))
    ]
    urls += [reverse(name) + "?mine=1" for name in ("arc_list", "violations", "requests")]
    urls += [
        _url(name, key, world["targets"])
        for name, (method, key) in sorted(PROTECTED.items())
        if method == "GET" and key
    ]
    urls += [reverse(name) + "?view=roster" for name in ("lot_list",)]
    checked = 0
    problems = {}
    for who in ("board", "owner_b", "owner_a"):
        pages = _pages_for(client, world[who], urls)
        checked += len(pages)
        for url, html in pages.items():
            bad = _offending(html)
            if bad:
                problems[f"{who} {url}"] = bad[:3]
    assert checked > 150, checked  # the sweep really rendered pages
    assert not problems, problems


def test_home_pages_show_the_address_as_title(client, world):
    lot_b = world["lot_b"]
    client.force_login(world["board"])
    for url in (
        reverse("lot_detail", args=[lot_b.pk]),
        reverse("lot_ledger", args=[lot_b.pk]),
    ):
        html = client.get(url).content.decode()
        assert "<h1>2 Bravo Way</h1>" in html, url
        assert f'<div class="lot-ref">Lot {lot_b.number}</div>' in html, url
    lists = client.get(reverse("lot_list")).content.decode()
    assert "2 Bravo Way" in lists and "<h1>Homes and owners</h1>" in lists
    client.force_login(world["owner_a"])
    html = client.get(reverse("my_lot")).content.decode()
    assert "<h2>1 Alpha Way</h2>" in html
    assert "Your home, the people on your account" in html
    assert "<h2>1 Alpha Way</h2>" in client.get(reverse("my_account")).content.decode()


def test_home_lists_follow_the_address_not_the_lot_number(client):
    late = LotFactory(number=900, street_address="1 Aspen Court")
    early = LotFactory(number=901, street_address="9 Zinnia Lane")
    LotFactory(number=1, street_address="5 Maple Drive")
    board = make_member(board=True)
    client.force_login(board)
    html = client.get(reverse("lot_list")).content.decode()
    assert html.index("1 Aspen Court") < html.index("5 Maple Drive") < html.index("9 Zinnia")
    assert late.number < early.number  # sorted by address, not number


def test_board_search_still_finds_a_home_by_its_lot_number(client, world):
    client.force_login(world["board"])
    html = client.get(reverse("search"), {"q": str(world["lot_b"].number)}).content.decode()
    assert "2 Bravo Way" in html
    assert not _offending(html)


def test_preview_banner_names_the_address(client, world):
    client.force_login(world["board"])
    client.post(reverse("owner_preview_start"), {"lot": world["lot_b"].pk})
    html = client.get(reverse("home")).content.decode()
    assert "the owner of 2 Bravo Way sees" in html
    assert not _offending(html)


def test_missing_address_is_a_setup_item_for_the_board(client):
    board = make_member(board=True)
    LotFactory(number=77, street_address="")
    from core.my_items import items_for

    titles = [i.title for i in items_for(board)]
    assert "1 home has no address recorded" in titles
    client.force_login(board)
    assert "Home 77 (no address recorded)" in client.get(reverse("my_items")).content.decode()


def test_csv_exports_lead_with_the_address(client, world):
    client.force_login(world["board"])
    roster = client.get(reverse("lot_list") + "?view=roster&export=csv").content.decode()
    header = roster.splitlines()[0].split(",")
    assert header[0] == "Address" and header[-1] == "Lot number"
    assert "2 Bravo Way" in roster
    for name in ("violations", "arc_list", "requests"):
        body = client.get(reverse(name) + "?export=csv").content.decode()
        head = body.splitlines()[0].split(",")
        assert head[0] == "Address" and "Lot" not in head and "Lot number" not in head, name


# Formal documents ("<address> (Lot N)") are checked next to each document's
# own tests: ledger/tests/test_letters.py, arc/tests/test_arc.py,
# arc/tests/test_signoffs.py and compliance/tests/test_violations.py.


def test_pdf_text_reads_runs_and_escapes():
    from core import pdf

    data = pdf.render([("normal", "To **165 Imaginary Orchard Way (Lot 30)** today")])
    assert "To 165 Imaginary Orchard Way (Lot 30) today" in pdf_text(data)
