from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from comms import services
from comms.models import Announcement, AnnouncementRead, Audience, Event
from core import policy


class AnnouncementForm(forms.Form):
    title = forms.CharField(max_length=200)
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 6}))
    audience = forms.ChoiceField(choices=Audience.choices[:2])
    urgent = forms.BooleanField(required=False)
    # Urgent texts (streamlining): only when the "sms" feature is on and the
    # person may send them (sms.send_urgent); otherwise the fields are removed.
    send_text = forms.BooleanField(required=False)
    text_message = forms.CharField(
        label="Text message (optional)",
        required=False,
        max_length=300,
        widget=forms.Textarea(attrs={"rows": 2, "maxlength": 300}),
        help_text="Up to 300 characters; the title is used if left blank. No names, "
        "addresses, amounts or anything about one home. The text links to this announcement.",
    )

    def __init__(self, *args, can_text: bool = False, opted_in: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_text = can_text
        self.opted_in = opted_in
        if can_text:
            self.fields["send_text"].label = f"Also send as an urgent text ({opted_in} opted in)"
        else:
            del self.fields["send_text"]
            del self.fields["text_message"]

    def clean(self):
        from comms import sms

        data = super().clean()
        if not (self.can_text and data.get("send_text")):
            return data
        # Checked before publishing, so a text that cannot go out never
        # leaves a published announcement behind.
        if not sms.can_send():
            self.add_error("send_text", "Text messages are not set up yet (no provider).")
            return data
        if not self.opted_in:
            self.add_error("send_text", "No owners have opted in to text messages yet.")
            return data
        if data.get("audience") != Audience.MEMBERS:
            self.add_error("send_text", "Texts go to owners: choose all owners as the audience.")
            return data
        try:
            data["text_message"] = sms.check_message(
                data.get("text_message") or data.get("title") or ""
            )
        except sms.SmsError as exc:
            self.add_error("text_message", str(exc))
        return data


class EventForm(forms.Form):
    title = forms.CharField(max_length=200)
    starts_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    ends_at = forms.DateTimeField(
        required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    location = forms.CharField(max_length=200, required=False, label="Place")
    join_url = forms.CharField(
        label="Link to join online",
        max_length=500,
        required=False,
        widget=forms.URLInput(attrs={"placeholder": "https://"}),
        help_text="Optional. Shown only to people who can see the event, never in emails.",
    )
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    audience = forms.ChoiceField(choices=Audience.choices[:2])

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("auto_id", "event_%s")
        super().__init__(*args, **kwargs)

    def clean_join_url(self):
        from comms.calendar_links import clean_join_url

        return clean_join_url(self.cleaned_data.get("join_url"))

    def clean(self):
        data = super().clean()
        if data.get("ends_at") and data.get("starts_at") and data["ends_at"] < data["starts_at"]:
            self.add_error("ends_at", "An event cannot end before it starts.")
        return data


@login_required
def announcements(request):
    policy.require(request.user, "announcement.view")
    can_text = _can_text(request)
    opted_in = 0
    if can_text:
        from community.services import sms_recipients

        opted_in = len(sms_recipients())
    form = AnnouncementForm(request.POST or None, can_text=can_text, opted_in=opted_in)
    if request.method == "POST":
        policy.require(request.user, "announcement.publish")
        if form.is_valid():
            data = dict(form.cleaned_data)
            send_text = data.pop("send_text", False)
            text = data.pop("text_message", "")
            try:
                announcement = services.publish_announcement(
                    actor=request.user, request=request, **data
                )
            except services.CommsError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Published. Members are being emailed a link.")
                if send_text:
                    _send_text(request, announcement, text)
                return redirect("announcements")
    items = Announcement.objects.for_user(request.user)[:100]
    read = set(
        AnnouncementRead.objects.filter(user=request.user).values_list("announcement_id", flat=True)
    )
    return render(
        request,
        "comms/announcements.html",
        {
            "items": [(a, a.pk in read) for a in items],
            "form": form,
            "can_publish": policy.can(request.user, "announcement.publish"),
            "open_form": bool(form.errors),
        },
    )


def _can_text(request) -> bool:
    """The urgent-text box: the "sms" feature on and sms.send_urgent."""
    from comms import sms

    return sms.available(request) and policy.can(request.user, "sms.send_urgent")


def _send_text(request, announcement, text: str) -> None:
    """Queue the urgent text linking to the new announcement. The service
    checks sms.send_urgent again; a refusal leaves the announcement published."""
    from comms import sms

    try:
        notice = sms.send_urgent_notice(
            actor=request.user,
            message=text,
            link=f"announcement:{announcement.pk}",
            request=request,
        )
    except sms.SmsError as exc:
        messages.error(request, f"The announcement is published, but no text was sent: {exc}")
    else:
        n = notice.recipients
        messages.success(request, f"Urgent text sending to {n} owner{'s' if n != 1 else ''}.")


@login_required
def announcement_detail(request, pk):
    announcement = get_object_or_404(Announcement.objects.for_user(request.user), pk=pk)
    services.mark_read(announcement, request.user)
    return render(request, "comms/announcement_detail.html", {"a": announcement})


@login_required
def calendar(request):
    """One calendar for events and meetings (D49): filter All / Meetings /
    Events, and Past meetings with their approved minutes. Meetings open
    their meeting page; executive sessions appear only in the board's view."""
    policy.require(request.user, "calendar.view")
    form = EventForm(request.POST or None)
    if request.method == "POST":
        policy.require(request.user, "calendar.manage")
        if form.is_valid():
            try:
                services.create_event(actor=request.user, request=request, **form.cleaned_data)
            except services.CommsError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Event added.")
                return redirect("calendar")
        # Invalid: the page again with the form open and what was typed.
    return render_calendar(request, form=form)


CALENDAR_FILTERS = [
    ("all", "All"),
    ("meetings", "Meetings"),
    ("events", "Events"),
    ("past", "Past meetings"),
]


def render_calendar(request, *, form=None, meeting_form=None, status=200):
    """The Calendar page; also used by governance.views.meetings to show a
    refused "New meeting" form again with what was typed."""
    from django.utils import timezone

    from comms import calendar_links

    user = request.user
    shown = request.GET.get("type", "all")
    if request.GET.get("view") == "past":
        shown = "past"
    if shown not in dict(CALENDAR_FILTERS):
        shown = "all"
    can_manage = policy.can(user, "calendar.manage")
    can_meet = policy.can(user, "meeting.manage") and policy.can(user, "meeting.view")
    if form is None:
        form = EventForm()
    if meeting_form is None and can_meet:
        from governance.views import MeetingForm

        meeting_form = MeetingForm()
    opening = request.GET.get("new", "")
    open_event = bool(form.errors) or (opening == "event" and can_manage)
    open_meeting = bool(meeting_form is not None and meeting_form.errors) or (
        opening == "meeting" and can_meet
    )
    rows, past = [], []
    if shown == "past":
        past = _past_meetings(user)
    else:
        rows = _calendar_rows(user, shown)
    for row in rows:
        row["cal"] = calendar_links.menu(row["item"], row["ics_url"])
    return render(
        request,
        "comms/calendar.html",
        {
            "rows": rows,
            "past": past,
            "shown": shown,
            "filters": CALENDAR_FILTERS,
            "going": _going(user),
            "bookings": _bookings(request) if shown in ("all", "events") else [],
            "form": form,
            "meeting_form": meeting_form if can_meet else None,
            "can_manage": can_manage,
            "can_meet": can_meet,
            "open_event": open_event,
            "open_meeting": open_meeting,
            "now": timezone.now(),
        },
        status=status,
    )


def _calendar_rows(user, shown: str) -> list[dict]:
    """Coming events and meetings this person may see, soonest first. A
    meeting's event is shown as the meeting (it opens the meeting page)."""
    from django.urls import reverse
    from django.utils import timezone

    from comms import calendar_links
    from governance import services as governance

    since = timezone.now() - timezone.timedelta(hours=6)
    events = list(services.upcoming(user)[:200])
    by_event = governance.meetings_for_events([e.pk for e in events], user)
    made_by_meetings = _meeting_events()
    rows = []
    for e in events:
        meeting = by_event.get(e.pk)
        if meeting is not None:
            rows.append(_meeting_row(meeting, event=e))
        elif e.pk not in made_by_meetings:
            rows.append(
                {
                    "kind": "event",
                    "event": e,
                    "title": e.title,
                    "starts_at": e.starts_at,
                    "ends_at": e.ends_at,
                    "place": e.location,
                    "online": bool(e.join_url),
                    "cancelled": e.cancelled,
                    "url": reverse("event_detail", args=[e.pk]),
                    "item": calendar_links.item_for_event(e),
                    "ics_url": reverse("event_ics", args=[e.pk]),
                }
            )
    for m in governance.upcoming_meetings_off_calendar(user, since)[:200]:
        rows.append(_meeting_row(m))
    if shown == "meetings":
        rows = [r for r in rows if r["kind"] == "meeting"]
    elif shown == "events":
        rows = [r for r in rows if r["kind"] == "event"]
    rows.sort(key=lambda r: r["starts_at"])
    return rows[:200]


def _meeting_row(meeting, event=None) -> dict:
    from django.urls import reverse

    from comms import calendar_links

    return {
        "kind": "meeting",
        "meeting": meeting,
        "event": event,
        "title": meeting.title,
        "kind_label": meeting.get_kind_display(),
        "executive": meeting.kind == "executive",
        "starts_at": meeting.starts_at,
        "ends_at": meeting.ends_at,
        "place": meeting.location,
        "online": bool(meeting.join_url),
        "cancelled": meeting.status == "cancelled",
        "url": reverse("meeting_detail", args=[meeting.pk]),
        "item": calendar_links.item_for_meeting(meeting),
        "ics_url": reverse("meeting_ics", args=[meeting.pk]),
    }


def _past_meetings(user) -> list[dict]:
    """Past meetings with minutes: approved minutes for everyone who may see
    the meeting; the board and manager also see draft minutes (D34-fixgov)."""
    from django.utils import timezone

    from governance import services as governance

    staff = policy.can(user, "meeting.manage")
    out = []
    for row in governance.past_meetings(user, timezone.now()):
        m = row["meeting"]
        out.append(
            {
                "meeting": m,
                "approval": row["approval"],
                "minutes_id": m.minutes_document_id if (row["approval"] or staff) else None,
            }
        )
    return out


@login_required
def event_ics(request, pk):
    """The event as an .ics file for the person's own calendar, after
    sign-in and through the event's own scoping (404 otherwise). An event a
    meeting made is the meeting's file (same UID as on the meeting page)."""
    from comms import calendar_links

    policy.require(request.user, "calendar.view")
    event = get_object_or_404(Event.objects.for_user(request.user), pk=pk)
    from governance.services import meeting_for_event

    meeting = meeting_for_event(event, request.user)
    item = (
        calendar_links.item_for_meeting(meeting)
        if meeting is not None
        else calendar_links.item_for_event(event)
    )
    return calendar_links.ics_response(item)


def _meeting_events() -> set[int]:
    """Events a meeting made: they are cancelled from the meeting page."""
    from governance.services import meeting_event_ids

    return meeting_event_ids()


def _going(user) -> set[int]:
    from comms.rsvp import going_event_ids

    return going_event_ids(user)


def _bookings(request) -> list[dict]:
    """Confirmed common-area bookings (no names) when reservations are on."""
    from core import features

    if not features.enabled("reservations", request):
        return []
    if not policy.can(request.user, "reservation.view"):
        return []
    from community.reservations import calendar_entries

    return calendar_entries(request.user)


@login_required
@require_POST
def event_cancel(request, pk):
    policy.require(request.user, "calendar.manage")
    event = get_object_or_404(Event.objects.for_user(request.user), pk=pk)
    if event.pk in _meeting_events():
        messages.error(request, "This is a meeting: cancel it from the meeting page.")
        return redirect("calendar")
    try:
        services.cancel_event(event, actor=request.user, request=request)
    except services.CommsError as exc:
        messages.error(request, str(exc))
    return redirect("calendar")


@login_required
@require_POST
def thread_post(request, kind, pk):
    """Post on a subject's thread; the subject is fetched through its own
    scoping first, so out-of-scope subjects are 404."""
    from django.urls import reverse

    from comms import threads

    if kind not in threads.KINDS:
        from django.http import Http404

        raise Http404
    obj = threads.subject_for(kind, pk, request.user)
    try:
        threads.post(
            kind,
            obj,
            body=request.POST.get("body", ""),
            internal=request.POST.get("internal") == "on",
            actor=request.user,
            request=request,
        )
    except threads.ThreadError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Message posted.")
    return redirect(reverse(threads.DETAIL_URLS[kind], args=[obj.pk]) + "#thread")


@login_required
def faq(request):
    from comms.models import FaqEntry

    policy.require(request.user, "announcement.view")
    can_manage = policy.can(request.user, "faq.manage")
    if request.method == "POST":
        policy.require(request.user, "faq.manage")
        entry = None
        if request.POST.get("pk"):
            entry = get_object_or_404(FaqEntry, pk=request.POST["pk"])
        try:
            services.save_faq(
                question=request.POST.get("question", ""),
                answer=request.POST.get("answer", ""),
                category=request.POST.get("category", ""),
                sort_order=request.POST.get("sort_order") or 100,
                published=request.POST.get("published") == "on",
                entry=entry,
                actor=request.user,
                request=request,
            )
        except (services.CommsError, ValueError) as exc:
            text = str(exc) if isinstance(exc, services.CommsError) else "Check the order number."
            messages.error(request, text)
        else:
            messages.success(request, "Saved.")
        return redirect("faq")
    entries = FaqEntry.objects.all() if can_manage else FaqEntry.objects.filter(published=True)
    q = " ".join((request.GET.get("q") or "").split())[:100]
    if q:
        from django.db.models import Q

        entries = entries.filter(Q(question__icontains=q) | Q(answer__icontains=q))
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(e.category or "General", []).append(e)
    return render(request, "comms/faq.html", {"groups": groups, "can_manage": can_manage, "q": q})
