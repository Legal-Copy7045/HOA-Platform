"""Paper mail pages (D55): board and manager only (``mail.print``).

Every page checks the permission first and fetches mailings through their
scoped manager (owners' scope is always empty), so anyone else gets 403 or
404 on every URL, downloads included.
"""

from __future__ import annotations

import datetime

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from comms import papermail
from comms.models import Mailing, MailingAudience, MailingKind, WindowStyle
from core import policy
from documents.attachments import pdf_response


class ComposeForm(forms.Form):
    subject = forms.CharField(label='Subject (the "RE:" line)', max_length=200)
    body = forms.CharField(
        label="Letter",
        widget=forms.Textarea(attrs={"rows": 12}),
        max_length=papermail.MAX_BODY,
        help_text='"Dear ...:" and the signature are added for you.',
    )
    audience = forms.ChoiceField(
        label="Who gets it",
        choices=[
            ("paper", "Owners who get paper (not agreed to email notices)"),
            ("all", "Every owner, whatever they chose (for mailings that must go to all)"),
        ],
        widget=forms.RadioSelect,
        initial="paper",
    )
    home = forms.ChoiceField(label="Only one home", required=False)

    def __init__(self, *args, homes=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["home"].choices = [("", "(every home)")] + [
            (str(lot.pk), lot.label) for lot in homes
        ]


class SettingsForm(forms.Form):
    window = forms.ChoiceField(
        label="Window envelopes", choices=WindowStyle.choices, widget=forms.RadioSelect
    )
    address_left_in = forms.DecimalField(
        label="Recipient block: from the left edge (inches)", decimal_places=2
    )
    address_top_in = forms.DecimalField(
        label="Recipient block: from the top edge (inches)", decimal_places=2
    )
    return_left_in = forms.DecimalField(
        label="Return address (double window): from the left (inches)", decimal_places=2
    )
    return_top_in = forms.DecimalField(
        label="Return address (double window): from the top (inches)", decimal_places=2
    )
    envelope_shift_x_in = forms.DecimalField(
        label="Envelopes: move right by (inches, negative moves left)", decimal_places=2
    )
    envelope_shift_y_in = forms.DecimalField(
        label="Envelopes: move up by (inches, negative moves down)", decimal_places=2
    )
    label_shift_x_in = forms.DecimalField(label="Labels: move right by (inches)", decimal_places=2)
    label_shift_y_in = forms.DecimalField(label="Labels: move down by (inches)", decimal_places=2)
    duplex = forms.BooleanField(
        label="Printing on both sides: start every letter on a front side", required=False
    )
    imb_enabled = forms.BooleanField(label="Print the tracking barcode", required=False)
    mailer_id = forms.CharField(label="USPS Mailer ID", max_length=9, required=False)
    barcode_id = forms.CharField(label="Barcode ID", max_length=2)
    service_type = forms.ChoiceField(label="Service Type ID", choices=papermail.SERVICE_TYPES)


def _require(request):
    policy.require(request.user, "mail.print")


def _homes(user):
    from community.models import Lot, sorted_homes

    return sorted_homes(Lot.objects.for_user(user))


def _source(request, data) -> tuple[dict, object]:
    """(defaults, source object for the page) from meeting / budget /
    template ids, each fetched through its own scoping."""
    from governance.models import Meeting
    from ledger.models import Budget

    meeting_id = _int(data.get("meeting"))
    budget_id = _int(data.get("budget"))
    template = data.get("template") or ""
    if meeting_id:
        meeting = get_object_or_404(Meeting.objects.for_user(request.user), pk=meeting_id)
        return papermail.source_defaults(meeting=meeting), meeting
    if budget_id:
        if not policy.can(request.user, "finance.view"):
            raise papermail.PaperMailError("Budgets are not open to you.")
        budget = get_object_or_404(Budget, pk=budget_id)
        stage = data.get("stage") or "proposed"
        return papermail.source_defaults(budget=budget, stage=stage), budget
    if template:
        # Delinquency notices go only from the home's collection case, one
        # home at a time ("Also prepare a paper copy", WT-006), never to a
        # group of homes from here.
        raise papermail.PaperMailError(
            "Delinquency notices are prepared from the home's collection case "
            "(Finance, Collections), one home at a time."
        )
    return papermail.source_defaults(), None


def _int(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@login_required
def paper_mail(request):
    """Who gets paper now, the ways to start a mailing, and the mailings."""
    _require(request)
    from governance.models import Meeting
    from ledger.models import Budget

    groups = papermail.who_gets_paper()
    mailings = list(Mailing.objects.for_user(request.user)[:50])
    events = {}
    if mailings:
        from comms.models import MailingEvent

        events = {
            e.mailing_id: e
            for e in MailingEvent.objects.for_user(request.user).filter(
                mailing__in=[m.pk for m in mailings]
            )
        }
    for m in mailings:
        m.closing = events.get(m.pk)
    meetings = (
        Meeting.objects.for_user(request.user)
        .exclude(kind="executive")
        .filter(status="scheduled", starts_at__gte=timezone.now() - datetime.timedelta(days=1))
        .order_by("starts_at")
    )
    return render(
        request,
        "comms/paper_mail.html",
        {
            "groups": groups,
            "counts": papermail.counts(groups),
            "mailings": mailings,
            "meetings": meetings,
            "budgets": Budget.objects.all()[:5] if policy.can(request.user, "finance.view") else [],
            "homes": _homes(request.user),
            "settings": papermail.settings(),
        },
    )


@login_required
def paper_mail_new(request):
    _require(request)
    data = request.POST if request.method == "POST" else request.GET
    try:
        defaults, source = _source(request, data)
    except papermail.PaperMailError as exc:
        messages.error(request, str(exc))
        return redirect("paper_mail")
    homes = _homes(request.user)
    preview = None
    if request.method == "POST":
        form = ComposeForm(request.POST, homes=homes)
        if form.is_valid():
            everyone = form.cleaned_data["audience"] == "all"
            lot_id = _int(form.cleaned_data["home"])
            args = {
                "actor": request.user,
                "defaults": defaults,
                "subject": form.cleaned_data["subject"],
                "body": form.cleaned_data["body"],
                "everyone": everyone,
                "lot_id": lot_id,
            }
            try:
                if request.POST.get("step") == "create":
                    mailing = papermail.create_mailing(request=request, **args)
                    messages.success(
                        request,
                        f"Mailing prepared: {mailing.letters_count} letter(s). Print them below.",
                    )
                    return redirect("paper_mail_detail", pk=mailing.pk)
                preview = papermail.preview(**args)
            except papermail.PaperMailError as exc:
                form.add_error(None, str(exc))
    else:
        initial = {"subject": defaults.get("subject", ""), "body": defaults.get("body", "")}
        if data.get("everyone") == "1":
            initial["audience"] = "all"
        if _int(data.get("home")):
            initial["home"] = str(_int(data.get("home")))
        form = ComposeForm(initial=initial, homes=homes)
    return render(
        request,
        "comms/paper_mail_new.html",
        {
            "form": form,
            "defaults": defaults,
            "source": source,
            "kind_label": MailingKind(defaults["kind"]).label,
            "placeholders": papermail.placeholders_for(defaults["kind"]),
            "preview": preview,
            "hidden": {
                k: data.get(k) for k in ("meeting", "budget", "stage", "template") if data.get(k)
            },
        },
        status=400 if form.errors else 200,
    )


def _mailing(request, pk) -> Mailing:
    _require(request)
    return get_object_or_404(Mailing.objects.for_user(request.user), pk=pk)


@login_required
def paper_mail_detail(request, pk):
    mailing = _mailing(request, pk)
    from comms.models import MailPiece

    closing = papermail.closing_event(mailing)
    letters = papermail._letters(mailing)
    first = papermail.rendered_text(mailing, letters[0]) if letters else None
    meeting_title = (
        papermail.meeting_titles([mailing.meeting_id]).get(mailing.meeting_id)
        if mailing.meeting_id
        else None
    )
    from comms import usps_tracking

    tracking = None
    if usps_tracking.feature_on(request):
        tracking = usps_tracking.mailing_tracking(mailing, letters)
    return render(
        request,
        "comms/paper_mail_detail.html",
        {
            "tracking": tracking,
            "sorted_meaning": usps_tracking.SORTED_MEANING,
            "m": mailing,
            "closing": closing,
            "first": first,
            "meeting_title": meeting_title,
            "audience_label": MailingAudience(mailing.audience).label,
            "kind_label": MailingKind(mailing.kind).label,
            "settings": papermail.settings(),
            "pieces": MailPiece.objects.for_user(request.user).filter(mailing=mailing).count(),
            "today": timezone.localdate().isoformat(),
        },
    )


@login_required
def paper_mail_letters(request, pk):
    mailing = _mailing(request, pk)
    first_only = request.GET.get("first") == "1"
    duplex = request.GET.get("duplex") == "1"
    try:
        data, pages = papermail.letters_pdf(mailing, duplex=duplex, first_only=first_only)
    except papermail.PaperMailError as exc:
        messages.error(request, str(exc))
        return redirect("paper_mail_detail", pk=mailing.pk)
    papermail.audit_printed(
        mailing,
        actor=request.user,
        request=request,
        fmt="letter_preview" if first_only else "letters",
        pages=pages,
    )
    return pdf_response(data, f"paper-mail-{mailing.pk}-letters.pdf")


@login_required
def paper_mail_envelopes(request, pk):
    mailing = _mailing(request, pk)
    try:
        data, pages = papermail.envelopes_pdf(mailing)
    except papermail.PaperMailError as exc:
        messages.error(request, str(exc))
        return redirect("paper_mail_detail", pk=mailing.pk)
    papermail.audit_printed(
        mailing, actor=request.user, request=request, fmt="envelopes", pages=pages
    )
    return pdf_response(data, f"paper-mail-{mailing.pk}-envelopes.pdf")


@login_required
def paper_mail_labels(request, pk):
    mailing = _mailing(request, pk)
    try:
        data, pages = papermail.labels_pdf(mailing, start=_int(request.GET.get("start")) or 1)
    except papermail.PaperMailError as exc:
        messages.error(request, str(exc))
        return redirect("paper_mail_detail", pk=mailing.pk)
    papermail.audit_printed(mailing, actor=request.user, request=request, fmt="labels", pages=pages)
    return pdf_response(data, f"paper-mail-{mailing.pk}-labels.pdf")


@login_required
@require_POST
def paper_mail_mark(request, pk):
    mailing = _mailing(request, pk)
    discard = request.POST.get("discard") == "1"
    mailed_on = None
    if not discard:
        try:
            mailed_on = datetime.date.fromisoformat(request.POST.get("mailed_on", ""))
        except ValueError:
            messages.error(request, "Enter the date it was mailed.")
            return redirect("paper_mail_detail", pk=mailing.pk)
    try:
        papermail.record_mailed(
            mailing, actor=request.user, mailed_on=mailed_on, discard=discard, request=request
        )
    except papermail.PaperMailError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request, "Marked as not sent." if discard else "Marked as mailed. Thank you."
        )
    return redirect("paper_mail_detail", pk=mailing.pk)


@login_required
@require_POST
def paper_mail_piece_checked(request, pk):
    """USPS tracking (D60): the address of a letter with no scan was checked."""
    mailing = _mailing(request, pk)
    from comms import usps_tracking
    from comms.models import MailPiece

    piece = get_object_or_404(
        MailPiece.objects.for_user(request.user).filter(mailing=mailing),
        pk=_int(request.POST.get("piece")) or 0,
    )
    try:
        usps_tracking.mark_checked(piece, actor=request.user, request=request)
    except ValueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Marked as checked. It leaves My items.")
    return redirect(reverse("paper_mail_detail", args=[mailing.pk]) + "#tracking")


_SETTING_FIELDS = tuple(SettingsForm.base_fields)


@login_required
def paper_mail_settings(request):
    _require(request)
    current = papermail.settings()
    if request.method == "POST":
        form = SettingsForm(request.POST)
        if form.is_valid():
            try:
                papermail.save_settings(actor=request.user, request=request, **form.cleaned_data)
            except papermail.PaperMailError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Paper mail settings saved.")
                return redirect("paper_mail_settings")
    else:
        form = SettingsForm(initial={f: getattr(current, f) for f in _SETTING_FIELDS})
    return render(
        request,
        "comms/paper_mail_settings.html",
        {
            "form": form,
            "current": current,
            "service_types": papermail.SERVICE_TYPES,
            "usps": _usps_status(request),
            "stid_url": papermail.STID_URL,
        },
        status=400 if form.errors else 200,
    )


def _usps_status(request) -> dict | None:
    from comms import usps_tracking

    if not usps_tracking.feature_on(request):
        return None
    return usps_tracking.setup_status()


@login_required
def paper_mail_test(request):
    _require(request)
    kind = request.GET.get("kind", "letter")
    try:
        data = papermail.test_pdf(kind)
    except papermail.PaperMailError as exc:
        messages.error(request, str(exc))
        return redirect("paper_mail_settings")
    return pdf_response(
        data, f"alignment-test-{kind if kind in ('envelope', 'labels') else 'letter'}.pdf"
    )
