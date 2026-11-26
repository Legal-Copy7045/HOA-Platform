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
    location = forms.CharField(max_length=200, required=False)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    audience = forms.ChoiceField(choices=Audience.choices[:2])


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
    policy.require(request.user, "calendar.view")
    form = EventForm(request.POST or None)
    if request.method == "POST":
        policy.require(request.user, "calendar.manage")
        if form.is_valid():
            try:
                services.create_event(actor=request.user, request=request, **form.cleaned_data)
            except services.CommsError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Event added.")
            return redirect("calendar")
    return render(
        request,
        "comms/calendar.html",
        {
            "events": services.upcoming(request.user)[:200],
            "going": _going(request.user),
            "bookings": _bookings(request),
            "form": form,
            "can_manage": policy.can(request.user, "calendar.manage"),
        },
    )


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
