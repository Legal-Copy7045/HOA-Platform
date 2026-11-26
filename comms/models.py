from django.conf import settings
from django.db import models
from django.utils import timezone


class Audience(models.TextChoices):
    MEMBERS = "members", "All owners and board"
    BOARD = "board", "Board, manager and auditor only"
    COMMITTEE = "committee", "One committee (and the board)"


class AudienceQuerySet(models.QuerySet):
    def for_user(self, user):
        from core import policy

        if not policy.can(user, "announcement.view"):
            return self.none()
        if policy.can(user, "document.view_board"):
            return self
        committees = policy.roles_for(user).committee_ids
        if committees and self.model is Event:
            return self.filter(
                models.Q(audience=Audience.MEMBERS)
                | models.Q(audience=Audience.COMMITTEE, committee_id__in=committees)
            )
        return self.filter(audience=Audience.MEMBERS)


class Announcement(models.Model):
    """Published announcements are never edited; post a correction instead."""

    title = models.CharField(max_length=200)
    body = models.TextField()
    audience = models.CharField(max_length=16, choices=Audience.choices, default=Audience.MEMBERS)
    urgent = models.BooleanField(default=False)
    published_at = models.DateTimeField(default=timezone.now, db_index=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    emailed_count = models.PositiveIntegerField(default=0)

    objects = AudienceQuerySet.as_manager()

    class Meta:
        ordering = ["-published_at", "-id"]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Announcements are kept; publish a correction instead.")


class AnnouncementRead(models.Model):
    announcement = models.ForeignKey(Announcement, on_delete=models.PROTECT, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    read_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["announcement", "user"], name="announcement_read_once")
        ]

    def __str__(self) -> str:
        return f"read #{self.pk}"


class Event(models.Model):
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)
    # Optional virtual join link (https only, comms.calendar_links.clean_join_url);
    # a meeting's event mirrors its meeting's link (D49).
    join_url = models.URLField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    audience = models.CharField(max_length=16, choices=Audience.choices, default=Audience.MEMBERS)
    cancelled = models.BooleanField(default=False)
    committee = models.ForeignKey(
        "governance.Committee", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    # RSVPs and volunteer sign-ups (comms/rsvp.py). Off until the board or
    # manager turns them on for this event.
    rsvp_enabled = models.BooleanField(default=False)
    rsvp_cap = models.PositiveIntegerField(
        null=True, blank=True, help_text="Most people (guests included) who can say Going."
    )
    rsvp_max_guests = models.PositiveSmallIntegerField(
        default=0, help_text="Guests each person may bring (0: no guests)."
    )
    rsvp_deadline = models.DateTimeField(
        null=True, blank=True, help_text="Answers and sign-ups close; empty: when it starts."
    )
    rsvp_remind = models.BooleanField(default=False)
    rsvp_reminded_at = models.DateTimeField(null=True, blank=True)

    objects = AudienceQuerySet.as_manager()

    class Meta:
        ordering = ["starts_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__isnull=True)
                | models.Q(ends_at__gte=models.F("starts_at")),
                name="event_ends_after_start",
            ),
            models.CheckConstraint(
                condition=(models.Q(audience="committee") & models.Q(committee__isnull=False))
                | (~models.Q(audience="committee") & models.Q(committee__isnull=True)),
                name="event_committee_matches_audience",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Events are cancelled, not deleted.")

    @property
    def answers_close_at(self):
        """When RSVPs and volunteer sign-ups close."""
        return self.rsvp_deadline or self.starts_at


# --- Message threads (Milestone 3) ----------------------------------------------------

from core.append_only import AppendOnlyModel  # noqa: E402


class ThreadMessage(AppendOnlyModel):
    """One message on the thread of a request, ARC request, violation or work
    order. Append-only: a correction is a new message. ``internal`` messages
    are for the board, manager and reviewers only (never the owner side);
    who may read a thread is decided by comms.threads, through the subject's
    own scoping."""

    subject_kind = models.CharField(max_length=24)
    subject_id = models.PositiveBigIntegerField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    body = models.TextField()
    internal = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["subject_kind", "subject_id"])]

    def __str__(self) -> str:
        return f"message #{self.pk}"


