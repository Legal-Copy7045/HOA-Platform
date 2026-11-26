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
    form = AnnouncementForm(request.POST or None)
    if request.method == "POST":
        policy.require(request.user, "announcement.publish")
        if form.is_valid():
            try:
                services.publish_announcement(
                    actor=request.user, request=request, **form.cleaned_data
                )
            except services.CommsError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Published. Members are being emailed a link.")
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
        },
    )


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
