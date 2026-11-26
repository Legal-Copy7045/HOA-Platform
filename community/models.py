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
from django.contrib.postgres.fields import DateRangeField, DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

from core.append_only import AppendOnlyModel
from core.scoping import ScopedModel, ScopedQuerySet


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


class Lot(NeverDeleteMixin, ScopedModel):
    number = models.PositiveSmallIntegerField(unique=True)
    street_address = models.CharField(max_length=200)
    history = HistoricalRecords()

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = ""  # the lot itself

    class Meta:
        ordering = ["number"]

    def __str__(self) -> str:
        return self.label

    # D40: people know a home by its address, not its plat lot number.
    # `label` is what every page, email and list shows; `legal_label` adds
    # the lot number in brackets for formal documents a title company, court
    # or owner keeps (resale certificate, letters, statements, decisions).

    @property
    def label(self) -> str:
        """The address on one line, e.g. "165 Imaginary Orchard Way"."""
        first = (self.street_address or "").strip().splitlines()
        line = " ".join(first[0].split()) if first else ""
        return line or f"Home {self.number} (no address recorded)"

    @property
    def lot_ref(self) -> str:
        """The plat reference shown only as a muted subtitle or in brackets."""
        return f"Lot {self.number}"

    @property
    def legal_label(self) -> str:
        """For formal documents: "165 Imaginary Orchard Way (Lot 30)"."""
        return f"{self.label} ({self.lot_ref})"

    @property
    def has_address(self) -> bool:
        return bool((self.street_address or "").strip())

    @property
    def sort_key(self) -> tuple:
        """Lists are in address order: street name, then house number."""
        return home_sort_key(self)


def home_sort_key(lot) -> tuple:
    """(street, house number, lot number) so "98 Elm" comes before "102 Elm"."""
    label = lot.label if lot.has_address else ""
    head, _, rest = label.partition(" ")
    if head.isdigit():
        return (rest.lower(), int(head), lot.number)
    return (label.lower(), 0, lot.number)


def sorted_homes(lots) -> list:
    """Lots (or anything with a ``.lot``-free Lot interface) in address order."""
    return sorted(lots, key=home_sort_key)


class Person(NeverDeleteMixin, ScopedModel):
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

    objects = ScopedQuerySet.as_manager()

    class Meta:
        ordering = ["display_name", "id"]

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners see themselves and their current co-owners - not past owners."""
        co_owners = Ownership.objects.unscoped("co-owner lookup").active()
        co_owners = co_owners.filter(lot_id__in=roles.account_lot_ids).values("person_id")
        return queryset.filter(models.Q(pk=roles.person_id) | models.Q(pk__in=co_owners))

    def __str__(self) -> str:
        return f"Person #{self.pk}"


class OwnershipType(models.TextChoices):
    SOLE = "sole", "Sole owner"
    JOINT = "joint", "Joint owner"
    TRUST = "trust", "Trust"
    ENTITY = "entity", "Entity (company, estate)"


class AccountQuerySet(ScopedQuerySet):
    def active(self, on: datetime.date | None = None) -> AccountQuerySet:
        return self.filter(_active_q("opened_on", "closed_on", on or today()))


class Account(ScopedModel):
    """One ownership period of one lot: the owners on the deed share it,
    with its account number and balance. It goes dormant when the lot is
    sold; the lot keeps its history and a new account starts."""

    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="accounts")
    number = models.CharField(max_length=40, unique=True)
    opened_on = models.DateField()
    closed_on = models.DateField(
        null=True, blank=True, help_text="Exclusive: the last day is the day before."
    )
    # When the closing was recorded (fixres BUG-001): an entry posted while
    # the account was still open, dated after a closing recorded later with
    # an earlier date, was put here by the date rule, not chosen by hand.
    closed_recorded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = AccountQuerySet.as_manager()
    scope_lot_path = "lot"

    class Meta:
        ordering = ["lot__number", "-opened_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(closed_on__isnull=True) | Q(closed_on__gte=F("opened_on")),
                name="account_close_not_before_open",
            ),
            models.UniqueConstraint(
                fields=["lot"],
                condition=Q(closed_on__isnull=True),
                name="account_one_active_per_lot",
            ),
        ]

    def __str__(self) -> str:
        return f"Account {self.number}"

    @property
    def is_active(self) -> bool:
        return self.closed_on is None or self.closed_on > today()

    @property
    def status_label(self) -> str:
        return "Active" if self.is_active else "Dormant"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Current account(s) of the lots this person can see, plus the
        dormant accounts they were an owner on (read-only history)."""
        own = Q(lot_id__in=roles.account_lot_ids)
        past = Q(pk__in=list(roles.former_account_ids))
        return queryset.filter(own | past)


