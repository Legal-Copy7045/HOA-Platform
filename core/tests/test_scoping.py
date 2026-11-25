"""The scoped-manager guard: unscoped queries fail loudly (CLAUDE.md)."""

import pytest

from community.models import Lot, Ownership
from community.tests.factories import LotFactory
from core.scoping import UnscopedQueryError, system_access

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "query",
    [
        lambda: list(Lot.objects.all()),
        lambda: Lot.objects.get(number=1),
        lambda: Lot.objects.filter(number=1).first(),
        lambda: Lot.objects.count(),
        lambda: Lot.objects.exists(),
        lambda: list(Lot.objects.values_list("number", flat=True)),
        lambda: Lot.objects.aggregate(),
        lambda: list(Lot.objects.iterator()),
        lambda: Lot.objects.in_bulk(),
        lambda: Lot.objects.filter(number=1).update(street_address="x"),
        lambda: Lot.objects.get_or_create(number=99, defaults={"street_address": "x"}),
    ],
    ids=[
        "all",
        "get",
        "first",
        "count",
        "exists",
        "values_list",
        "aggregate",
        "iterator",
        "in_bulk",
        "update",
        "get_or_create",
    ],
)
def test_unscoped_queries_raise(query):
    LotFactory(number=1)
    with pytest.raises(UnscopedQueryError):
        query()


def test_reverse_relations_are_guarded_too():
    lot = LotFactory()
    with pytest.raises(UnscopedQueryError):
        list(lot.ownerships.all())


def test_explicit_unscoped_and_system_access_work():
    LotFactory(number=7)
    assert Lot.objects.unscoped("test").count() == 1
    with system_access("test"):
        assert Lot.objects.get(number=7).number == 7
    with pytest.raises(UnscopedQueryError):
        Lot.objects.count()  # the context ended


def test_unscoped_requires_a_reason():
    with pytest.raises(ValueError):
        Lot.objects.unscoped("")


def test_model_validation_still_works():
    LotFactory(number=3)
    duplicate = Lot(number=3, street_address="3 Fictional Lane")
    with pytest.raises(Exception, match="already exists"):
        duplicate.full_clean()


def test_forward_foreign_keys_are_not_blocked():
    # ownership.lot uses Django's base manager; the FK value itself is not a leak.
    from community.services import record_ownership
    from community.tests.factories import PersonFactory, days_ago
    from core.policy import SYSTEM

    o = record_ownership(
        person=PersonFactory(), lot=LotFactory(), start_date=days_ago(1), actor=SYSTEM
    )
    fresh = Ownership.objects.unscoped("test").get(pk=o.pk)
    assert fresh.lot.pk == o.lot_id
