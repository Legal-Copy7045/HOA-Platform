"""An owner with many to-dos sees them all on the dashboard and never a link
to the staff-only My items page (it would be refused; found by the demo
data crawl, PAGE-1265/1266)."""

from unittest import mock

import pytest
from django.urls import reverse

from community.tests.factories import LotFactory, make_member
from core.my_items import Item

pytestmark = pytest.mark.django_db


def _many(n=7):
    return [
        Item("Your account", f"To-do {i}", "Detail", "/my-profile/", "warning", side="home")
        for i in range(n)
    ]


def test_owner_with_many_todos_gets_no_my_items_link(client):
    owner = make_member(lots=[LotFactory()])
    client.force_login(owner)
    with mock.patch("core.my_items.items_for_request", return_value=_many()):
        html = client.get(reverse("home")).content.decode()
    assert "To-do 6" in html
    assert reverse("my_items") not in html


def test_board_member_with_many_todos_keeps_the_link(client):
    board = make_member(board=True)
    client.force_login(board)
    items = [
        Item("Owner requests", f"Board to-do {i}", "Detail", "/", "warning", side="board")
        for i in range(12)
    ]
    with mock.patch("core.my_items.items_for_request", return_value=items):
        html = client.get(reverse("home")).content.decode()
    assert reverse("my_items") in html
