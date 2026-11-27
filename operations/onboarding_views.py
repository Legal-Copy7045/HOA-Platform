"""Vendor onboarding pages (D79).

Two halves:

* The public form (``vendor_onboarding_start`` ... ``_done``): the only pages
  besides the sign-in steps that open without signing in. The emailed token
  is swapped for a short session bound to that one round and the address bar
  is redirected to a token-free URL. Every page refuses a signed-in user,
  answers unknown, expired, replaced and used links the same way, is rate
  limited per client and per link, never cached, sent with a strict Content
  Security Policy and no referrer, and shows nothing of the portal: no menu,
  no links, nothing about the association beyond its name and what the
  vendor must provide. The vendor sees only their own draft and the names of
  files they uploaded (never the files themselves).
* Board and manager pages under Finance → Vendors.
"""

from __future__ import annotations

import hashlib
import hmac

from csp.decorators import csp_replace
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core import policy
from core.money import parse_cents
from core.net import client_ip
from operations import vendor_crypto
from operations import vendor_onboarding as vo
from operations.models import (
    OPEN_ROUND_STATES,
    EntityType,
    OnboardingRound,
    RoundMode,
    RoundStatus,
    Vendor,
    VendorFile,
    VendorType,
)

SESSION_LINK = "vendor_onboarding_link"
SESSION_HASH = "vendor_onboarding_hash"
SESSION_STARTED = "vendor_onboarding_started"

# The vendor pages run no script, load nothing from elsewhere and cannot be
# framed; forms post only to this site.
PUBLIC_CSP = {
    "default-src": ["'none'"],
    "script-src": ["'none'"],
    "connect-src": ["'none'"],
    "worker-src": ["'none'"],
    "manifest-src": ["'none'"],
    "img-src": ["'self'"],
    "style-src": ["'self'"],
    "font-src": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
    "base-uri": ["'none'"],
    "object-src": ["'none'"],
}
# (per hour) opening links per client and per link; form posts per client and
# per round.
LIMITS = {"open_ip": 30, "open_token": 10, "post_ip": 240, "post_round": 120}  # nosec B105
INVALID = "operations/onboarding/public_invalid.html"


