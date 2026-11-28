"""Community setup wizard pages (decision D51).

One page per step (``community.setup.STEPS``), each saving through the same
service as the page that normally owns the setting, with the same checks,
audit events and confirmations. Board members only
(``community.manage_rules``); everyone else gets 403.
"""

from __future__ import annotations

import re

from django import forms as djforms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.http import require_POST

from community import forms, services, setup
from community.rule_input import (
    citation_example,
    example_value,
    is_demo_citation,
    to_stored,
    to_typed,
)
from community.rules import (
    BOOL,
    CENTS,
    TEXT,
    group_title,
    unit_suffix,
    validate_value,
    value_display,
)
from community.services import CommunityError
from core import features, policy

# --- Rule steps --------------------------------------------------------------------------


def _slug(key: str) -> str:
    return key.replace(".", "-")


def _bool_text(raw) -> str:
    return "Yes" if str(raw).strip().lower() in ("1", "true", "yes") else "No"


def _shown(spec, raw) -> str:
    """A stored value as people read it ("$75.00", "51%", "3%", "Yes")."""
    return value_display(spec, raw)


def _default_typed(spec) -> str:
    """The default as a greyed placeholder: as it would be typed for a
    percent (the % is shown after the box), as people read it otherwise."""
    from community.pa_law import guide

    raw = spec.default
    if raw is None:
        g = guide(spec.key)
        raw = g.fallback if g else None
    if raw in (None, ""):
        return ""
    if unit_suffix(spec):
        return to_typed(spec, raw)
    return _shown(spec, raw)


def _default_text(spec) -> str:
    """What applies when nothing is recorded: the default or the PA law
    fallback, as people read it, or ""."""
    from community.pa_law import guide

    if spec.default is not None:
        return _shown(spec, spec.default)
    g = guide(spec.key)
    if g and g.fallback is not None:
        return _shown(spec, g.fallback)
    return ""


