"""System status -> Keys and connections (D93): sysadmins enter, test and
remove third-party service keys in the browser; the board approves Stripe
and Teller changes. Every page is checked in this order: signed in, the role
(403 before anything else), then a fresh passkey or authenticator check for
anything that saves, decides or shows a key's last four characters.

Values are write-only: secret inputs never render a value back, POST data is
marked sensitive for error reports, and nothing here logs or audits a value."""

from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from core import credential_checks, credentials, fresh_mfa, policy
from core.models import CredentialChange

CONFIRM_VALUE = "keys-confirmed"

# Settings that stay on the server, and why (shown on the page; D93).
SERVER_ONLY = (
    (
        "DJANGO_SECRET_KEY",
        "Django's secret key",
        "Signs sessions and seals secret ballots; the portal cannot start without it.",
        "Set on the server at install (make-env.sh). Never change it by hand: sessions end and "
        "sealed ballots need it.",
    ),
    (
        "CREDENTIALS_KEK",
        "Key-encryption key for this page",
        "Unlocks the keys saved here. It must never be in the database it protects.",
        "Created on the server automatically by start-portal.sh (System status -> Install, or "
        "Restart). Keep a copy in the HOA's password manager with DJANGO_SECRET_KEY.",
    ),
    (
        "AUDIT_HASH_KEY",
        "Audit hash key",
        "Hashes unknown sign-in addresses in the audit log.",
        "Set on the server at install (make-env.sh).",
    ),
    (
        "DATABASE",
        "Database passwords and roles",
        "The portal needs them to reach its database at all, before it could read this page.",
        "Set on the server at install (make-env.sh, docker/postgres/init-roles.sh).",
    ),
    (
        "S3_SECRET_ACCESS_KEY",
        "File store credentials",
        "Uploaded files are read with them from the first request.",
        "Set on the server at install; moving to Cloudflare R2 is move-files-to-r2.sh.",
    ),
    (
        "TUNNEL_TOKEN",
        "Cloudflare tunnel token",
        "Only the tunnel container has it; the portal never sees it (security review E3).",
        "Set on the server at install (make-env.sh asks for it).",
    ),
    (
        "BACKUP_S3_BUCKET",
        "Off-site backup bucket",
        "The nightly backup runs on the server, and restoring after losing the server must not "
        "depend on the database being there.",
        "Optional: sudo bash infra/poc/setup-offsite-backups.sh on the server (once).",
    ),
    (
        "BANK_FEED_TOKEN_KEY",
        "Bank access token key",
        "Encrypts the bank's access tokens; made on the server, nothing to type.",
        "Created on the server automatically by start-portal.sh.",
    ),
    (
        "PAYMENTS_LIVE_ALLOWED",
        "Live payments switch",
        "A brake on the server: live Stripe keys are refused unless the server allows them.",
        "Production only: PAYMENTS_LIVE_ALLOWED=true and DEPLOYMENT_STAGE=production in .env.",
    ),
    (
        "SMS_AUTH_TOKEN",
        "Text messages (Twilio)",
        "Not on this page yet.",
        "Optional: SMS_PROVIDER and the SMS_ settings in .env, then Restart.",
    ),
)


def _server_only_rows():
    rows = []
    for name, label, why, todo in SERVER_ONLY:
        if name == "DATABASE":
            is_set, visible = True, True
        elif name == "TUNNEL_TOKEN":
            is_set, visible = None, False
        elif name == "CREDENTIALS_KEK":
            is_set, visible = credentials.kek_configured(), True
        else:
            is_set, visible = bool(getattr(settings, name, "")), True
        rows.append(
            {
                "name": name,
                "label": label,
                "why": why,
                "todo": todo,
                "set": is_set,
                "visible": visible,
            }
        )
    return rows


def _can_view_change(user) -> bool:
    return policy.can(user, "system.manage_keys") or policy.can(user, "system.approve_keys")


@login_required
def keys_connections(request):
    """The overview: every service, Set / Not set, who and when, pending
    changes, and the server-only settings with what to do."""
    policy.require(request.user, "system.manage_keys")
    fresh = fresh_mfa.is_fresh(request)
    return render(
        request,
        "core/keys.html",
        {
            "services": [credentials.overview(s) for s in credentials.SERVICES],
            "server_only": _server_only_rows(),
            "kek": credentials.kek_configured(),
            "fresh": fresh,
            "reveal_url": fresh_mfa.prompt_url(request, reverse("keys_connections")),
            "has_passkey": fresh_mfa.has_passkey(request.user),
            "window_minutes": fresh_mfa.WINDOW_SECONDS // 60,
        },
    )