def _public(view):
    """Headers every public vendor page carries."""

    def wrapped(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["Referrer-Policy"] = "no-referrer"
        response["X-Robots-Tag"] = "noindex, nofollow"
        response["Cache-Control"] = "no-store, private"
        return response

    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return csp_replace(PUBLIC_CSP)(never_cache(wrapped))


def _bucket(kind: str, key: str) -> str:
    digest = hmac.new(settings.SECRET_KEY.encode(), key.encode(), hashlib.sha256).hexdigest()
    return f"vo-rl:{kind}:{digest[:40]}"


def limited(kind: str, key: str) -> bool:
    """Count one hit; True once the hourly limit for ``kind`` is passed."""
    bucket = _bucket(kind, key or "-")
    cache.add(bucket, 0, timeout=3600)
    try:
        count = cache.incr(bucket)
    except ValueError:
        cache.set(bucket, 1, timeout=3600)
        count = 1
    return count > LIMITS[kind]


def _too_many(request):
    return render(request, "operations/onboarding/public_limited.html", status=429)


def _neutral(request, status=404, **ctx):
    return render(request, INVALID, ctx, status=status)


def _forget(request) -> None:
    for key in (SESSION_LINK, SESSION_HASH, SESSION_STARTED):
        request.session.pop(key, None)


def _session_round(request):
    """The round this browser's session may work on, or None."""
    if request.user.is_authenticated:
        return None
    link_id = request.session.get(SESSION_LINK)
    stored = request.session.get(SESSION_HASH) or ""
    started = request.session.get(SESSION_STARTED) or 0
    if not link_id or not stored:
        return None
    if timezone.now().timestamp() - float(started) > vo.SESSION_SECONDS:
        _forget(request)
        return None
    from operations.models import OnboardingLink

    link = OnboardingLink.objects.select_related("round__vendor").filter(pk=link_id).first()
    if (
        link is None
        or not hmac.compare_digest(link.token_hash or "", stored)
        or not vo.link_usable(link)
    ):
        _forget(request)
        return None
    return link.round


@_public
@require_GET
def start(request, token):
    """Swap the emailed token for a short session and leave the URL bar."""
    if request.user.is_authenticated:
        return _neutral(request, status=400, signed_in=True)
    ip = client_ip(request) or "-"
    if limited("open_ip", ip) or limited("open_token", hashlib.sha256(token.encode()).hexdigest()):
        return _too_many(request)
    link = vo.find_open_link(token)
    if link is None:
        return _neutral(request)
    request.session.cycle_key()
    _forget(request)
    request.session[SESSION_LINK] = link.pk
    request.session[SESSION_HASH] = link.token_hash
    request.session[SESSION_STARTED] = timezone.now().timestamp()
    request.session.set_expiry(vo.SESSION_SECONDS)
    vo.record_open(link, request=request)
    return redirect("vendor_onboarding_form")


# --- The vendor's form -------------------------------------------------------------------


def _date_field(label):
    return forms.DateField(
        label=label, required=False, widget=forms.DateInput(attrs={"type": "date"})
    )


class MoneyText(forms.CharField):
    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return None
        try:
            cents = parse_cents(value)
        except ValueError as exc:
            raise forms.ValidationError("Enter an amount like 1,000,000.") from exc
        if cents <= 0 or cents > 10_000_000_000_00:
            raise forms.ValidationError("Enter the policy limit in dollars.")
        return cents


class VendorInfoForm(forms.Form):
    """Every field is optional while saving a draft; Submit checks what the
    round needs (vendor_onboarding.missing)."""

    legal_name = forms.CharField(
        label="Legal name (as on your W-9)", max_length=200, required=False
    )
    dba = forms.CharField(label="Doing business as (if different)", max_length=200, required=False)
    entity_type = forms.ChoiceField(
        label="How is the business taxed?",
        choices=[("", "Choose…"), *EntityType.choices],
        required=False,
    )
    entity_other = forms.CharField(label="If other, what kind", max_length=100, required=False)
    contact_name = forms.CharField(label="Contact name", max_length=120, required=False)
    contact_email = forms.EmailField(label="Contact email", required=False)
    contact_phone = forms.CharField(label="Contact phone", max_length=40, required=False)
    remit_address = forms.CharField(
        label="Remit-to address (where checks are mailed)",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    services = forms.CharField(
        label="Services you provide",
        max_length=300,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    w9_revision_confirmed = forms.BooleanField(
        label=(
            f"My uploaded W-9 is the current IRS revision ({vo.CURRENT_W9_REVISION}) and is signed"
        ),
        required=False,
    )
    coi_gl_carrier = forms.CharField(
        label="General liability insurer", max_length=120, required=False
    )
    coi_gl_amount_cents = MoneyText(
        label="General liability limit per occurrence ($)", required=False
    )
    coi_gl_expires = _date_field("General liability policy expires")
    coi_auto_carrier = forms.CharField(
        label="Auto liability insurer", max_length=120, required=False
    )
    coi_auto_amount_cents = MoneyText(label="Auto liability limit ($)", required=False)
    coi_auto_expires = _date_field("Auto policy expires")
    workers_comp_mode = forms.ChoiceField(
        label="Workers' compensation",
        choices=[
            ("", "Choose…"),
            ("certificate", "I carry workers' compensation insurance"),
            ("exempt", "I am exempt under Pennsylvania law (no employees)"),
        ],
        required=False,
    )
    workers_comp_carrier = forms.CharField(
        label="Workers' compensation insurer", max_length=120, required=False
    )
    workers_comp_expires = _date_field("Workers' compensation policy expires")
    hic_registration = forms.CharField(
        label="PA Home Improvement Contractor registration number (PA + 6 digits)",
        max_length=20,
        required=False,
    )
    licence_text = forms.CharField(
        label="Licences or registrations (type, number, state)",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    conflict = forms.ChoiceField(
        label=(
            "Is the business, or anyone who owns or works for it, related to, owned by or "
            "otherwise connected with a board member or a homeowner of the community?"
        ),
        choices=[("", "Choose…"), ("no", "No"), ("yes", "Yes")],
        required=False,
    )
    conflict_details = forms.CharField(
        label="If yes, who and how (names and relationship)",
        max_length=1000,
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    terms_accepted = forms.BooleanField(
        label="I have read and accept the association's vendor terms above", required=False
    )

    BASE = (
        "legal_name",
        "dba",
        "entity_type",
        "entity_other",
        "contact_name",
        "contact_email",
        "contact_phone",
        "remit_address",
        "services",
        "conflict",
        "conflict_details",
        "terms_accepted",
    )
    BY_ITEM = {
        "w9": ("w9_revision_confirmed",),
        "coi_gl": ("coi_gl_carrier", "coi_gl_amount_cents", "coi_gl_expires"),
        "coi_auto": ("coi_auto_carrier", "coi_auto_amount_cents", "coi_auto_expires"),
        "workers_comp": ("workers_comp_mode", "workers_comp_carrier", "workers_comp_expires"),
        "hic_registration": ("hic_registration",),
        "licence": ("licence_text",),
    }

    def __init__(self, *args, rnd: OnboardingRound, **kwargs):
        super().__init__(*args, **kwargs)
        keep = set()
        if rnd.mode == RoundMode.FULL:
            keep |= set(self.BASE)
        for item in vo.round_items(rnd) + vo.optional_items(rnd):
            keep |= set(self.BY_ITEM.get(item, ()))
        for name in list(self.fields):
            if name not in keep:
                del self.fields[name]

    def clean(self):
        data = super().clean()
        for name, value in list(data.items()):
            if isinstance(value, str) and name in vo.TEXT_FIELDS:
                try:
                    vo.check_free_text(
                        self.fields[name].label, value, phone=name == "contact_phone"
                    )
                except vo.OnboardingError as exc:
                    self.add_error(name, str(exc))
        return data


def _initial(rnd: OnboardingRound) -> dict:
    out = dict(rnd.answers or {})
    for key in ("coi_gl_amount_cents", "coi_auto_amount_cents"):
        if out.get(key):
            out[key] = f"{int(out[key]) // 100:,}"
    return out


def _form_context(request, rnd, form):
    version, terms = vo.current_terms()
    files = vo.current_files(rnd)
    required = vo.round_items(rnd)
    optional = vo.optional_items(rnd)
    docs = []
    for key in required + optional:
        item = vo.ITEMS[key]
        if not item.file_kind:
            continue
        f = files.get(item.file_kind)
        docs.append(
            {
                "item": item,
                "required": key in required and item.file_required,
                "file": f,
                "received": timezone.localdate(f.uploaded_at) if f else None,
            }
        )
    return {
        "association": vo.association_name(),
        "rnd": rnd,
        "form": form,
        "docs": docs,
        "terms": terms,
        "terms_version": version,
        "update": rnd.mode == RoundMode.UPDATE,
        "items": [vo.ITEMS[k] for k in required],
        "missing": vo.missing(rnd),
        "max_mb": vo.MAX_FILE_BYTES // (1024 * 1024),
        "key_ready": vendor_crypto.configured(),
    }


@_public
@require_http_methods(["GET", "POST"])
def form(request):
    rnd = _session_round(request)
    if rnd is None:
        return _neutral(request)
    if request.method == "POST":
        if limited("post_ip", client_ip(request) or "-") or limited("post_round", str(rnd.pk)):
            return _too_many(request)
        form = VendorInfoForm(request.POST, rnd=rnd)
        if form.is_valid():
            data = dict(form.cleaned_data)
            if "terms_accepted" in data:
                data["terms_version"] = vo.current_terms()[0] if data["terms_accepted"] else -1
            try:
                vo.save_answers(rnd, data)
            except vo.OnboardingError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, "Saved. You can come back to this page until you submit.")
                return redirect("vendor_onboarding_form")
        return render(
            request, "operations/onboarding/public_form.html", _form_context(request, rnd, form)
        )
    form = VendorInfoForm(initial=_initial(rnd), rnd=rnd)
    return render(
        request, "operations/onboarding/public_form.html", _form_context(request, rnd, form)
    )


@csrf_exempt
@_public
@require_POST
def upload(request):
    """One PDF per post. The size limit applies before the body is buffered
    (core.uploads.limit_uploads), then CSRF is checked."""
    from core.uploads import limit_uploads

    ok = limit_uploads(request, vo.MAX_FILE_BYTES)
    return _upload_checked(request, ok)


@csrf_protect
def _upload_checked(request, ok):
    rnd = _session_round(request)
    if rnd is None:
        return _neutral(request)
    if limited("post_ip", client_ip(request) or "-") or limited("post_round", str(rnd.pk)):
        return _too_many(request)
    if not ok or getattr(request, "upload_too_large", False):
        messages.error(
            request, f"Files must be {vo.MAX_FILE_BYTES // (1024 * 1024)} MB or smaller."
        )
        return redirect("vendor_onboarding_form")
    uploaded = request.FILES.get("file")
    if getattr(request, "upload_too_large", False):
        messages.error(
            request, f"Files must be {vo.MAX_FILE_BYTES // (1024 * 1024)} MB or smaller."
        )
        return redirect("vendor_onboarding_form")
    try:
        vo.upload_file(rnd, request.POST.get("kind", ""), uploaded, request=request)
    except vo.OnboardingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "File received.")
    return redirect("vendor_onboarding_form")


@_public
@require_POST
def submit(request):
    rnd = _session_round(request)
    if rnd is None:
        return _neutral(request)
    if limited("post_ip", client_ip(request) or "-") or limited("post_round", str(rnd.pk)):
        return _too_many(request)
    try:
        vo.submit(rnd, request=request)
    except vo.OnboardingError as exc:
        messages.error(request, str(exc))
        return redirect("vendor_onboarding_form")
    _forget(request)
    request.session.cycle_key()
    return redirect("vendor_onboarding_done")


@_public
@require_GET
def done(request):
    return render(
        request, "operations/onboarding/public_done.html", {"association": vo.association_name()}
    )


# --- Board and manager pages ------------------------------------------------------------


class InviteForm(forms.Form):
    name = forms.CharField(label="Vendor name", max_length=200)
    email = forms.EmailField(label="Vendor's email (the link goes here)")
    services = forms.CharField(label="Services", max_length=200, required=False)
    vendor_type = forms.ChoiceField(label="Kind of work", choices=VendorType.choices)


class ReasonForm(forms.Form):
    reason = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), max_length=2000)


def _review_perm(request):
    policy.require(request.user, "vendor.onboarding_review")


@login_required
@require_GET
def onboarding_list(request):
    _review_perm(request)
    return _render_list(request, InviteForm())


def _render_list(request, invite_form, status=200):
    rounds = OnboardingRound.objects.select_related("vendor").order_by("-status_changed_at")
    return render(
        request,
        "operations/onboarding/list.html",
        {
            "open_rounds": [r for r in rounds if r.status in OPEN_ROUND_STATES],
            "closed_rounds": [r for r in rounds if r.status not in OPEN_ROUND_STATES][:100],
            "form": invite_form,
            "can_invite": policy.can(request.user, "vendor.onboarding_invite"),
            "key_ready": vendor_crypto.configured(),
        },
        status=status,
    )


@login_required
@require_POST
def invite(request):
    policy.require(request.user, "vendor.onboarding_invite")
    form = InviteForm(request.POST)
    if form.is_valid():
        try:
            vendor = vo.invite_vendor(actor=request.user, request=request, **form.cleaned_data)
        except vo.OnboardingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f"Onboarding link sent to {vendor.email}.")
            rnd = vendor.onboarding_rounds.first()
            return redirect("vendor_onboarding_review", pk=rnd.pk)
    return _render_list(request, form, status=400)