class RuleStepForm(djforms.Form):
    """One value and one citation field per rule of a step. Values are typed
    as on Community settings (money in dollars) and checked by the same code
    (``rule_input.to_stored``, ``rules.validate_value``)."""

    def __init__(self, specs, rows, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.specs = specs
        self.rows = rows
        self.stored: dict[str, tuple[str, str]] = {}
        for spec in specs:
            row = rows.get(spec.key)
            slug = _slug(spec.key)
            default = _default_text(spec)
            if spec.kind == BOOL:
                field = djforms.ChoiceField(
                    required=False,
                    choices=[
                        ("", f"Not set (default: {default})" if default else "Not set"),
                        ("true", "Yes"),
                        ("false", "No"),
                    ],
                )
                recorded = row.value if row else ""
                initial = ("true" if _bool_text(recorded) == "Yes" else "false") if recorded else ""
            else:
                attrs = {"inputmode": "decimal" if spec.kind == CENTS else "numeric"}
                if spec.kind == TEXT:
                    attrs = {}
                # Greyed: the default that applies when left empty, else an
                # example of what goes there.
                placeholder = _default_typed(spec) or example_value(spec)
                if placeholder:
                    attrs["placeholder"] = placeholder
                field = djforms.CharField(
                    required=False, max_length=200, widget=djforms.TextInput(attrs=attrs)
                )
                initial = to_typed(spec, row.value if row else "")
            field.label = spec.shown_label + (" (dollars)" if spec.kind == CENTS else "")
            if unit_suffix(spec) and "%" not in field.label:
                field.label += " (%)"
            field.initial = initial
            self.fields[f"{slug}__value"] = field
            citation = row.citation if row else ""
            demo = is_demo_citation(citation)
            self.fields[f"{slug}__citation"] = djforms.CharField(
                required=False,
                max_length=200,
                label=f"Citation for: {spec.shown_label}",
                # The demo data's "DEMO - replace" is not a citation to edit:
                # the box shows an example instead (and keeps it if left empty).
                initial="" if demo else citation,
                help_text=(
                    "The demo data has no real citation here: enter where this rule is written."
                    if demo
                    else ""
                ),
                widget=djforms.TextInput(attrs={"placeholder": citation_example(spec)}),
            )

    def clean(self):
        cleaned = super().clean()
        for spec in self.specs:
            slug = _slug(spec.key)
            typed = cleaned.get(f"{slug}__value") or ""
            try:
                value = validate_value(spec.key, to_stored(spec, typed))
            except ValueError as exc:
                self.add_error(f"{slug}__value", str(exc))
                continue
            citation = (cleaned.get(f"{slug}__citation") or "").strip()
            row = self.rows.get(spec.key)
            if not citation and row and is_demo_citation(row.citation):
                citation = row.citation  # left empty: the demo citation stays as it was
            self.stored[spec.key] = (value, citation)
        return cleaned

    def changed_keys(self) -> list[str]:
        """Rules whose value or citation differs from what is recorded: only
        those are saved (and audited)."""
        out = []
        for key, (value, citation) in self.stored.items():
            row = self.rows.get(key)
            if (value, citation[:200]) != (
                (row.value if row else ""),
                (row.citation if row else ""),
            ):
                out.append(key)
        return out


def _rule_rows():
    from community.models import GoverningRule

    return {r.key: r for r in GoverningRule.objects.all()}


def _since():
    """When the last finished run ended: values changed after it are marked."""
    record = setup.last_finished()
    return record.at if record else None


def _rule_items(form, since):
    """Template rows for a rule step, grouped under their group headings."""
    from community import pa_law
    from community.rules import rule_origin, rules_needing_attention

    needing = {s.key for s in rules_needing_attention()}
    groups: dict[str, dict] = {}
    for spec in form.specs:
        title = group_title(spec.group)
        group = groups.setdefault(
            title, {"title": title, "note": pa_law.GROUP_NOTES.get(spec.group, ""), "rules": []}
        )
        row = form.rows.get(spec.key)
        slug = _slug(spec.key)
        group["rules"].append(
            {
                "spec": spec,
                "value": form[f"{slug}__value"],
                "citation": form[f"{slug}__citation"],
                "help": spec.shown_help,
                "guide": pa_law.guide(spec.key),
                "origin": rule_origin(spec.key),
                "default": _default_text(spec),
                "suffix": unit_suffix(spec),
                "needs_attention": spec.key in needing,
                "changed": bool(since and row and row.updated_at > since),
            }
        )
    return list(groups.values())


# --- Feature switches --------------------------------------------------------------------


def _feature_keys(step) -> list[str]:
    if step.kind == setup.FEATURES:
        return [f.key for f in features.FEATURES]
    return [k for k in step.features if k in features.BY_KEY]


def _feature_rows(request, keys, since, data=None):
    from core.models import FeatureSetting

    states = features.states(request)
    stored = {f.key: f for f in FeatureSetting.objects.filter(key__in=keys)}
    rows = []
    for key in keys:
        f = features.BY_KEY[key]
        on = states.get(key, f.default)
        if data is not None:
            on = data.get(f"feature__{key}") == "on"
        row = stored.get(key)
        rows.append(
            {
                "feature": f,
                "on": on,
                "is_default": row is None,
                "changed": bool(since and row and row.updated_at > since),
            }
        )
    return rows


def _feature_changes(request, keys) -> dict[str, bool]:
    states = features.states(request)
    out = {}
    for key in keys:
        want = request.POST.get(f"feature__{key}") == "on"
        if want != states.get(key, features.BY_KEY[key].default):
            out[key] = want
    return out


def _switch_off_warnings(changes) -> list[str]:
    """The Features page's own warning for each feature being switched off."""
    from core.views import _feature_open_items, _switch_off_warning

    off = [k for k, on in changes.items() if not on]
    if not off:
        return []
    open_items = _feature_open_items()
    return [_switch_off_warning(features.BY_KEY[k], open_items.get(k)) for k in off]


# --- Status steps ------------------------------------------------------------------------


def _branding_status():
    from community.branding import current_backgrounds

    return current_backgrounds()


def _people_status(user):
    from accounts.models import Invitation
    from community.models import BoardSeat, Lot, OfficeHolding, Ownership, Role, RoleAssignment

    seats = list(BoardSeat.objects.for_user(user).active().select_related("person"))
    offices = list(OfficeHolding.objects.for_user(user).active().select_related("person"))
    lots = list(Lot.objects.for_user(user))
    owned = set(Ownership.objects.for_user(user).active().values_list("lot_id", flat=True))
    roles = RoleAssignment.objects.for_user(user).active()
    return {
        "seats": seats,
        "offices": offices,
        "thin": policy.thinly_covered_actions(),
        "sysadmins": roles.filter(role=Role.SYSADMIN).count(),
        "managers": roles.filter(role=Role.MANAGER).count(),
        "pending_invitations": Invitation.objects.for_user(user).pending().count(),
        "homes": len(lots),
        "homes_without_owner": sum(1 for lot in lots if lot.pk not in owned),
        "homes_without_address": sum(1 for lot in lots if not lot.has_address),
    }


def _server_status():
    """Where the server-side set-up stands: yes/no facts and the command a
    sysadmin runs. Never a key, and never a field to type one into (D36)."""
    from django.conf import settings

    from audit.models import AuditEvent
    from core import health, host_tasks, offsite
    from core.models import HostTask
    from ledger.bankfeeds import teller
    from payments.providers import config

    try:
        email_ok, email_detail = health._email()
    except Exception as exc:  # shown like a failed check
        email_ok, email_detail = False, f"Check failed ({type(exc).__name__})"
    test = AuditEvent.objects.filter(action="system.test_email_sent").order_by("-occurred_at")
    test = test.only("occurred_at", "metadata").first()
    stripe = config()
    last_health = HostTask.objects.filter(kind=HostTask.Kind.HEALTH_CHECK).first()
    try:
        from comms import push

        push_ready = push.configured()
    except Exception:
        push_ready = False
    try:
        from comms import usps_tracking

        usps = usps_tracking.setup_status()
    except Exception as exc:  # shown like a failed check
        usps = {"state": "failing", "text": f"Check failed ({type(exc).__name__})"}
    try:
        from comms import bounces

        bounce = bounces.setup_status()
    except Exception as exc:  # shown like a failed check
        bounce = {"state": "failing", "text": f"Check failed ({type(exc).__name__})"}
    try:
        from comms import delivery

        relay = delivery.setup_status()
    except Exception as exc:  # shown like a failed check
        relay = {"state": "failing", "provider": "", "text": f"Check failed ({type(exc).__name__})"}
    return {
        "relay": relay,
        "email_ok": email_ok,
        "email_detail": email_detail,
        "email_redirect": bool(settings.EMAIL_REDIRECT_ALL_TO),
        "test_email": test,
        "test_email_ok": bool(test and test.metadata.get("ok")),
        "backups": host_tasks.backups_overview(),
        "offsite": offsite.configured(),
        "last_health": last_health,
        "stripe": stripe,
        "teller": teller.configured(),
        "push": push_ready,
        "usps": usps,
        "bounce": bounce,
        "vendor_key": _vendor_key_ready(),
        "sms": getattr(settings, "SMS_PROVIDER", "none") != "none",
        "file_store_r2": _file_store_is_r2(),
    }


def _key_services(step_key: str) -> list[dict]:
    """Keys and connections (D93) for this step: configured or not, and
    whether saved in the portal. Never a value, never a field to type one."""
    from core import credentials

    out = []
    for key in setup.KEY_SERVICES[step_key]:
        try:
            info = credentials.status(key)
        except Exception as exc:  # shown like a failed check
            info = {
                "label": credentials.BY_KEY[key].label,
                "configured": False,
                "in_portal": False,
                "pending": False,
                "unreadable": False,
                "error": type(exc).__name__,
            }
        info["key"] = key
        info["script"] = credentials.BY_KEY[key].script
        out.append(info)
    return out


def _vendor_key_ready() -> bool:
    """Whether the vendor document key is set (D79). Never the key."""
    from operations import vendor_crypto

    return vendor_crypto.configured()


def _file_store_is_r2() -> bool:
    """Whether uploads go to Cloudflare R2 (else the local SeaweedFS store).
    Only the endpoint's host name is read, never a key."""
    from urllib.parse import urlsplit

    from django.conf import settings

    options = settings.STORAGES.get("default", {}).get("OPTIONS", {})
    host = urlsplit(options.get("endpoint_url") or "").hostname or ""
    return host.endswith(".r2.cloudflarestorage.com")


def _links(request, step):
    groups = policy.roles_for(request.user).groups
    from core.navigation import allowed

    out = []
    for label, name, permission in step.links:
        if not allowed(groups, permission) or not features.url_name_enabled(name, request):
            continue
        try:
            out.append({"label": label, "url": reverse(name)})
        except NoReverseMatch:
            continue
    return out


# --- Pages -------------------------------------------------------------------------------


def _require(request):
    policy.require(request.user, setup.PERMISSION)


def _step_list(states):
    return [
        {
            "step": s,
            "number": setup.number(s.key),
            "state": states.get(s.key, ""),
            "url": reverse("setup_step", args=[s.key]),
        }
        for s in setup.STEPS
    ]


def setup_front() -> dict:
    """Where setup stands and every step: the front of Settings for the
    board (core.hub_views.settings_hub, WT-004)."""
    run = setup.current_run()
    done = setup.finished(run)
    states = setup.step_states(run) if run and not done else {}
    resume = next((k for k in setup.WORK_STEPS if states.get(k) != "completed"), "review")
    return {
        "run": run,
        "finished": done,
        "steps": _step_list(states),
        "left": setup.steps_left(run) if run and not done else len(setup.WORK_STEPS),
        "resume_url": reverse("setup_step", args=[resume]),
        "last": setup.last_finished(),
        "total": len(setup.STEPS),
    }


@login_required
def setup_home(request):
    """Community setup is the front of Settings (WT-004): the old address
    (My items, bookmarks, the steps' Back) opens it there."""
    _require(request)
    return redirect("settings_hub")


@login_required
@require_POST
def setup_start(request):
    """Start setup, or run it again after a finished run. An open run is
    continued, not replaced."""
    _require(request)
    run = setup.current_run()
    if run is None or setup.finished(run):
        run = setup.start_run(actor=request.user, request=request)
        messages.success(request, f"Setup run {run} started. Every step shows today's values.")
    return redirect("setup_step", step=setup.KEYS[0])


_SECTION = re.compile(r"§\s*(\d{4}(?:\([a-z0-9]+\))*)")


def _same_act_entry(pa: list[dict], citation: str) -> dict | None:
    """The PA Act entry whose text already cites every section of a
    68 Pa.C.S. citation (WT-013), or None."""
    if "68 Pa.C.S." not in citation:
        return None
    wanted = set(_SECTION.findall(citation))
    if not wanted:
        return None
    for entry in pa:
        if wanted <= set(_SECTION.findall(entry["text"])):
            return entry
    return None


def law_panel(step) -> dict:
    """The step's "What the law says" panel: the Pennsylvania Act's notes for
    the rule groups it shows (community/pa_law.py) and the federal and other
    PA notes for the step (community/us_law.py), or its "no specific law"
    note. Data only; each rule's own PA guidance is shown beside it."""
    from community import pa_law, us_law

    groups = list(step.groups) + [
        s.group for s in setup.rules_for(step.key) if s.group not in step.groups
    ]
    pa = []
    seen_text = set()
    for group in dict.fromkeys(groups):
        text = pa_law.GROUP_NOTES.get(group)
        if text and text not in seen_text:
            seen_text.add(text)
            pa.append({"title": group_title(group), "text": text})
    notes = []
    seen_citations = set()
    for note in us_law.for_step(step.key):
        if note.citation in seen_citations:
            continue  # the same law twice in one step (WT-013)
        seen_citations.add(note.citation)
        same = _same_act_entry(pa, note.citation)
        if same is not None and "citation" not in same:
            # The Act's note already says this, citing the same sections:
            # one block, keeping the citation, "For this choice" and caveat.
            same.update(citation=note.citation, meaning=note.meaning, caveat=note.caveat)
            continue
        notes.append(note)
    return {
        "pa": pa,
        "notes": notes,
        "none": us_law.NO_SPECIFIC_LAW.get(step.key, ""),
        "footer": us_law.FOOTER,
    }


def _context(request, step, run, states, **extra):
    prev, _next = setup.neighbours(step.key)
    from community.terms import term_text

    return {
        "law": law_panel(step),
        "step": step,
        "title": term_text(step.title),
        "intro": term_text(step.intro),
        "number": setup.number(step.key),
        "total": len(setup.STEPS),
        "percent": round(setup.number(step.key) * 100 / len(setup.STEPS)),
        "prev_url": (
            reverse("setup_step", args=[prev]) if prev else reverse("settings_hub") + "#setup"
        ),
        "run": run,
        "state": states.get(step.key, ""),
        "links": _links(request, step),
        **extra,
    }


def _go_next(step_key):
    _prev, nxt = setup.neighbours(step_key)
    return redirect("setup_step", step=nxt or "review")


@login_required
def setup_step(request, step):
    _require(request)
    if step not in setup.BY_KEY:
        raise Http404
    s = setup.BY_KEY[step]
    run = setup.current_run()
    states = setup.step_states(run) if run and not setup.finished(run) else {}
    if s.kind == setup.REVIEW:
        return _review(request, s, run, states)
    if request.method == "POST" and request.POST.get("action") == "skip":
        setup.record_step(step, skipped=True, actor=request.user, request=request)
        messages.info(request, f"Skipped for now: {s.title}. It stays on the list.")
        return _go_next(step)
    since = _since()
    data = request.POST if request.method == "POST" else None

    # Each kind of step: build the form(s), and on POST save through the
    # owning service. A response means "show this" (errors, a confirmation).
    profile_form = rule_form = area_form = None
    if s.kind == setup.PROFILE:
        profile_form = _profile_form(data)
    specs = setup.rules_for(step) if s.kind == setup.RULES else []
    if specs:
        rule_form = RuleStepForm(specs, _rule_rows(), data)
    if step == "common_areas":
        area_form = _area_form(data)
        if request.method == "POST":
            response = _area_in_use(request, s)
            if response is not None:
                return response
    feature_keys = _feature_keys(s)

    if request.method == "POST":
        response = _save(
            request, s, profile_form, rule_form, area_form, feature_keys, run, states, since
        )
        if response is not None:
            return response
        run = setup.record_step(step, actor=request.user, request=request)
        messages.success(request, f"Saved: {s.title}.")
        return _go_next(step)

    return _render_step(request, s, run, states, since, profile_form, rule_form, area_form)


AREA_PLACEHOLDERS = {
    "name": "e.g. Pond and walking path",
    "notes": "e.g. Mowed by the landscaper April to October; the gate key is with the manager.",
}


def _area_form(data):
    """The optional "Add a common area" fields. No browser "required": the
    step saves with no area added (and with no areas at all)."""
    form = forms.CommonAreaForm(data, prefix="area", use_required_attribute=False)
    for name, text in AREA_PLACEHOLDERS.items():
        form.fields[name].widget.attrs["placeholder"] = text
    form.fields["name"].required = False  # checked only when an area is being added
    return form


def _area_in_use(request, s):
    """The Remove / Put back buttons beside each area: the area is taken out
    of use or put back (never deleted: its bookings and history stay), then
    the step shows again."""
    from community.models import CommonArea

    for field, in_use in (("area_remove", False), ("area_restore", True)):
        pk = (request.POST.get(field) or "").strip()
        if not pk:
            continue
        if not pk.isdigit():
            raise Http404
        area = CommonArea.objects.filter(pk=int(pk)).first()
        if area is None:
            raise Http404
        try:
            services.set_common_area_in_use(
                area=area, in_use=in_use, actor=request.user, request=request
            )
        except CommunityError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"{area.name} put back in use."
                if in_use
                else f"{area.name} removed: no longer offered or bookable. Its history is kept.",
            )
        return redirect("setup_step", step=s.key)
    return None


