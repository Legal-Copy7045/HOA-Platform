"""Synthetic test data only (CLAUDE.md: no real personal data)."""

import datetime

import factory

from accounts.models import User
from community.models import Lot, Person


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User

    email = factory.Sequence(lambda n: f"user{n}@example.com")


class LotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lot

    number = factory.Sequence(lambda n: n + 1)
    street_address = factory.Sequence(lambda n: f"{n + 1} Fictional Lane")


class PersonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Person

    display_name = factory.Sequence(lambda n: f"Test Person {n}")
    email = factory.Sequence(lambda n: f"person{n}@example.com")


def days_ago(n: int) -> datetime.date:
    from community.models import today

    return today() - datetime.timedelta(days=n)