def _coverage_warnings(answers: dict) -> list[str]:
    from community.rules import rule

    out = []
    for key, rule_key, label in (
        ("coi_gl_amount_cents", "vendor.min_general_liability_cents", "General liability"),
        ("coi_auto_amount_cents", "vendor.min_auto_liability_cents", "Auto liability"),
    ):
        minimum = rule(rule_key)
        have = answers.get(key)
        if minimum and have and int(have) < minimum:
            from core.money import format_cents

            out.append(
                f"{label} limit {format_cents(int(have))} is below the board's minimum "
                f"{format_cents(minimum)}."
            )
    today = timezone.localdate()
    for key, label in (
        ("coi_gl_expires", "General liability"),
        ("coi_auto_expires", "Auto liability"),
        ("workers_comp_expires", "Workers' compensation"),
    ):
        d = vo._date(answers.get(key))
        if d and d < today:
            out.append(f"{label} certificate has expired ({d:%b %-d, %Y}).")
    return out


@login_required
@require_GET
def review(request, pk):
    _review_perm(request)
    rnd = get_object_or_404(OnboardingRound.objects.select_related("vendor"), pk=pk)
    return _render_review(request, rnd)


def _render_review(request, rnd, *, reason_form=None, status=200):
    submission = vo.latest_submission(rnd)
    answers = (submission.answers if submission and rnd.status != RoundStatus.DRAFT else None) or (
        rnd.answers or {}
    )
    profile = vo.profile_of(rnd.vendor)
    files = []
    snap_ids = {f["id"] for f in (submission.files if submission else [])}
    for f in rnd.files.order_by("-uploaded_at"):
        files.append(
            {
                "file": f,
                "label": vo.ITEMS[vo.FILE_KINDS[f.kind]].label
                if f.kind in vo.FILE_KINDS
                else f.kind,
                "scan": vo.latest_scan(f),
                "submitted": f.pk in snap_ids,
                "may_view": vo.may_view_file(request.user, f),
            }
        )
    from documents.services import scan_allows_download

    for row in files:
        row["downloadable"] = scan_allows_download(row["scan"])
    user = request.user
    conflicted = sorted(vo.conflicted_person_ids(rnd.vendor_id))
    from community.models import Person
    from core.scoping import system_access

    with system_access("vendor conflict people"):
        conflict_people = list(Person.objects.filter(pk__in=conflicted))
        all_people = list(Person.objects.order_by("display_name").only("pk", "display_name"))
    entity = answers.get("entity_type", "")
    vtype = profile.vendor_type if profile else VendorType.OTHER
    return render(
        request,
        "operations/onboarding/review.html",
        {
            "rnd": rnd,
            "vendor": rnd.vendor,
            "profile": profile,
            "answers": answers,
            "entity_label": dict(EntityType.choices).get(entity, ""),
            "eligible_1099": vo.eligible_for_1099_types(entity, vtype),
            "citation_1099": vo.CITATION_1099,
            "preparer_caveat": vo.PREPARER_CAVEAT,
            "required": [vo.ITEMS[k] for k in vo.round_items(rnd)],
            "submission": submission,
            "submissions": rnd.submissions.prefetch_related("decisions__decided_by").order_by(
                "-number"
            ),
            "approvals": len(vo.approvers(submission)) if submission else 0,
            "needed": vo.APPROVALS_NEEDED,
            "files": files,
            "links": rnd.links.order_by("-created_at"),
            "warnings": _coverage_warnings(answers),
            "missing": vo.missing(rnd)
            if rnd.status in (RoundStatus.DRAFT, RoundStatus.RETURNED)
            else [],
            "approval_refusal": vo.approval_refusal(rnd, user)
            if policy.can(user, "vendor.onboarding_approve")
            else None,
            "can_decide": policy.can(user, "vendor.onboarding_approve"),
            "can_invite": policy.can(user, "vendor.onboarding_invite"),
            "possible_people": vo.possible_conflict_people(answers.get("conflict_details", "")),
            "conflict_people": conflict_people,
            "all_people": all_people,
            "reason_form": reason_form or ReasonForm(),
            "key_ready": vendor_crypto.configured(),
            "submitted": rnd.status == RoundStatus.SUBMITTED,
            "editable": rnd.status in (RoundStatus.DRAFT, RoundStatus.RETURNED),
            "open": rnd.status in OPEN_ROUND_STATES,
        },
        status=status,
    )


