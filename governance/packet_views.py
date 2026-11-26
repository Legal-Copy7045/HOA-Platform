"""Meeting packet pages (M5): build on the meeting's Meeting packet page,
download the .zip from the meeting page."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from core import features, policy
from documents.models import Document
from governance import packets
from governance.models import Meeting, MeetingKind, MeetingPacket, PacketVariant


class PacketForm(forms.Form):
    minutes = forms.ModelChoiceField(
        queryset=Document.objects.none(),
        required=False,
        label="Draft minutes for approval",
        help_text="From Documents, category Minutes. Listed first in the attachment index.",
    )
    attachments = forms.ModelMultipleChoiceField(
        queryset=Document.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Attachments",
    )

    def __init__(self, *args, user, meeting, variant, **kwargs):
        super().__init__(*args, prefix=variant, **kwargs)
        self.variant = variant
        self.fields["attachments"].queryset = packets.attachment_choices(user, variant)
        if variant == PacketVariant.BOARD:
            self.fields["minutes"].queryset = packets.minutes_choices(user)
            self.fields["minutes"].initial = packets.default_draft_minutes(user, meeting)
        else:
            del self.fields["minutes"]
            self.fields["attachments"].help_text = "Only documents every owner can already see."

    def clean_attachments(self):
        chosen = self.cleaned_data["attachments"]
        if len(chosen) > packets.MAX_ATTACHMENTS:
            raise forms.ValidationError(f"Attach at most {packets.MAX_ATTACHMENTS} documents.")
        return chosen


@login_required
def meeting_packet(request, pk):
    policy.require(request.user, "meeting.packet_build")
    meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=pk)
    variants = [PacketVariant.BOARD]
    if meeting.kind != MeetingKind.EXECUTIVE:
        variants.append(PacketVariant.OWNER)
    posted = request.POST.get("variant") if request.method == "POST" else None
    if posted is not None and posted not in variants:
        raise Http404
    forms_by_variant = {
        v: PacketForm(
            request.POST if posted == v else None, user=request.user, meeting=meeting, variant=v
        )
        for v in variants
    }
    if posted:
        form = forms_by_variant[posted]
        if form.is_valid():
            minutes = form.cleaned_data.get("minutes")
            try:
                packets.build_packet(
                    meeting,
                    variant=posted,
                    attachment_ids=[d.pk for d in form.cleaned_data["attachments"]],
                    minutes_id=minutes.pk if minutes else None,
                    actor=request.user,
                    request=request,
                )
            except packets.PacketError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(
                    request,
                    "Board packet saved in Documents (board only)."
                    if posted == PacketVariant.BOARD
                    else "Owner packet published: owners can open it from the meeting page.",
                )
                return redirect("meeting_packet", pk=meeting.pk)
    history = (
        MeetingPacket.objects.filter(meeting=meeting)
        .select_related("version", "built_by")
        .prefetch_related("items")
    )
    return render(
        request,
        "governance/meeting_packet.html",
        {
            "m": meeting,
            "forms": [(v, PacketVariant(v).label, forms_by_variant[v]) for v in variants],
            "open_variant": posted,
            "history": history,
            "latest": packets.visible_packets(request.user, meeting),
            # The crumb goes straight to the meetings list (on the Calendar
            # when that feature is on, D49), never through a redirect.
            "calendar_on": features.enabled("calendar", request),
        },
    )


@login_required
def meeting_packet_zip(request, pk, packet_id):
    meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=pk)
    packet = packets.packet_for(request.user, meeting, packet_id)
    if packet is None:
        raise Http404
    try:
        stream, filename = packets.packet_zip(packet, user=request.user, request=request)
    except packets.PacketError as exc:  # includes PacketBusy (rate limit)
        messages.error(request, str(exc))
        return redirect("meeting_detail", pk=meeting.pk)
    # Streamed chunk by chunk (SEC-002): the archive is never held in memory.
    response = StreamingHttpResponse(stream, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response
