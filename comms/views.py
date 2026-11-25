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
    audience = forms.ChoiceField(choices=Audience.choices)
    urgent = forms.BooleanField(required=False)


class EventForm(forms.Form):
    title = forms.CharField(max_length=200)
    starts_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    ends_at = forms.DateTimeField(
        required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    location = forms.CharField(max_length=200, required=False)
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    audience = forms.ChoiceField(choices=Audience.choices)


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
            "form": form,
            "can_manage": policy.can(request.user, "calendar.manage"),
        },
    )


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