@login_required
@require_POST
def action(request, pk, action):
    _review_perm(request)
    rnd = get_object_or_404(OnboardingRound.objects.select_related("vendor"), pk=pk)
    reason_form = ReasonForm(request.POST)
    try:
        if action == "approve":
            vo.approve(rnd, actor=request.user, note=request.POST.get("note", ""), request=request)
            rnd.refresh_from_db()
            messages.success(
                request,
                "Approved: the vendor is now active."
                if rnd.status == RoundStatus.APPROVED
                else "Your approval is recorded; a second board member must approve too.",
            )
        elif action in ("return", "decline"):
            if not reason_form.is_valid():
                raise vo.OnboardingError("Give a reason.")
            fn = vo.return_to_vendor if action == "return" else vo.decline
            fn(rnd, reason=reason_form.cleaned_data["reason"], actor=request.user, request=request)
            messages.success(
                request,
                "Returned: the vendor gets a new link with your reason."
                if action == "return"
                else "Declined. The vendor stays inactive.",
            )
        elif action == "resend":
            vo.resend_link(rnd, actor=request.user, request=request)
            messages.success(request, "A new link is on its way; the old one no longer works.")
        elif action == "cancel":
            vo.cancel_round(rnd, actor=request.user, request=request)
            messages.success(request, "Cancelled. The link no longer works.")
        elif action in ("conflict_add", "conflict_remove"):
            from community.models import Person
            from core.scoping import system_access

            with system_access("vendor conflict link"):
                person = Person.objects.filter(pk=request.POST.get("person") or 0).first()
            if person is None:
                raise vo.OnboardingError("Choose a person.")
            vo.link_conflict(
                rnd.vendor,
                person,
                note=request.POST.get("note", ""),
                removed=action == "conflict_remove",
                actor=request.user,
                request=request,
            )
            messages.success(request, "Conflict record saved.")
        else:
            raise Http404
    except vo.OnboardingError as exc:
        reason_form.add_error(None, str(exc))
        return _render_review(request, rnd, reason_form=reason_form, status=400)
    return redirect("vendor_onboarding_review", pk=rnd.pk)