class OwnershipQuerySet(ScopedQuerySet):
    def active(self, on: datetime.date | None = None) -> OwnershipQuerySet:
        return self.filter(_active_q("start_date", "end_date", on or today()))


class Ownership(NeverDeleteMixin, ScopedModel):
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="ownerships")
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="ownerships")
    account = models.ForeignKey(
        Account, null=True, blank=True, on_delete=models.PROTECT, related_name="ownerships"
    )
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
    scope_lot_path = "lot"

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

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners and account contacts see the current owners of their lots,
        not the lot's past owners."""
        return queryset.filter(lot_id__in=roles.account_lot_ids).active()

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


class BoardSeatQuerySet(ScopedQuerySet):
    def active(self, on: datetime.date | None = None) -> BoardSeatQuerySet:
        return self.filter(_active_q("term_start", "ended_on", on or today()))

    def past_term_end(self, on: datetime.date | None = None) -> BoardSeatQuerySet:
        """Active seats whose scheduled term has passed (board-page warning, D1)."""
        on = on or today()
        return self.active(on).filter(term_end__isnull=False, term_end__lt=on)


class BoardSeat(NeverDeleteMixin, ScopedModel):
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
    scope_person_path = "person"

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


class OfficeHoldingQuerySet(ScopedQuerySet):
    def active(self, on: datetime.date | None = None) -> OfficeHoldingQuerySet:
        return self.filter(_active_q("start_date", "end_date", on or today()))


class OfficeHolding(NeverDeleteMixin, ScopedModel):
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
    scope_person_path = "person"

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


class Role(models.TextChoices):
    ARC_REVIEWER = "arc_reviewer", "ARC reviewer"
    AUDITOR = "auditor", "Auditor (read-only)"
    SYSADMIN = "sysadmin", "System administrator"
    MANAGER = "manager", "Community manager (management company)"


class AppointedBy(models.TextChoices):
    BOARD = "board", "Board"
    DECLARANT = "declarant", "Declarant"


class RoleAssignmentQuerySet(ScopedQuerySet):
    def active(self, on: datetime.date | None = None) -> RoleAssignmentQuerySet:
        return self.filter(_active_q("start_date", "end_date", on or today()))


