"""Lots, people, ownership and the board (decisions D1, D4).

Access rules derive from these records, never from flags on the login:

* an owner's access comes from an *active* ``Ownership``;
* board membership comes from an *active* ``BoardSeat``;
* an office comes from an *active* ``OfficeHolding``.

"Active on day D" always means ``start <= D < end`` (end date exclusive), so
ending a record with today's date removes access immediately.

Nothing here is ever deleted: records are ended, and django-simple-history
keeps every change. Change these models only through community.services.
"""

from __future__ import annotations

import datetime

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q
from django.utils import timezone
from simple_history.models import HistoricalRecords


def today() -> datetime.date:
    return timezone.localdate()


class NeverDeleteMixin:
    def delete(self, *args, **kwargs):
        raise PermissionError(f"{type(self).__name__} records are never deleted; end them.")


class DateRange(Func):
    """Postgres daterange(start, end, '[)') - used by exclusion constraints."""

    function = "DATERANGE"
    output_field = DateRangeField()


def _active_q(start: str, end: str, on: datetime.date) -> Q:
    return Q(**{f"{start}__lte": on}) & (Q(**{f"{end}__isnull": True}) | Q(**{f"{end}__gt": on}))


class Lot(NeverDeleteMixin, models.Model):
    number = models.PositiveSmallIntegerField(unique=True)
    street_address = models.CharField(max_length=200)
    history = HistoricalRecords()

    class Meta:
        ordering = ["number"]

    def __str__(self) -> str:
        return f"Lot {self.number}"


class Person(NeverDeleteMixin, models.Model):
    """A member of the community. May exist before (or without) a login."""

    display_name = models.CharField(max_length=150)
    email = models.EmailField(max_length=254, blank=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="person",
    )
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    class Meta:
        ordering = ["display_name", "id"]

    def __str__(self) -> str:
        return f"Person #{self.pk}"


class OwnershipType(models.TextChoices):
    SOLE = "sole", "Sole owner"
    JOINT = "joint", "Joint owner"
    TRUST = "trust", "Trust"
    ENTITY = "entity", "Entity (company, estate)"


class OwnershipQuerySet(models.QuerySet):
    def active(self, on: datetime.date | None = None) -> OwnershipQuerySet:
        return self.filter(_active_q("start_date", "end_date", on or today()))


class Ownership(NeverDeleteMixin, models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="ownerships")
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="ownerships")
    ownership_type = models.CharField(max_length=16, choices=OwnershipType.choices)
    start_date = models.DateField()
    end_date = models.DateField(
        null=True, blank=True, help_text="Exclusive: last day is the day before."
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = OwnershipQuerySet.as_manager()

    class Meta:
        ordering = ["lot__number", "start_date", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="ownership_end_not_before_start",
            ),
            # The same person cannot hold two overlapping ownerships of one lot.
            ExclusionConstraint(
                name="ownership_no_overlap_per_person_lot",
                expressions=[
                    (F("person"), RangeOperators.EQUAL),
                    (F("lot"), RangeOperators.EQUAL),
                    (DateRange("start_date", "end_date"), RangeOperators.OVERLAPS),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"Ownership #{self.pk}"

    def is_active(self, on: datetime.date | None = None) -> bool:
        on = on or today()
        return self.start_date <= on and (self.end_date is None or self.end_date > on)


class SeatType(models.TextChoices):
    DECLARANT_APPOINTED = "declarant_appointed", "Declarant-appointed"
    OWNER_ELECTED = "owner_elected", "Owner-elected"


class SeatEndReason(models.TextChoices):
    TERM_EXPIRED = "term_expired", "Term expired"
    RESIGNED = "resigned", "Resigned"
    REMOVED = "removed", "Removed"
    REPLACED = "replaced", "Replaced"
    OTHER = "other", "Other"


class BoardSeatQuerySet(models.QuerySet):
    def active(self, on: datetime.date | None = None) -> BoardSeatQuerySet:
        return self.filter(_active_q("term_start", "ended_on", on or today()))

    def past_term_end(self, on: datetime.date | None = None) -> BoardSeatQuerySet:
        """Active seats whose scheduled term has passed (board-page warning, D1)."""
        on = on or today()
        return self.active(on).filter(term_end__isnull=False, term_end__lt=on)


class BoardSeat(NeverDeleteMixin, models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="board_seats")
    seat_type = models.CharField(max_length=24, choices=SeatType.choices)
    term_start = models.DateField()
    term_end = models.DateField(
        null=True, blank=True, help_text="Scheduled end. Membership continues until ended_on."
    )
    ended_on = models.DateField(null=True, blank=True, help_text="Actual end (exclusive).")
    end_reason = models.CharField(max_length=16, choices=SeatEndReason.choices, blank=True)
    citation = models.CharField(max_length=200, blank=True, help_text='e.g. "Bylaws §3.2"')
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = BoardSeatQuerySet.as_manager()

    class Meta:
        ordering = ["term_start", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ended_on__isnull=True) | Q(ended_on__gte=F("term_start")),
                name="seat_end_not_before_start",
            ),
            models.CheckConstraint(
                condition=Q(ended_on__isnull=True, end_reason="")
                | (Q(ended_on__isnull=False) & ~Q(end_reason="")),
                name="seat_end_reason_with_end",
            ),
            ExclusionConstraint(
                name="seat_no_overlap_per_person",
                expressions=[
                    (F("person"), RangeOperators.EQUAL),
                    (DateRange("term_start", "ended_on"), RangeOperators.OVERLAPS),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"Board seat #{self.pk}"


class Office(models.TextChoices):
    PRESIDENT = "president", "President"
    VICE_PRESIDENT = "vice_president", "Vice president"
    TREASURER = "treasurer", "Treasurer"
    SECRETARY = "secretary", "Secretary"


class OfficeHoldingQuerySet(models.QuerySet):
    def active(self, on: datetime.date | None = None) -> OfficeHoldingQuerySet:
        return self.filter(_active_q("start_date", "end_date", on or today()))


class OfficeHolding(NeverDeleteMixin, models.Model):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="offices")
    office = models.CharField(max_length=16, choices=Office.choices)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Exclusive.")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = OfficeHoldingQuerySet.as_manager()

    class Meta:
        ordering = ["office", "start_date", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="office_end_not_before_start",
            ),
            # One holder per office at any time.
            ExclusionConstraint(
                name="office_single_holder",
                expressions=[
                    (F("office"), RangeOperators.EQUAL),
                    (DateRange("start_date", "end_date"), RangeOperators.OVERLAPS),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_office_display()} holding #{self.pk}"