def _stream(data: bytes, filename: str):
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


@login_required
@require_GET
def file(request, pk, file_id):
    _review_perm(request)
    rnd = get_object_or_404(OnboardingRound, pk=pk)
    vf = get_object_or_404(VendorFile, pk=file_id, round=rnd)
    if not vo.may_view_file(request.user, vf):
        raise PermissionDenied("vendor.w9_view")
    try:
        data = vo.open_file(vf, actor=request.user, request=request)
    except vo.OnboardingError as exc:
        messages.error(request, str(exc))
        return redirect("vendor_onboarding_review", pk=rnd.pk)
    return _stream(data, vf.original_filename)


@login_required
@require_GET
def vendor_page(request, pk):
    _review_perm(request)
    vendor = get_object_or_404(Vendor, pk=pk)
    return _render_vendor(request, vendor)


def _render_vendor(request, vendor, error="", status=200):
    profile = vo.profile_of(vendor)
    today = timezone.localdate()
    return render(
        request,
        "operations/onboarding/vendor.html",
        {
            "vendor": vendor,
            "profile": profile,
            "status_text": vo.status_text(vendor),
            "rounds": vendor.onboarding_rounds.order_by("-created_at"),
            "open_round": vendor.onboarding_rounds.filter(status__in=OPEN_ROUND_STATES).first(),
            "w9_on_file": vo.w9_on_file(vendor),
            "w9_received_on": vo.w9_received_on(vendor),
            "coi_expires_on": vo.coi_expires_on(vendor),
            "coi_current": vo.coi_current(vendor, today),
            "required": [vo.ITEMS[k] for k in vo.required_documents(vendor)],
            "eligible_1099": vo.eligible_for_1099(vendor),
            "grandfathered": profile is None,
            "missing": vo.missing_for_grandfathered(vendor) if profile is None else [],
            "update_items": [vo.ITEMS[k] for k in vo.UPDATE_ITEMS],
            "vendor_types": VendorType.choices,
            "history": vo.access_history(vendor)
            if policy.can(request.user, "vendor.w9_view")
            else [],
            "can_invite": policy.can(request.user, "vendor.onboarding_invite"),
            "key_ready": vendor_crypto.configured(),
            "error": error,
            "active": vo.is_active(vendor),
        },
        status=status,
    )