class RoleAssignment(NeverDeleteMixin, ScopedModel):
    """ARC reviewer, auditor and sysadmin roles (decision D1). Ended, never deleted."""

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="roles")
    role = models.CharField(max_length=16, choices=Role.choices)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True, help_text="Exclusive.")
    appointed_by = models.CharField(
        max_length=16, choices=AppointedBy.choices, blank=True, help_text="Required for ARC."
    )
    citation = models.CharField(max_length=200, blank=True, help_text='e.g. "Declaration §9.1"')
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = RoleAssignmentQuerySet.as_manager()
    scope_person_path = "person"

    class Meta:
        ordering = ["role", "start_date", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__isnull=True) | Q(end_date__gte=F("start_date")),
                name="role_end_not_before_start",
            ),
            models.CheckConstraint(
                condition=~Q(role="arc_reviewer") | ~Q(appointed_by=""),
                name="role_arc_records_appointer",
            ),
            ExclusionConstraint(
                name="role_no_overlap_per_person",
                expressions=[
                    (F("person"), RangeOperators.EQUAL),
                    (F("role"), RangeOperators.EQUAL),
                    (DateRange("start_date", "end_date"), RangeOperators.OVERLAPS),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_role_display()} assignment #{self.pk}"


class NominationOutcome(models.TextChoices):
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined or withdrawn"


class SysadminNomination(NeverDeleteMixin, ScopedModel):
    """Granting sysadmin needs two different board members (decision D4)."""

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="+")
    nominated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    nominated_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=NominationOutcome.choices, blank=True)
    assignment = models.OneToOneField(
        "RoleAssignment", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "person"

    class Meta:
        ordering = ["-nominated_at", "-id"]

    def __str__(self) -> str:
        return f"Sysadmin nomination #{self.pk}"

    @property
    def is_open(self) -> bool:
        return not self.outcome and self.expires_at > timezone.now()


# --- Community definition (rules as data, CLAUDE.md rule 6) ---------------------


class CommunityProfile(models.Model):
    """The single row describing the association itself."""

    legal_name = models.CharField(max_length=200, default="Oak Creek Farms Homeowners Association")
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)
    # Shown at the top of everyone's dashboard (Milestone 3); plain text.
    welcome_message = models.TextField(blank=True)
    # The printable alteration request form (Milestone 4): where to return it
    # and the conditions owners agree to. Set by the board; not hard-coded.
    arc_form_return_to = models.CharField(max_length=300, blank=True)
    arc_form_conditions = models.TextField(blank=True)
    # Who owners contact about sign-in trouble (UX review): shown on the
    # sign-in help page, which anyone can open, so association contacts only.
    signin_help_contact = models.CharField(max_length=200, blank=True)
    # Letters and statements (Milestone 4): set by the board, never hard-coded.
    letterhead = models.TextField(
        blank=True, help_text="Lines at the top of letters, e.g. name, c/o manager, address, email."
    )
    remit_to = models.TextField(blank=True, help_text="Where owners mail checks.")
    letter_contact = models.CharField(
        max_length=200, blank=True, help_text="How owners reach accounting, e.g. email and phone."
    )
    letter_signature = models.TextField(
        blank=True, help_text="Signature block on letters, e.g. association, by its agent."
    )
    # Resale certificate facts that are the same for every lot (Milestone 4).
    # Empty means "none" on the certificate; the board keeps them current.
    resale_restraints = models.TextField(blank=True)
    resale_other_fees = models.TextField(blank=True)
    resale_capital_expenditures = models.TextField(blank=True)
    resale_reserves_cents = models.BigIntegerField(null=True, blank=True)
    resale_reserve_designations = models.TextField(
        blank=True, help_text="One per line: amount | project"
    )
    resale_judgments = models.TextField(blank=True)
    resale_insurance_cents = models.BigIntegerField(null=True, blank=True)
    resale_insurance_company = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self) -> str:
        return self.legal_name

    @classmethod
    def get(cls) -> CommunityProfile:
        profile, _ = cls.objects.get_or_create(pk=1)
        return profile

    def delete(self, *args, **kwargs):
        raise PermissionError("The community profile cannot be deleted.")


class GoverningRule(models.Model):
    """A value that comes from the Declaration, bylaws or a board decision.

    Keys are fixed in community.rules.REGISTRY; each value carries a citation.
    """

    key = models.CharField(max_length=64, unique=True)
    value = models.CharField(max_length=200, blank=True)
    citation = models.CharField(max_length=200, blank=True, help_text='e.g. "Declaration §9.3"')
    notes = models.TextField(blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["key"]

    def __str__(self) -> str:
        return self.key

    def delete(self, *args, **kwargs):
        raise PermissionError("Rules are changed, never deleted (history is kept).")


class CommonAreaKind(models.TextChoices):
    STORMWATER = "stormwater", "Stormwater / ponds"
    LANDSCAPING = "landscaping", "Entrance and landscaping"
    ROADS = "roads", "Roads and lighting"
    AMENITY = "amenity", "Amenity"
    OTHER = "other", "Other"


class CommonArea(NeverDeleteMixin, models.Model):
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=CommonAreaKind.choices)
    notes = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ContactProfile(ScopedModel):
    """An owner's own contact details, notice consent and directory choices."""

    person = models.OneToOneField(Person, on_delete=models.PROTECT, related_name="contact")
    phone = models.CharField(max_length=40, blank=True)
    mailing_address = models.TextField(
        blank=True, help_text="Only if different from the lot address."
    )
    electronic_notice_consent = models.BooleanField(
        default=False, help_text="Agree to receive official notices by email."
    )
    consent_changed_at = models.DateTimeField(null=True, blank=True)
    # Directory: hidden unless the owner opts in, field by field (decision 5).
    in_directory = models.BooleanField(default=False)
    directory_show_email = models.BooleanField(default=False)
    directory_show_phone = models.BooleanField(default=False)
    # Routine updates (replies, discussions, sign-off requests): at once or in
    # a weekly digest (Milestone 4). Legal notices are always sent at once.
    weekly_digest = models.BooleanField(default=False)
    # Urgent text messages (Milestone 4): opt-in, and only to a number the
    # owner proved by entering a texted code. Never shown in the directory.
    sms_number = models.CharField(max_length=20, blank=True)
    sms_verified_at = models.DateTimeField(null=True, blank=True)
    sms_opt_in = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "person"

    def __str__(self) -> str:
        return f"Contact profile #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Profiles are edited, not deleted.")