def _service_form(service: credentials.Service, data=None, files=None):
    fields: dict[str, forms.Field] = {}
    for f in service.fields:
        if f.kind == credentials.SECRET:
            fields[f.name] = forms.CharField(
                label=f.label,
                required=False,
                help_text=f.help or "Leave blank to keep the saved one.",
                max_length=f.max_length,
                strip=True,
                widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off"}),
            )
        elif f.kind == credentials.FILE:
            fields[f.name] = forms.FileField(label=f.label, required=False, help_text=f.help)
        elif f.kind == credentials.CHOICE:
            fields[f.name] = forms.ChoiceField(
                label=f.label,
                required=False,
                choices=[("", "(keep as it is)"), *f.choices],
                help_text=f.help,
            )
        elif f.kind in (credentials.GENERATED, credentials.LIST):
            continue
        else:
            fields[f.name] = forms.CharField(
                label=f.label, required=False, max_length=f.max_length, help_text=f.help
            )
    fields["confirm"] = forms.BooleanField(
        label="I am changing the association's connection to this service.", required=False
    )
    fields["save_anyway"] = forms.BooleanField(
        label="Save even though the test fails (recorded and the board is told)", required=False
    )
    if service.pinned:
        fields["pin_change"] = forms.BooleanField(
            label=f"Change the pinned {service.pinned.lower()} (two board members, "
            f"{credentials.PIN_CHANGE_WAIT_HOURS} hours)",
            required=False,
        )
    form_class = type("ServiceKeysForm", (forms.Form,), fields)
    return form_class(data, files)


@sensitive_variables("values")
def _typed_values(service, form) -> dict:
    values: dict[str, str] = {}
    for f in service.fields:
        if f.name not in form.cleaned_data:
            continue
        raw = form.cleaned_data[f.name]
        if f.kind == credentials.FILE:
            values[f.name] = credential_checks.pem_upload_b64(raw) if raw else ""
        else:
            values[f.name] = (raw or "").strip() if isinstance(raw, str) else raw
    return {k: v for k, v in values.items() if v not in (None, "")}


@sensitive_post_parameters()
@login_required
def keys_service(request, service):
    """One service: enter and test its keys, save, or remove them."""
    policy.require(request.user, "system.manage_keys")
    svc = credentials.BY_KEY.get(service)
    if svc is None:
        raise Http404
    return _keys_service(request, svc)


@fresh_mfa.required
@sensitive_variables("values")
def _keys_service(request, svc):
    result = None
    form = _service_form(svc)
    if request.method == "POST":
        action = request.POST.get("action", "")
        if request.POST.get("check") != CONFIRM_VALUE:
            raise PermissionDenied("keys form")
        if svc.generated:
            return _vendor_key_action(request, svc, action)
        if action == "clear":
            if request.POST.get("confirm") != "on":
                messages.error(request, "Tick the confirmation to remove the saved keys.")
            else:
                try:
                    credentials.clear(request.user, svc.key, request=request)
                except credentials.CredentialError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(
                        request,
                        f"{svc.label}: the keys saved here were removed; the "
                        "server's own settings apply again. The board was told.",
                    )
            return redirect("keys_service", service=svc.key)
        form = _service_form(svc, request.POST, request.FILES)
        if form.is_valid():
            try:
                values = _typed_values(svc, form)
            except credentials.CredentialError as exc:
                messages.error(request, str(exc))
                values = None
            if values is not None:
                result = _test_and_maybe_save(request, svc, form, values, action)
                if result == "saved":
                    return redirect("keys_connections")
        form = _service_form(svc, None) if result == "saved" else _rebound(svc, request)
    return render(
        request,
        "core/keys_service.html",
        {
            "svc": svc,
            "info": credentials.overview(svc),
            "form": form,
            "result": result if result != "saved" else None,
            "check_value": CONFIRM_VALUE,
            "kek": credentials.kek_configured(),
            "has_passkey": fresh_mfa.has_passkey(request.user),
            "approval_hours": credentials.APPROVAL_HOURS,
            "pin_wait": credentials.PIN_CHANGE_WAIT_HOURS,
        },
    )


def _rebound(svc, request):
    """The form again after a test: plain settings keep what was typed,
    secrets and files are never put back into the page."""
    data = request.POST.copy()
    for f in svc.fields:
        if f.secret:
            data.pop(f.name, None)
    data.pop("confirm", None)
    data.pop("save_anyway", None)
    form = _service_form(svc, data)
    form.is_valid()
    return form