@login_required
@require_POST
def vendor_action(request, pk, action):
    policy.require(request.user, "vendor.onboarding_invite")
    vendor = get_object_or_404(Vendor, pk=pk)
    try:
        if action == "update":
            rnd = vo.request_update(
                vendor, items=request.POST.getlist("items"), actor=request.user, request=request
            )
        elif action == "complete":
            rnd = vo.complete_onboarding(
                vendor,
                vendor_type=request.POST.get("vendor_type", ""),
                actor=request.user,
                request=request,
            )
        else:
            raise Http404
    except vo.OnboardingError as exc:
        return _render_vendor(request, vendor, error=str(exc), status=400)
    messages.success(request, f"Link sent to {vendor.email}.")
    return redirect("vendor_onboarding_review", pk=rnd.pk)


@login_required
@require_GET
def vendor_file_summary(request, pk):
    _review_perm(request)
    vendor = get_object_or_404(Vendor, pk=pk)
    data = vo.vendor_file_pdf(vendor, actor=request.user, request=request)
    return _stream(data, f"vendor-file-{vendor.pk}.pdf")


class TermsForm(forms.Form):
    text = forms.CharField(label="Vendor terms", widget=forms.Textarea(attrs={"rows": 10}))
    citation = forms.CharField(label="Adopted by", max_length=200, required=False)