def _branding_key(instance, filename):
    import uuid

    return f"branding/{uuid.uuid4().hex}"


class LoginBackground(models.Model):
    """The picture behind the sign-in page. Uploaded by the board (or a
    sysadmin), re-encoded on upload so no hidden data (GPS, camera details)
    or crafted content survives. The newest row is the one shown; removing
    the picture adds a row with no file, so the history is kept."""

    class Variant(models.TextChoices):
        DESKTOP = "desktop", "Desktop and tablet"
        PHONE = "phone", "Phone"

    variant = models.CharField(max_length=8, choices=Variant.choices, default=Variant.DESKTOP)
    file = models.FileField(upload_to=_branding_key, max_length=200, blank=True)
    content_type = models.CharField(max_length=40, blank=True)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    uploaded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-uploaded_at", "-id"]

    def __str__(self) -> str:
        return f"login background #{self.pk}"


class ContactKind(models.TextChoices):
    AUTHORIZED = "authorized", "Authorized contact"
    VIEW_ONLY = "view_only", "View-only contact"
    TENANT = "tenant", "Tenant"


CONTACT_KIND_HELP = {
    ContactKind.AUTHORIZED: "Sees the account and raises requests; does not vote or apply for ARC.",
    ContactKind.VIEW_ONLY: "Sees the account and notices; cannot act.",
    ContactKind.TENANT: (
        "Lives there: community documents, news and their own requests; no balance."
    ),
}


class AccountContactQuerySet(ScopedQuerySet):
    def active(self, on: datetime.date | None = None) -> AccountContactQuerySet:
        return self.filter(_active_q("start_date", "end_date", on or today()))


class AccountContact(NeverDeleteMixin, ScopedModel):
    """Someone on an account who is not on the deed: what they may do comes
    from their kind. Ends when the account goes dormant or the board ends it."""

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="contacts")
    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="account_contacts")
    kind = models.CharField(max_length=12, choices=ContactKind.choices)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = AccountContactQuerySet.as_manager()
    scope_lot_path = "account__lot"

    class Meta:
        ordering = ["account", "kind", "start_date"]
        constraints = [
            ExclusionConstraint(
                name="account_contact_no_overlap",
                expressions=[
                    (F("person"), RangeOperators.EQUAL),
                    (F("account"), RangeOperators.EQUAL),
                    (DateRange("start_date", "end_date"), RangeOperators.OVERLAPS),
                ],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} #{self.pk}"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        return queryset.filter(account__lot_id__in=roles.account_lot_ids).active()


class ContactMethodKind(models.TextChoices):
    EMAIL = "email", "Email"
    PHONE = "phone", "Phone"
    ADDRESS = "address", "Mailing address"


class ContactMethod(ScopedModel):
    """An extra way to reach a person (a second email, a mobile, an
    absentee owner's mailing address), each with its own notice consent."""

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="contact_methods")
    kind = models.CharField(max_length=8, choices=ContactMethodKind.choices)
    label = models.CharField(max_length=60, blank=True, help_text='e.g. "Work", "Spouse"')
    value = models.CharField(max_length=300)
    notices = models.BooleanField(
        default=False, help_text="Send association notices here too (email) / by post (address)."
    )
    created_at = models.DateTimeField(default=timezone.now)

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "person"

    class Meta:
        ordering = ["person", "kind", "id"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} for person #{self.person_id}"


