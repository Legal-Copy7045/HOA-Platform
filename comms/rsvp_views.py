"""Event page with RSVPs and volunteer sign-ups (comms.rsvp)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from comms import rsvp
from comms.models import Event, RsvpAnswer, VolunteerSlot
from core import policy
from core.lists import csv_response


class RsvpForm(forms.Form):
    answer = forms.ChoiceField(
        label="Your answer", choices=RsvpAnswer.choices, widget=forms.RadioSelect
    )
    guests = forms.IntegerField(
        label="Guests coming with you",
        min_value=0,
        max_value=rsvp.MAX_GUESTS_LIMIT,
        initial=0,
        required=False,
    )


class RsvpSettingsForm(forms.Form):
    enabled = forms.BooleanField(label="Take RSVPs for this event", required=False)
    cap = forms.IntegerField(
        label="Headcount limit (guests included)",
        min_value=1,
        max_value=10_000,
        required=False,
        help_text="Leave empty for no limit.",
    )
    max_guests = forms.IntegerField(
        label="Guests each person may bring",
        min_value=0,
        max_value=rsvp.MAX_GUESTS_LIMIT,
        initial=0,
        required=False,
    )
    deadline = forms.DateTimeField(
        label="Answers close",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="Leave empty to close when the event starts.",
    )
    remind = forms.BooleanField(
        label="Email a reminder the day before to people going", required=False
    )


class SlotForm(forms.Form):
    title = forms.CharField(
        label="What volunteers will do", max_length=200, help_text='For example "Bring a dessert".'
    )
    capacity = forms.IntegerField(label="People needed", min_value=1, max_value=500, initial=1)


def _event(request, pk) -> Event:
    return get_object_or_404(Event.objects.for_user(request.user), pk=pk)


def _settings_initial(event: Event) -> dict:
    deadline = event.rsvp_deadline
    return {
        "enabled": event.rsvp_enabled,
        "cap": event.rsvp_cap,
        "max_guests": event.rsvp_max_guests,
        "deadline": timezone.localtime(deadline).strftime("%Y-%m-%dT%H:%M") if deadline else None,
        "remind": event.rsvp_remind,
    }


def _render(request, event, *, rsvp_form=None, settings_form=None, slot_form=None, status=200):
    user = request.user
    mine = rsvp.own_answer(event, user)
    can_answer = policy.can(user, "event.rsvp")
    can_manage = policy.can(user, "event.rsvp_manage")
    names_ok = policy.can(user, "event.rsvp_names")
    if rsvp_form is None:
        initial = {"answer": mine.answer, "guests": mine.guests} if mine else {}
        rsvp_form = RsvpForm(initial=initial)
    context = {
        "e": event,
        "mine": mine,
        "counts": rsvp.counts(event) if event.rsvp_enabled else None,
        "slots": rsvp.slots_for(event, user),
        "is_open": not event.cancelled and timezone.now() < event.answers_close_at,
        "can_answer": can_answer,
        "can_manage": can_manage,
        "rsvp_form": rsvp_form,
        "settings_form": settings_form or RsvpSettingsForm(initial=_settings_initial(event)),
        "slot_form": slot_form or SlotForm(),
        "names": rsvp.answers_with_names(event, user) if names_ok and event.rsvp_enabled else None,
        "names_ok": names_ok,
        "meeting": _meeting_for(event, user),
    }
    return render(request, "comms/event_detail.html", context, status=status)


def _meeting_for(event, user):
    """The meeting behind this event (made by governance), to link to it."""
    from governance.services import meeting_for_event

    return meeting_for_event(event, user)


@login_required
@require_GET
def event_detail(request, pk):
    policy.require(request.user, "calendar.view")
    return _render(request, _event(request, pk))


@login_required
@require_POST
def event_rsvp(request, pk):
    policy.require(request.user, "event.rsvp")
    event = _event(request, pk)
    form = RsvpForm(request.POST)
    if form.is_valid():
        try:
            rsvp.answer(
                event,
                answer=form.cleaned_data["answer"],
                guests=form.cleaned_data.get("guests") or 0,
                actor=request.user,
                request=request,
            )
        except rsvp.RsvpError as exc:
            form.add_error(None, str(exc))
        except PermissionDenied as exc:
            raise Http404 from exc
        else:
            messages.success(request, "Thanks, your answer is saved.")
            return redirect("event_detail", pk=event.pk)
    return _render(request, event, rsvp_form=form)


@login_required
@require_POST
def event_rsvp_settings(request, pk):
    policy.require(request.user, "event.rsvp_manage")
    event = _event(request, pk)
    form = RsvpSettingsForm(request.POST)
    if form.is_valid():
        data = form.cleaned_data
        try:
            rsvp.save_settings(
                event,
                enabled=data["enabled"],
                cap=data["cap"],
                max_guests=data["max_guests"] or 0,
                deadline=data["deadline"],
                remind=data["remind"],
                actor=request.user,
                request=request,
            )
        except rsvp.RsvpError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "RSVP settings saved.")
            return redirect("event_detail", pk=event.pk)
    return _render(request, event, settings_form=form)


@login_required
@require_POST
def event_slot_add(request, pk):
    policy.require(request.user, "event.rsvp_manage")
    event = _event(request, pk)
    form = SlotForm(request.POST)
    if form.is_valid():
        try:
            rsvp.add_slot(
                event,
                title=form.cleaned_data["title"],
                capacity=form.cleaned_data["capacity"],
                actor=request.user,
                request=request,
            )
        except rsvp.RsvpError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, "Volunteer slot added.")
            return redirect("event_detail", pk=event.pk)
    return _render(request, event, slot_form=form)


@login_required
@require_POST
def event_slot_action(request, pk, action):
    slot = get_object_or_404(VolunteerSlot, pk=pk)
    # The slot's event must be visible to this person; otherwise 404.
    event = _event(request, slot.event_id)
    try:
        if action == "join":
            policy.require(request.user, "event.rsvp")
            rsvp.sign_up(slot, actor=request.user, request=request)
            messages.success(request, "You are signed up. Thank you!")
        elif action == "leave":
            policy.require(request.user, "event.rsvp")
            rsvp.leave(slot, actor=request.user, request=request)
            messages.success(request, "You have left that slot.")
        elif action == "remove":
            policy.require(request.user, "event.rsvp_manage")
            rsvp.remove_slot(slot, actor=request.user, request=request)
            messages.success(request, "Slot removed.")
        else:
            raise Http404
    except rsvp.RsvpError as exc:
        messages.error(request, str(exc))
    return redirect("event_detail", pk=event.pk)


@login_required
@require_GET
def event_rsvp_export(request, pk):
    policy.require(request.user, "event.rsvp_names")
    event = _event(request, pk)
    header, rows = rsvp.export_rows(event, request.user)
    return csv_response(request, f"event-{event.pk}-rsvps", header, rows, audit_kind="event_rsvps")