@login_required
@require_http_methods(["GET", "POST"])
def requirements(request):
    _review_perm(request)
    can_edit = policy.can(request.user, "vendor.onboarding_configure")
    version, text = vo.current_terms()
    terms_form = TermsForm(initial={"text": text})
    error = ""
    if request.method == "POST":
        policy.require(request.user, "vendor.onboarding_configure")
        try:
            if request.POST.get("form") == "terms":
                terms_form = TermsForm(request.POST)
                if not terms_form.is_valid():
                    raise vo.OnboardingError("Enter the terms.")
                vo.save_terms(actor=request.user, request=request, **terms_form.cleaned_data)
                messages.success(request, "Terms saved as a new version.")
            else:
                citation = request.POST.get("citation", "")
                changed = 0
                for row in vo.requirement_table():
                    for item in row["items"]:
                        want = f"req__{row['type']}__{item['key']}" in request.POST
                        if want != item["required"]:
                            vo.set_requirement(
                                vendor_type=row["type"],
                                item=item["key"],
                                required=want,
                                citation=citation,
                                actor=request.user,
                                request=request,
                            )
                            changed += 1
                messages.success(request, f"{changed} change{'s' if changed != 1 else ''} saved.")
            return redirect("vendor_requirements")
        except vo.OnboardingError as exc:
            error = str(exc)
    return render(
        request,
        "operations/onboarding/requirements.html",
        {
            "table": vo.requirement_table(),
            "items": list(vo.ITEMS.values()),
            "terms_form": terms_form,
            "terms_version": version,
            "can_edit": can_edit,
            "error": error,
            "w9_revision": vo.CURRENT_W9_REVISION,
            "citation_1099": vo.CITATION_1099,
            "preparer_caveat": vo.PREPARER_CAVEAT,
            "counsel_caveat": vo.COUNSEL_CAVEAT,
        },
        status=400 if error else 200,
    )


@login_required
@require_GET
def w9_retention(request):
    policy.require(request.user, "vendor.w9_dispose")
    return render(
        request,
        "operations/onboarding/retention.html",
        {"due": vo.retention_due(), "years": vo.retention_years()},
    )


@login_required
@require_POST
def w9_disposal(request, pk, step):
    policy.require(request.user, "vendor.w9_dispose")
    vf = get_object_or_404(VendorFile, pk=pk, kind="w9")
    try:
        if step == "propose":
            vo.propose_disposal(
                vf, reason=request.POST.get("reason", ""), actor=request.user, request=request
            )
            messages.success(request, "Disposal proposed; a different board member confirms it.")
        elif step == "confirm":
            vo.confirm_disposal(vf, actor=request.user, request=request)
            messages.success(request, "The W-9 was removed; the record of it stays.")
        else:
            raise Http404
    except vo.OnboardingError as exc:
        messages.error(request, str(exc))
    return redirect("vendor_w9_retention")