class AccountChangeAction(models.TextChoices):
    ADD = "add", "Add someone"
    REMOVE = "remove", "Remove someone"
    # Owners on the deed (D41): only with the recorded deed attached.
    ADD_OWNER = "add_owner", "Add an owner on the deed"
    REMOVE_OWNER = "remove_owner", "Remove an owner from the deed"


OWNER_CHANGE_ACTIONS = frozenset({AccountChangeAction.ADD_OWNER, AccountChangeAction.REMOVE_OWNER})
# A deed adds a co-owner as joint owner, a trust or an entity; "sole" is a sale.
ADDED_OWNER_TYPES = (OwnershipType.JOINT, OwnershipType.TRUST, OwnershipType.ENTITY)
DEED_FILE_LABEL = "Recorded deed"


class AccountChangeState(models.TextChoices):
    PENDING = "pending", "Waiting for the board"
    APPROVED = "approved", "Approved"
    DECLINED = "declined", "Declined"
    WITHDRAWN = "withdrawn", "Withdrawn"


class AccountChangeRequest(NeverDeleteMixin, ScopedModel):
    """An owner asks to add or remove a contact on their account, or (with
    the recorded deed attached) an owner on the deed; a board member (not
    the requester, nor the person added or removed) approves or declines.
    Approval makes the change through the normal services, so it is audited
    the same way, and invites an added person who has an email (D41)."""

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="change_requests")
    action = models.CharField(max_length=12, choices=AccountChangeAction.choices)
    kind = models.CharField(max_length=12, choices=ContactKind.choices, blank=True)
    # For "add" / "add_owner": who to add (a new person record is created on approval).
    name = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    # They don't use email: kept on the account as a record, without a sign-in.
    no_login = models.BooleanField(default=False)
    # For "remove": which contact.
    contact = models.ForeignKey(
        AccountContact, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # For "add_owner": joint / trust / entity. For both owner actions: the
    # date on the recorded deed (the deed itself is an attachment).
    ownership_type = models.CharField(max_length=16, choices=OwnershipType.choices, blank=True)
    deed_date = models.DateField(null=True, blank=True)
    # For "remove_owner": which ownership ends.
    ownership = models.ForeignKey(
        Ownership, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    # Set on approval: the person added, and whether their invitation went out
    # (None: no invitation was due).
    person = models.ForeignKey(
        Person, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    invitation_sent = models.BooleanField(null=True, blank=True)
    reason = models.TextField(blank=True)
    state = models.CharField(
        max_length=10, choices=AccountChangeState.choices, default=AccountChangeState.PENDING
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = "account__lot"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners of the account's lot see its change requests."""
        return queryset.filter(
            account__lot_id__in=roles.owned_lot_ids, account__closed_on__isnull=True
        )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Account change #{self.pk}"

    @property
    def is_owner_change(self) -> bool:
        return self.action in OWNER_CHANGE_ACTIONS

    @property
    def adds_person(self) -> bool:
        return self.action in (AccountChangeAction.ADD, AccountChangeAction.ADD_OWNER)

    @property
    def will_invite(self) -> bool:
        """Approval sends a sign-in invitation (an added person with an email
        who uses it)."""
        return self.adds_person and bool(self.email) and not self.no_login


class BoardSeatNomination(NeverDeleteMixin, ScopedModel):
    """A proposed board seat waiting for a second board member (decision D21).
    On approval the seat is recorded by the approver."""

    person = models.ForeignKey(Person, on_delete=models.PROTECT, related_name="+")
    seat_type = models.CharField(max_length=24, choices=SeatType.choices)
    term_start = models.DateField()
    term_end = models.DateField(null=True, blank=True)
    citation = models.CharField(max_length=200, blank=True)
    nominated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    nominated_at = models.DateTimeField(default=timezone.now)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    outcome = models.CharField(max_length=16, choices=NominationOutcome.choices, blank=True)
    seat = models.OneToOneField(
        BoardSeat, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "person"

    class Meta:
        ordering = ["-nominated_at", "-id"]

    def __str__(self) -> str:
        return f"Board seat nomination #{self.pk}"

    @property
    def is_open(self) -> bool:
        return not self.outcome


# --- Common-area reservations (Milestone 4, optional feature "reservations") ---------


class TimeRange(Func):
    """Postgres tstzrange(start, end, '[)') - for the no-double-booking constraint."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class ReservationSettings(models.Model):
    """Booking rules for one common area, set by the board. Where a rule comes
    from the Declaration or the Rules and Regulations, ``citation`` says where.
    Fees and deposits are shown to owners as text only: nothing is charged
    here."""

    area = models.OneToOneField(
        CommonArea, on_delete=models.PROTECT, related_name="reservation_settings"
    )
    bookable = models.BooleanField(default=False)
    needs_approval = models.BooleanField(
        default=True, help_text="A board member or the manager confirms each booking."
    )
    max_hours = models.PositiveSmallIntegerField(null=True, blank=True)
    advance_days = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="How far ahead a booking may be made."
    )
    monthly_limit_per_lot = models.PositiveSmallIntegerField(null=True, blank=True)
    fee_cents = models.BigIntegerField(null=True, blank=True)
    deposit_cents = models.BigIntegerField(null=True, blank=True)
    allow_authorized = models.BooleanField(
        default=False, help_text="Authorized contacts on an account may book."
    )
    allow_tenants = models.BooleanField(default=False, help_text="Tenants may book.")
    terms = models.TextField(blank=True, help_text="Rules owners agree to, shown when booking.")
    citation = models.CharField(max_length=200, blank=True, help_text='e.g. "Rules §4.2"')
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(fee_cents__isnull=True) | Q(fee_cents__gte=0))
                & (Q(deposit_cents__isnull=True) | Q(deposit_cents__gte=0)),
                name="reservation_settings_money_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"Booking rules for area #{self.area_id}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Booking rules are changed, never deleted (history is kept).")


class ReservationState(models.TextChoices):
    PENDING = "pending", "Waiting for approval"
    CONFIRMED = "confirmed", "Confirmed"
    DECLINED = "declined", "Declined"
    CANCELLED = "cancelled", "Cancelled"


OPEN_RESERVATION_STATES = (ReservationState.PENDING, ReservationState.CONFIRMED)


class Reservation(NeverDeleteMixin, ScopedModel):
    """One booking of a common area for one lot. Change only through
    community.reservations. Two confirmed bookings of one area can never
    overlap: the database refuses it."""

    area = models.ForeignKey(CommonArea, on_delete=models.PROTECT, related_name="reservations")
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="reservations")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    purpose = models.CharField(max_length=200, blank=True)
    state = models.CharField(
        max_length=10, choices=ReservationState.choices, default=ReservationState.PENDING
    )
    created_at = models.DateTimeField(default=timezone.now)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, help_text="Reason for declining or cancelling.")
    history = HistoricalRecords()

    objects = ScopedQuerySet.as_manager()
    scope_lot_path = "lot"

    class Meta:
        ordering = ["starts_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=F("starts_at")), name="reservation_ends_after_start"
            ),
            ExclusionConstraint(
                name="reservation_no_double_booking",
                expressions=[
                    (F("area"), RangeOperators.EQUAL),
                    (TimeRange("starts_at", "ends_at"), RangeOperators.OVERLAPS),
                ],
                condition=Q(state="confirmed"),
            ),
        ]

    def __str__(self) -> str:
        return f"Booking #{self.pk}"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        """Owners and authorized contacts see their lot's bookings from their own
        time there; anyone sees the bookings they made themselves."""
        from core.scoping import owner_era_q

        condition = owner_era_q(roles)
        if roles.user_id is not None:
            condition |= Q(requested_by_id=roles.user_id)
        return queryset.filter(condition)

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_RESERVATION_STATES


# --- Retention of former owners' contact details (Milestone 4) -----------------------


class RetentionRun(AppendOnlyModel):
    """One run of the contact-details retention rule: counts only, never
    names. Append-only (see migration)."""

    ran_at = models.DateTimeField(default=timezone.now)
    ran_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    days = models.PositiveIntegerField()
    people = models.PositiveIntegerField(default=0)
    profiles_cleared = models.PositiveIntegerField(default=0)
    methods_removed = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-ran_at", "-id"]

    def __str__(self) -> str:
        return f"Retention run #{self.pk}"