def _render_step(
    request, s, run, states, since, profile_form, rule_form, area_form, data=None, status=200
):
    from community.models import CommonArea, CommunityProfile

    extra = {
        "profile_form": profile_form,
        "rule_form": rule_form,
        "rule_groups": _rule_items(rule_form, since) if rule_form is not None else [],
        "area_form": area_form,
        "feature_rows": _feature_rows(request, _feature_keys(s), since, data),
        "since": since,
    }
    if s.kind == setup.PROFILE:
        profile = CommunityProfile.get()
        extra["profile_changed"] = bool(since and profile.updated_at > since)
    if s.key == "common_areas":
        extra["areas"] = CommonArea.objects.all()
    if s.kind == setup.BRANDING:
        extra["backgrounds"] = _branding_status()
    if s.kind == setup.PEOPLE:
        extra["people"] = _people_status(request.user)
    if s.kind == setup.SERVER or s.key == "payments":
        extra["server"] = _server_status()
    if s.key == "notices":
        extra["sms_ready"] = _server_status()["sms"]
    if s.key in setup.KEY_SERVICES:
        extra["key_services"] = _key_services(s.key)
    return render(
        request,
        "community/setup/step.html",
        _context(request, s, run, states, **extra),
        status=status,
    )


# --- Saving ------------------------------------------------------------------------------