@sensitive_variables("values")
def _test_and_maybe_save(request, svc, form, values, action):
    if not credentials.note_test(request.user):
        messages.error(request, "Too many tests in the last hour. Try again later.")
        return None
    result = credential_checks.run(svc.key, values)
    from audit.services import record_event

    record_event(
        "system.credential_tested",
        actor=request.user,
        request=request,
        outcome="success" if result.ok else "failure",
        metadata={"service": svc.key, "ok": result.ok, "code": result.code},
    )
    if action != "save":
        return result
    if not form.cleaned_data.get("confirm"):
        messages.error(request, "Tick the confirmation to save. Nothing was saved.")
        return result
    anyway = bool(form.cleaned_data.get("save_anyway"))
    if result.code == "format":
        messages.error(request, "Nothing was saved: " + result.words)
        return result
    try:
        change = credentials.save(
            request.user,
            svc.key,
            values,
            test=result,
            saved_anyway=anyway,
            pin_change=bool(form.cleaned_data.get("pin_change")),
            request=request,
        )
    except credentials.CredentialError as exc:
        messages.error(request, str(exc))
        return result
    if change.approvals_needed:
        messages.success(
            request,
            f"{svc.label}: saved and waiting for the board. It takes effect once "
            + (
                f"two board members approve and {change.wait_hours} hours have passed."
                if change.kind == CredentialChange.Kind.PIN_CHANGE
                else f"another board member approves it (within {credentials.APPROVAL_HOURS} "
                "hours)."
            ),
        )
    else:
        messages.success(
            request, f"{svc.label}: saved and in use now (no restart needed). The board was told."
        )
    return "saved"


def _vendor_key_action(request, svc, action):
    if request.POST.get("confirm") != "on":
        messages.error(request, "Tick the confirmation first. Nothing was changed.")
        return redirect("keys_service", service=svc.key)
    try:
        if action == "make":
            credentials.make_vendor_key(request.user, request=request)
            messages.success(request, "Vendor document key made and in use. The board was told.")
        elif action == "rotate":
            credentials.make_vendor_key(request.user, rotate=True, request=request)
            messages.success(
                request,
                "New vendor document key in use; the old one opens older files until the "
                "background worker has re-encrypted them (a few minutes).",
            )
        elif action == "clear":
            credentials.clear(request.user, svc.key, request=request)
            messages.success(request, "The key made here was removed; the server's applies.")
        else:
            raise Http404
    except credentials.CredentialError as exc:
        messages.error(request, str(exc))
    return redirect("keys_service", service=svc.key)


@sensitive_post_parameters()
@login_required
def keys_change(request, pk):
    """One change: what it is, its state, and the board's decision on a
    Stripe or Teller change (never by the person who made it)."""
    if not _can_view_change(request.user):
        raise PermissionDenied("system.approve_keys")
    change = get_object_or_404(CredentialChange, pk=pk)
    if request.method == "POST":
        return _decide(request, change)
    cs = credentials.change_state(change)
    svc = credentials.BY_KEY.get(change.service)
    fresh = fresh_mfa.is_fresh(request)
    from core import conflicts

    return render(
        request,
        "core/keys_change.html",
        {
            "change": change,
            "cs": cs,
            "svc": svc,
            "names": [
                {
                    "label": credentials.FIELDS[v.name].label
                    if v.name in credentials.FIELDS
                    else v.name,
                    "secret": v.name in credentials.FIELDS and credentials.FIELDS[v.name].secret,
                    "last4": v.last4 if fresh else "",
                    "shown": v.shown,
                    "cleared": v.cleared,
                }
                for v in change.values.all()
                if v.name not in credentials.IMPLIED_NAMES
            ],
            "fresh": fresh,
            "reveal_url": fresh_mfa.prompt_url(request, request.get_full_path()),
            "can_decide": policy.can(request.user, "system.approve_keys")
            and cs.state == credentials.PENDING
            and not conflicts.own_matter(request.user, change),
            "own": conflicts.own_matter(request.user, change),
            "can_cancel": cs.state == credentials.PENDING
            and change.requested_by_id == request.user.pk,
            "check_value": CONFIRM_VALUE,
            "pin": credentials.pin(change.service),
        },
    )


@fresh_mfa.required
def _decide(request, change):
    decision = request.POST.get("decision", "")
    if request.POST.get("check") != CONFIRM_VALUE:
        raise PermissionDenied("keys decision")
    if decision in ("approve", "reject"):
        policy.require(request.user, "system.approve_keys")
    try:
        credentials.decide(request.user, change, decision, request=request)
    except credentials.CredentialError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Recorded. The board was told.")
    return redirect("keys_change", pk=change.pk)