class FaqEntry(models.Model):
    """A question and answer on the FAQ page (board and manager keep it)."""

    question = models.CharField(max_length=300)
    answer = models.TextField()
    category = models.CharField(max_length=80, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    published = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["category", "sort_order", "question"]

    def __str__(self) -> str:
        return self.question

    def delete(self, *args, **kwargs):
        raise PermissionError("FAQ entries are unpublished, not deleted.")


class DigestItem(models.Model):
    """A routine update held for someone's weekly digest. Only a subject line
    and a link - never the content (emails carry links only)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    subject = models.CharField(max_length=200)
    link = models.CharField(max_length=500)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"digest item #{self.pk}"


# --- Installable app and text alerts (Milestone 4) -------------------------------------

from core.scoping import ScopedQuerySet  # noqa: E402


class OwnLoginQuerySet(ScopedQuerySet):
    """Rows that belong to one login. ``for_user`` returns only that login's
    rows, for everyone (the board included): nobody manages another person's
    devices or codes. Services that fan out use ``unscoped(reason)``."""

    def for_user(self, user):
        clone = self._clone()
        clone._scope_ok = True
        if not getattr(user, "is_authenticated", False):
            return clone.none()
        return clone.filter(user_id=user.pk)


class PushSubscription(models.Model):
    """One browser or installed app that asked for push notifications.
    Removed when the person turns it off, when the push service says it is
    gone (404/410) or cannot be found, or after several failures in a row
    (review R2-007). Holds only what the push service needs to reach the
    device."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    # Failed sends since the last success; removed at push.MAX_FAILURES.
    failures = models.PositiveSmallIntegerField(default=0)

    objects = OwnLoginQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"push subscription #{self.pk}"


class SmsVerification(models.Model):
    """A 6-digit code texted to confirm a mobile number. Only a keyed hash of
    the code is stored; it expires after ten minutes and allows a few tries."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    number = models.CharField(max_length=20)
    code_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)

    objects = OwnLoginQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"sms verification #{self.pk}"


class UrgentNotice(models.Model):
    """An urgent text the board sent to owners who opted in. Kept as a record
    (never deleted); holds the short text and link, and counts only - never
    who received it or their numbers."""

    message = models.CharField(max_length=300)
    link_path = models.CharField(max_length=200)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)
    recipients = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"urgent notice #{self.pk}"

    def delete(self, *args, **kwargs):
        raise PermissionError("Urgent notices are kept as a record.")


# --- Event RSVPs and volunteer sign-ups -------------------------------------------------

from simple_history.models import HistoricalRecords  # noqa: E402

from core.scoping import ScopedModel  # noqa: E402


class RsvpAnswer(models.TextChoices):
    GOING = "going", "Going"
    MAYBE = "maybe", "Maybe"
    NOT_GOING = "not_going", "Not going"


class EventRsvp(ScopedModel):
    """One person's answer to one event. Not a legal record: changed in place
    (with history) until the event's answers close. Change only through
    comms.rsvp. Other owners see counts only; the board sees names."""

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="+")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    answer = models.CharField(max_length=10, choices=RsvpAnswer.choices)
    guests = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "person"

    class Meta:
        ordering = ["event", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "person"], name="event_rsvp_once"),
            models.CheckConstraint(
                condition=models.Q(answer="going") | models.Q(guests=0),
                name="event_rsvp_guests_only_when_going",
            ),
        ]

    def __str__(self) -> str:
        return f"rsvp #{self.pk}"


class VolunteerSlot(models.Model):
    """Something people can sign up to do at an event ("Bring a dessert").
    Removed slots stay as a record, hidden from the event page."""

    event = models.ForeignKey(Event, on_delete=models.PROTECT, related_name="volunteer_slots")
    title = models.CharField(max_length=200)
    capacity = models.PositiveSmallIntegerField()
    removed = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["event", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(capacity__gte=1), name="volunteer_slot_capacity_positive"
            )
        ]

    def __str__(self) -> str:
        return self.title

    def delete(self, *args, **kwargs):
        raise PermissionError("Volunteer slots are removed, not deleted.")


class VolunteerSignup(ScopedModel):
    """One person signed up for one slot. Leaving deletes the row (history
    keeps it). Change only through comms.rsvp, which locks the slot so it is
    never overfilled."""

    slot = models.ForeignKey(VolunteerSlot, on_delete=models.PROTECT, related_name="+")
    person = models.ForeignKey("community.Person", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    history = HistoricalRecords()

    objects = ScopedQuerySet.as_manager()
    scope_person_path = "person"

    class Meta:
        ordering = ["slot", "id"]
        constraints = [
            models.UniqueConstraint(fields=["slot", "person"], name="volunteer_signup_once")
        ]

    def __str__(self) -> str:
        return f"signup #{self.pk}"


# --- Paper mail (D55) ----------------------------------------------------------------

from core.append_only import ScopedAppendOnlyModel  # noqa: E402


class WindowStyle(models.TextChoices):
    SINGLE = "single", "Single window (recipient only)"
    DOUBLE = "double", "Double window (our return address and the recipient)"


class PaperMailSettings(models.Model):
    """Where the address block sits on a letter for a #10 window envelope,
    small shifts for the envelope and label printer, and the USPS
    Intelligent Mail barcode settings. One row; inches, two decimals."""

    window = models.CharField(max_length=8, choices=WindowStyle.choices, default="single")
    address_left_in = models.DecimalField(max_digits=4, decimal_places=2, default="1.00")
    address_top_in = models.DecimalField(max_digits=4, decimal_places=2, default="2.13")
    return_left_in = models.DecimalField(max_digits=4, decimal_places=2, default="0.50")
    return_top_in = models.DecimalField(max_digits=4, decimal_places=2, default="0.63")
    envelope_shift_x_in = models.DecimalField(max_digits=4, decimal_places=2, default="0.00")
    envelope_shift_y_in = models.DecimalField(max_digits=4, decimal_places=2, default="0.00")
    label_shift_x_in = models.DecimalField(max_digits=4, decimal_places=2, default="0.00")
    label_shift_y_in = models.DecimalField(max_digits=4, decimal_places=2, default="0.00")
    duplex = models.BooleanField(default=False)
    # Intelligent Mail barcode (USPS-B-3200): off until a Mailer ID is entered.
    imb_enabled = models.BooleanField(default=False)
    mailer_id = models.CharField(max_length=9, blank=True)
    barcode_id = models.CharField(max_length=2, default="00")
    service_type = models.CharField(max_length=3, default="310")
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self) -> str:
        return "Paper mail settings"

    @classmethod
    def get(cls) -> "PaperMailSettings":
        found, _ = cls.objects.get_or_create(pk=1)
        return found

    def delete(self, *args, **kwargs):
        raise PermissionError("Paper mail settings are changed, not deleted.")

    @property
    def barcode_on(self) -> bool:
        return bool(self.imb_enabled and len(self.mailer_id) in (6, 9) and self.mailer_id.isdigit())


class MailingKind(models.TextChoices):
    MEETING = "meeting", "Meeting notice"
    BUDGET = "budget", "Budget notice"
    LETTER = "letter", "Letter"
    TEMPLATE = "template", "Letter from Letters and certificates"


class MailingAudience(models.TextChoices):
    PAPER = "paper", "Owners who get paper"
    ALL = "all", "Every owner"


class Mailing(ScopedAppendOnlyModel):
    """One paper mailing: the wording with its {placeholders}, who it goes
    to (homes and the owners named on each letter, by id), and what it was
    made from. Append-only; the letters, envelopes and labels are made from
    it on demand and never stored."""

    kind = models.CharField(max_length=10, choices=MailingKind.choices)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    audience = models.CharField(max_length=8, choices=MailingAudience.choices)
    only_lot_id = models.PositiveBigIntegerField(null=True, blank=True)
    meeting_id = models.PositiveBigIntegerField(null=True, blank=True)
    budget_id = models.PositiveBigIntegerField(null=True, blank=True)
    budget_stage = models.CharField(max_length=10, blank=True)
    template_kind = models.CharField(max_length=10, blank=True)
    # Meeting or budget facts as they were when the mailing was made.
    shared_values = models.JSONField(default=dict, blank=True)
    # [{"lot": id, "people": [person ids]}], one entry per letter.
    groups = models.JSONField(default=list)
    homes_count = models.PositiveIntegerField()
    letters_count = models.PositiveIntegerField()
    consent_version = models.CharField(max_length=20, blank=True)
    letter_date = models.DateField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["meeting_id"])]

    def __str__(self) -> str:
        return f"Mailing #{self.pk}"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        # Board and manager pages only (mail.print); never owners' rows.
        return queryset.none()


class MailingEventKind(models.TextChoices):
    MAILED = "mailed", "Mailed"
    DISCARDED = "discarded", "Not sent"


class MailingEvent(ScopedAppendOnlyModel):
    """Mailed on a date, or not sent after all: once per mailing."""

    mailing = models.ForeignKey(Mailing, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=10, choices=MailingEventKind.choices)
    mailed_on = models.DateField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["recorded_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["mailing"], name="mailing_event_once"),
            models.CheckConstraint(
                condition=models.Q(kind="discarded") | models.Q(mailed_on__isnull=False),
                name="mailing_event_mailed_has_date",
            ),
        ]

    def __str__(self) -> str:
        return f"Mailing event #{self.pk}"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        return queryset.none()


class ImbSerial(models.Model):
    """The next Intelligent Mail serial number for one Mailer ID. Taken
    under select_for_update, so no two mailpieces share a number."""

    mailer_id = models.CharField(max_length=9, unique=True)
    next_serial = models.PositiveBigIntegerField(default=1)

    def __str__(self) -> str:
        return "Intelligent Mail serial counter"

    def delete(self, *args, **kwargs):
        raise PermissionError("Serial counters are kept.")


class MailPiece(ScopedAppendOnlyModel):
    """One barcoded mailpiece of a mailing (one per letter): its serial
    number and the ZIP routing used, so a USPS scan can be matched to the
    home. Reprints reuse the same numbers."""

    mailing = models.ForeignKey(Mailing, on_delete=models.PROTECT, related_name="+")
    group = models.PositiveIntegerField()  # index into Mailing.groups
    lot_id = models.PositiveBigIntegerField()
    mailer_id = models.CharField(max_length=9)
    serial = models.PositiveBigIntegerField()
    tracking = models.CharField(max_length=20)
    routing = models.CharField(max_length=11, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["mailing", "group"]
        constraints = [
            models.UniqueConstraint(fields=["mailer_id", "serial"], name="mailpiece_serial_unique"),
            models.UniqueConstraint(fields=["mailing", "group"], name="mailpiece_once_per_letter"),
        ]

    def __str__(self) -> str:
        return f"Mailpiece #{self.pk}"

    @classmethod
    def scope_for_owner(cls, queryset, roles):
        return queryset.none()