LETTER_FIELDS = services.LETTER_FIELDS


class CommunityDetailsForm(forms.ProfileForm):
    """The community profile plus the letter lines (Letters and certificates)."""

    letterhead = djforms.CharField(
        required=False,
        max_length=3000,
        widget=djforms.Textarea(attrs={"rows": 4}),
        label="Letterhead",
        help_text="Lines at the top of letters and statements; the first line is the heading, "
        "e.g. the association's name, c/o the manager, address, email.",
    )
    remit_to = djforms.CharField(
        required=False,
        max_length=3000,
        widget=djforms.Textarea(attrs={"rows": 3}),
        label="Where owners mail checks",
        help_text="The mailing address printed on statements and the payment slip.",
    )
    letter_contact = djforms.CharField(
        required=False,
        max_length=200,
        label="Contact line used in letters",
        help_text="How owners reach accounting, e.g. the management office's email and phone.",
    )
    letter_signature = djforms.CharField(
        required=False,
        max_length=3000,
        widget=djforms.Textarea(attrs={"rows": 3}),
        label="Signature block",
        help_text="e.g. the association, by its managing agent.",
    )

    # A greyed example in every box, so each says what goes there (fictional).
    PLACEHOLDERS = {
        "legal_name": "e.g. Maple Hollow Homeowners Association",
        "fiscal_year_start_month": "e.g. 1 (January)",
        "signin_help_contact": "e.g. board@example.org or (555) 010-0142",
        "welcome_message": "e.g. Welcome to the portal. The annual meeting is on May 12.",
        "arc_form_return_to": "e.g. Maple Hollow HOA, c/o Example Management, PO Box 100, "
        "Anytown PA 17000",
        "arc_form_conditions": "e.g. Work starts only after written approval and follows the "
        "approved plans.",
        "letterhead": "e.g. Maple Hollow Homeowners Association\nc/o Example Management\n"
        "PO Box 100, Anytown PA 17000",
        "remit_to": "e.g. Maple Hollow HOA\nPO Box 100\nAnytown PA 17000",
        "letter_contact": "e.g. accounts@example.org, (555) 010-0100",
        "letter_signature": "e.g. Maple Hollow Homeowners Association\nby Example Management, "
        "its managing agent",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, text in self.PLACEHOLDERS.items():
            if name in self.fields:
                self.fields[name].widget.attrs["placeholder"] = text


PROFILE_FIELDS = (
    "legal_name",
    "fiscal_year_start_month",
    "welcome_message",
    "arc_form_return_to",
    "arc_form_conditions",
    "signin_help_contact",
)


def _profile_form(data):
    from community.models import CommunityProfile

    if data is not None:
        return CommunityDetailsForm(data)
    profile = CommunityProfile.get()
    return CommunityDetailsForm(
        initial={f: getattr(profile, f) for f in PROFILE_FIELDS + LETTER_FIELDS}
    )


def _save(request, s, profile_form, rule_form, area_form, feature_keys, run, states, since):
    from community.models import CommunityProfile
    from community.views import _big_dues_change

    def again(status=200):
        return _render_step(
            request,
            s,
            run,
            states,
            since,
            profile_form,
            rule_form,
            area_form,
            data=request.POST,
            status=status,
        )

    valid = True
    for form in (profile_form, rule_form):
        if form is not None and not form.is_valid():
            valid = False
    adding_area = area_form is not None and (request.POST.get("area-name") or "").strip()
    if area_form is not None and not adding_area:
        # Adding an area is optional: an empty one is not an error.
        area_form = _area_form(None)
    if adding_area and not area_form.is_valid():
        valid = False
    if not valid:
        return again()

    changes = _feature_changes(request, feature_keys)
    changed_rules = rule_form.changed_keys() if rule_form is not None else []

    # Two-step confirmations, as on Community settings and Features.
    if request.POST.get("confirm") != "yes":
        dues = None
        if "dues.monthly_cents" in changed_rules:
            value, citation = rule_form.stored["dues.monthly_cents"]
            dues = _big_dues_change({"key": "dues.monthly_cents", "value": value})
        warnings = _switch_off_warnings(changes)
        if dues or warnings:
            return render(
                request,
                "community/setup/confirm.html",
                {
                    "step": s,
                    "dues": dues,
                    "warnings": warnings,
                    "post": [
                        (k, v)
                        for k in request.POST
                        if k not in ("csrfmiddlewaretoken", "confirm")
                        for v in request.POST.getlist(k)
                    ],
                    "back_url": reverse("setup_step", args=[s.key]),
                },
            )

    try:
        with transaction.atomic():
            if profile_form is not None:
                cleaned = profile_form.cleaned_data
                profile = CommunityProfile.get()
                if any(getattr(profile, f) != cleaned[f] for f in PROFILE_FIELDS):
                    services.update_profile(
                        actor=request.user,
                        request=request,
                        **{f: cleaned[f] for f in PROFILE_FIELDS},
                    )
                letters = {f: cleaned[f] for f in LETTER_FIELDS}
                if any((getattr(profile, f) or "") != (letters[f] or "").strip() for f in letters):
                    services.update_letter_settings(actor=request.user, request=request, **letters)
            for key in changed_rules:
                value, citation = rule_form.stored[key]
                row = rule_form.rows.get(key)
                services.set_rule(
                    key=key,
                    value=value,
                    citation=citation,
                    notes=row.notes if row else "",
                    actor=request.user,
                    request=request,
                )
            if adding_area:
                services.save_common_area(
                    actor=request.user, request=request, **area_form.cleaned_data
                )
            for key, on in changes.items():
                features.set_feature(key, on, actor=request.user, request=request)
    except CommunityError as exc:
        form = profile_form or rule_form or area_form
        if form is not None:
            form.add_error(None, str(exc))
        else:
            messages.error(request, str(exc))
        return again()
    return None


# --- Review ------------------------------------------------------------------------------


def _review_rows(request):
    """Every decision, step by step, with flags for defaults, missing values
    and missing citations."""
    from community.models import CommunityProfile, GoverningRule
    from community.rules import rule_origin, rules_needing_attention
    from community.terms import term_text

    rows = {r.key: r for r in GoverningRule.objects.all()}
    needing = {s.key for s in rules_needing_attention()}
    assigned = setup.assign_rules()
    profile = CommunityProfile.get()
    states_ = features.states(request)
    from core.models import FeatureSetting

    stored_features = set(FeatureSetting.objects.values_list("key", flat=True))
    out = []
    for s in setup.STEPS:
        if s.kind == setup.REVIEW:
            continue
        lines = []
        if s.kind == setup.PROFILE:
            for field, label in (
                ("legal_name", "Legal name"),
                ("letterhead", "Letterhead"),
                ("remit_to", "Where owners mail checks"),
                ("letter_contact", "Contact line used in letters"),
                ("signin_help_contact", "Sign-in help contact"),
            ):
                value = (getattr(profile, field) or "").strip()
                lines.append(
                    {
                        "label": label,
                        "value": value.splitlines()[0] if value else "",
                        "flag": "" if value else "Missing",
                    }
                )
        for spec in assigned.get(s.key, []):
            row = rows.get(spec.key)
            origin = rule_origin(spec.key)
            raw = row.value if row and row.value != "" else None
            if origin == "recorded":
                flag = "No citation" if spec.key in needing else ""
                value = _shown(spec, raw)
            elif origin == "default":
                flag = "Default, no citation" if spec.key in needing else "Default"
                value = _shown(spec, spec.default)
            elif origin == "pa_law":
                flag = "PA law fallback"
                value = _default_text(spec)
            else:
                flag, value = "Not set", ""
            lines.append(
                {
                    "label": term_text(spec.label),
                    "value": value,
                    "citation": row.citation if row else "",
                    "flag": flag,
                }
            )
        for key in _feature_keys(s) if s.kind != setup.FEATURES else []:
            lines.append(_feature_line(key, states_, stored_features))
        if s.kind == setup.FEATURES:
            for f in features.FEATURES:
                lines.append(_feature_line(f.key, states_, stored_features))
        out.append({"step": s, "title": term_text(s.title), "lines": lines})
    return out


def _feature_line(key, states_, stored):
    f = features.BY_KEY[key]
    on = states_.get(key, f.default)
    return {
        "label": f"Feature: {f.label}",
        "value": "On" if on else "Off",
        "flag": "" if key in stored else "Default",
    }


def _review(request, s, run, states):
    if request.method == "POST":
        run = setup.finish_run(actor=request.user, request=request)
        messages.success(
            request,
            f"Community setup finished (run {run}). Run it again at any time from Settings.",
        )
        return redirect(reverse("settings_hub") + "#setup")
    sections = _review_rows(request)
    for section in sections:
        section["state"] = states.get(section["step"].key, "")
        section["url"] = reverse("setup_step", args=[section["step"].key])
        # D100 (WT-021): a step's lines fold away unless one is flagged or
        # the step was skipped, so the review is a short list of steps.
        section["flags"] = sum(1 for line in section["lines"] if line["flag"])
        section["open"] = bool(section["flags"]) or section["state"] == "skipped"
    flagged = sum(1 for sec in sections for line in sec["lines"] if line["flag"])
    return render(
        request,
        "community/setup/review.html",
        _context(
            request,
            s,
            run,
            states,
            sections=sections,
            flagged=flagged,
            finished=setup.finished(run),
            not_done=[sec for sec in sections if sec["state"] != "completed"],
        ),
    )
