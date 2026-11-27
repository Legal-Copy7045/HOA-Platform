"""Vendor onboarding (decision D79).

The board or manager invites a vendor (name, email, kind of work). The vendor
gets one emailed link to a public form, the only portal page besides the
sign-in steps that opens without signing in (D79 amends CLAUDE.md). The link
works until it expires (rule ``vendor.onboarding_link_days``) or the vendor
submits, whichever comes first; sending a new link ends the old one. The
vendor fills in who they are, how they are taxed, where checks go, their
insurance, licences and any conflict of interest, uploads PDFs (W-9,
certificates) and accepts the association's vendor terms. Two different
board members then approve before the vendor is active; either may return it
to the vendor with a reason (a new link) or decline it.

Functions other areas call (stable):

* ``w9_on_file(vendor)``, ``w9_received_on(vendor)``: a current W-9 is held.
* ``coi_current(vendor, on=None)``: the general liability certificate covers
  that day (renewal reminders).
* ``required_documents(vendor)``: the item keys this vendor must provide.
* ``is_active(vendor)``: may be paid (approved, or older than this feature).
* ``eligible_for_1099(vendor)``: True, False, or None when not yet known.
* ``invoice_block_reason(vendor)``: why its invoices cannot be approved or
  paid, or None.
* ``person_conflicted(user, vendor_id)``: the conflict hook (D65).

Never stored or logged: a taxpayer identification number (the portal never
asks for one as a field and refuses text that looks like one), bank account
numbers (vendors are paid by check to the remit address only).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from audit.actions import ActionSpec, register
from audit.services import record_event
from core import policy
from core.policy import SYSTEM, require
from operations import vendor_crypto
from operations.models import (
    OPEN_ROUND_STATES,
    VENDOR_EDITABLE_STATES,
    Decision,
    DisposalStep,
    EntityType,
    LinkEnd,
    OnboardingDecision,
    OnboardingLink,
    OnboardingRound,
    OnboardingSubmission,
    ProfileStatus,
    RoundMode,
    RoundStatus,
    Vendor,
    VendorConflict,
    VendorFile,
    VendorFileDisposal,
    VendorFileScan,
    VendorProfile,
    VendorRequirement,
    VendorTerms,
    VendorType,
)

FEATURE = "vendor_onboarding"
APPROVALS_NEEDED = 2
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES_PER_ROUND = 30
SESSION_SECONDS = 2 * 60 * 60

PREPARER_CAVEAT = (
    "Orientation only, not tax advice: the association's tax preparer should confirm which "
    "vendors get a Form 1099 and the current IRS thresholds and forms."
)
COUNSEL_CAVEAT = (
    "Orientation only, not legal advice: the association's counsel should confirm what the "
    "Declaration, the board's contracts policy and Pennsylvania law require of vendors."
)
# The IRS revision of Form W-9 vendors confirm they used. Check irs.gov when
# the IRS publishes a new one (the preparer should confirm).
CURRENT_W9_REVISION = "Rev. March 2024"


class OnboardingError(ValueError):
    """A refusal whose message is safe to show."""


# --- Audit actions (ids and counts only; never names, emails or numbers) ---------------

for _spec in (
    ActionSpec("vendor.invited", "Vendor invited to onboarding", frozenset({"round_id"})),
    ActionSpec(
        "vendor.onboarding_link_created",
        "Vendor onboarding link created",
        frozenset({"round_id", "link_id", "replaced"}),
    ),
    ActionSpec(
        "vendor.onboarding_link_sent", "Vendor onboarding link emailed", frozenset({"link_id"})
    ),
    ActionSpec(
        "vendor.onboarding_link_opened", "Vendor onboarding link opened", frozenset({"link_id"})
    ),
    ActionSpec(
        "vendor.onboarding_update_requested",
        "Vendor asked to update documents",
        frozenset({"round_id", "items"}),
    ),
    ActionSpec(
        "vendor.onboarding_file_uploaded",
        "Vendor uploaded a document",
        frozenset({"round_id", "file_id", "kind", "scan"}),
    ),
    ActionSpec(
        "vendor.onboarding_submitted",
        "Vendor submitted onboarding",
        frozenset({"round_id", "submission_id"}),
    ),
    ActionSpec(
        "vendor.onboarding_decided",
        "Vendor onboarding decision",
        frozenset({"submission_id", "decision", "have", "need"}),
    ),
    ActionSpec(
        "vendor.onboarding_activated", "Vendor approved and active", frozenset({"round_id"})
    ),
    ActionSpec(
        "vendor.onboarding_cancelled", "Vendor onboarding cancelled", frozenset({"round_id"})
    ),
    ActionSpec(
        "vendor.conflict_linked",
        "Person linked to a vendor's conflict disclosure",
        frozenset({"person_id", "removed"}),
    ),
    ActionSpec(
        "vendor.requirement_set",
        "Vendor document requirement set",
        frozenset({"vendor_type", "item", "required"}),
    ),
    ActionSpec("vendor.terms_saved", "Vendor terms saved", frozenset({"version"})),
    ActionSpec(
        "vendor.document_viewed",
        "Vendor document opened",
        frozenset({"file_id", "kind", "scan"}),
    ),
    ActionSpec("vendor.w9_viewed", "Vendor W-9 opened", frozenset({"file_id", "scan"})),
    ActionSpec("vendor.file_summary_downloaded", "Vendor file summary downloaded"),
    ActionSpec(
        "vendor.document_disposal",
        "Vendor document disposal step",
        frozenset({"file_id", "step"}),
    ),
    ActionSpec("vendor.files_reencrypted", "Vendor documents re-encrypted", frozenset({"count"})),
):
    register(_spec)


# --- What vendors provide: data with citations ------------------------------------------


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    help: str
    file_kind: str = ""  # the upload's kind ("" = no file)
    file_required: bool = True


ITEMS: dict[str, Item] = {
    i.key: i
    for i in (
        Item(
            "w9",
            "Form W-9 (Request for Taxpayer Identification Number)",
            "Upload your signed IRS Form W-9 as a PDF. Never type your tax ID number anywhere "
            "else on this form.",
            "w9",
        ),
        Item(
            "coi_gl",
            "Certificate of insurance: general liability",
            "A certificate (for example an ACORD 25) showing your general liability insurer, "
            "limit and expiry date.",
            "coi_gl",
        ),
        Item(
            "coi_auto",
            "Certificate of insurance: auto liability",
            "If you drive vehicles onto the community's property for the work.",
            "coi_auto",
        ),
        Item(
            "workers_comp",
            "Workers' compensation certificate or exemption",
            "A certificate of workers' compensation insurance, or, if Pennsylvania law exempts "
            "you (for example a sole proprietor with no employees), your signed exemption "
            "statement.",
            "workers_comp",
        ),
        Item(
            "hic_registration",
            "Pennsylvania Home Improvement Contractor registration",
            "Your registration number from the Pennsylvania Attorney General (PA followed by "
            "six digits). A copy of the certificate is optional.",
            "hic",
            file_required=False,
        ),
        Item(
            "licence",
            "Professional licence or registration",
            "Licence or registration numbers that apply to the work (for example a CPA, "
            "engineer or attorney licence). A copy is optional.",
            "licence",
            file_required=False,
        ),
    )
}
FILE_KINDS = {i.file_kind: i.key for i in ITEMS.values() if i.file_kind}
UPDATE_ITEMS = ("w9", "coi_gl", "coi_auto", "workers_comp", "hic_registration", "licence")

REQUIREMENT_CITATION = (
    "Association vendor policy default (board may change; cite the board resolution)"
)
# The default required set per kind of work: sensible for a Pennsylvania HOA.
# The board changes it on Vendor requirements; each change carries a citation.
DEFAULT_REQUIREMENTS: dict[str, frozenset[str]] = {
    VendorType.CONTRACTOR: frozenset({"w9", "coi_gl", "coi_auto", "workers_comp"}),
    VendorType.HOME_IMPROVEMENT: frozenset(
        {"w9", "coi_gl", "coi_auto", "workers_comp", "hic_registration"}
    ),
    VendorType.LANDSCAPING: frozenset({"w9", "coi_gl", "coi_auto", "workers_comp"}),
    VendorType.PROFESSIONAL: frozenset({"w9", "licence"}),
    VendorType.ATTORNEY: frozenset({"w9", "licence"}),
    VendorType.SUPPLIER: frozenset({"w9"}),
    VendorType.UTILITY: frozenset(),
    VendorType.OTHER: frozenset({"w9", "coi_gl"}),
}
DEFAULT_CITATIONS: dict[str, str] = {
    "w9": "IRS Form W-9 instructions; IRS Instructions for Forms 1099-MISC and 1099-NEC",
    "coi_gl": "Association vendor policy (insurance); Declaration insurance article",
    "coi_auto": "Association vendor policy (insurance)",
    "workers_comp": "Pennsylvania Workers' Compensation Act (77 P.S. § 1 et seq.)",
    "hic_registration": "Home Improvement Consumer Protection Act (73 P.S. § 517.1 et seq.)",
    "licence": "Pennsylvania professional licensing laws for the profession",
}

# Form 1099 eligibility by how the vendor is taxed (D79). Payments to
# corporations are generally not reported; attorneys' fees are reported
# whatever the entity; payments for goods only and to utilities or government
# are not. Data with its citation; the preparer decides.
CITATION_1099 = (
    "IRS Instructions for Forms 1099-MISC and 1099-NEC (payments to corporations; attorneys' "
    "fees; payments for merchandise)"
)
ENTITY_1099: dict[str, bool] = {
    EntityType.INDIVIDUAL: True,
    EntityType.SMLLC: True,
    EntityType.PARTNERSHIP: True,
    EntityType.LLC_P: True,
    EntityType.TRUST: True,
    EntityType.OTHER: True,
    EntityType.C_CORP: False,
    EntityType.S_CORP: False,
    EntityType.LLC_C: False,
    EntityType.LLC_S: False,
    EntityType.EXEMPT: False,
}
ALWAYS_1099_TYPES = frozenset({VendorType.ATTORNEY})
NEVER_1099_TYPES = frozenset({VendorType.SUPPLIER, VendorType.UTILITY})

DEFAULT_TERMS = (
    "1. The association pays approved invoices by check, mailed to the remit-to address you "
    "give here. It never asks for or keeps bank account numbers.\n"
    "2. Keep the insurance you certified in force while you work for the association, and send "
    "a new certificate before the current one expires.\n"
    "3. Tell the association at once if anything you disclosed about a conflict of interest "
    "changes.\n"
    "4. Work on the community's property follows the association's rules and the work order "
    "or contract the board approved; nothing here is a contract for any work."
)


def eligible_for_1099_types(entity_type: str, vendor_type: str) -> bool | None:
    if vendor_type in ALWAYS_1099_TYPES:
        return True
    if vendor_type in NEVER_1099_TYPES:
        return False
    if not entity_type:
        return None
    return ENTITY_1099.get(entity_type, True)


def _requirement_rows() -> dict[tuple[str, str], VendorRequirement]:
    rows: dict[tuple[str, str], VendorRequirement] = {}
    for row in VendorRequirement.objects.order_by("set_at", "id"):
        rows[(row.vendor_type, row.item)] = row
    return rows


def requirement_table() -> list[dict]:
    """Every kind of work with each item's requirement and citation."""
    rows = _requirement_rows()
    out = []
    for vtype, label in VendorType.choices:
        items = []
        for key, item in ITEMS.items():
            row = rows.get((vtype, key))
            required = row.required if row else key in DEFAULT_REQUIREMENTS.get(vtype, ())
            items.append(
                {
                    "key": key,
                    "label": item.label,
                    "required": required,
                    "citation": row.citation if row else DEFAULT_CITATIONS.get(key, ""),
                    "default": row is None,
                }
            )
        out.append({"type": vtype, "label": label, "items": items})
    return out


def required_items(vendor_type: str, entity_type: str = "") -> list[str]:
    rows = _requirement_rows()
    out = []
    for key in ITEMS:
        row = rows.get((vendor_type, key))
        required = row.required if row else key in DEFAULT_REQUIREMENTS.get(vendor_type, ())
        if key == "w9" and eligible_for_1099_types(entity_type, vendor_type):
            required = True  # a 1099 needs the W-9, whatever the table says
        if required:
            out.append(key)
    return out


# --- Stable functions for other areas ---------------------------------------------------


def profile_of(vendor) -> VendorProfile | None:
    if vendor is None:
        return None
    vendor_id = vendor if isinstance(vendor, int) else vendor.pk
    return VendorProfile.objects.filter(vendor_id=vendor_id).first()


def _vendor(vendor) -> Vendor:
    """Always the stored row: a caller's instance may be stale (an invoice's
    cached vendor from before the approval)."""
    return Vendor.objects.get(pk=vendor if isinstance(vendor, int) else vendor.pk)


def is_grandfathered(vendor) -> bool:
    """Created before vendor onboarding: no onboarding profile."""
    return profile_of(vendor) is None


def is_active(vendor) -> bool:
    """May have invoices approved and paid: an active vendor that two board
    members approved, or one that existed before this feature (D79)."""
    vendor = _vendor(vendor)
    if not vendor.active:
        return False
    profile = profile_of(vendor)
    return profile is None or profile.approved_at is not None


def invoice_block_reason(vendor) -> str | None:
    vendor = _vendor(vendor)
    if is_active(vendor):
        return None
    profile = profile_of(vendor)
    if profile is not None and profile.approved_at is None:
        return (
            f"{vendor.name} isn't an approved vendor yet: its onboarding needs two board "
            "members' approval before its invoices can be approved or paid."
        )
    return f"{vendor.name} is inactive, so its invoices can't be approved or paid."


def _disposed_file_ids() -> set[int]:
    return set(
        VendorFileDisposal.objects.filter(step=DisposalStep.CONFIRM).values_list(
            "file_id", flat=True
        )
    )


def w9_received_on(vendor) -> datetime.date | None:
    vendor = _vendor(vendor)
    profile = profile_of(vendor)
    if profile is None:
        return None
    if profile.w9_file_id and profile.w9_file_id in _disposed_file_ids():
        return None
    return profile.w9_received_on


def w9_on_file(vendor) -> bool:
    """A W-9 is held: the approved onboarding's, or for a vendor older than
    onboarding the board's own tick (Vendor.w9_on_file)."""
    vendor = _vendor(vendor)
    profile = profile_of(vendor)
    if profile is None or profile.w9_file_id is None:
        # Older than onboarding, or onboarded without a W-9 upload yet: the
        # board's own tick on the vendor record.
        if profile is not None and profile.w9_received_on:
            return True
        return bool(vendor.w9_on_file)
    return w9_received_on(vendor) is not None


def coi_expires_on(vendor) -> datetime.date | None:
    """The certificate of insurance expiry: the later of the approved
    onboarding's and the vendor record's (a certificate recorded on
    Renewals after onboarding moves ``insurance_expires_on`` only)."""
    vendor = _vendor(vendor)
    profile = profile_of(vendor)
    dates = [d for d in (profile and profile.coi_expires_on, vendor.insurance_expires_on) if d]
    return max(dates) if dates else None


def coi_current(vendor, on: datetime.date | None = None) -> bool:
    on = on or timezone.localdate()
    expires = coi_expires_on(vendor)
    return expires is not None and expires >= on


def required_documents(vendor) -> list[str]:
    """Item keys (``ITEMS``) this vendor must provide, from its kind of work
    and, once known, how it is taxed."""
    vendor = _vendor(vendor)
    profile = profile_of(vendor)
    if profile is None:
        return required_items(VendorType.OTHER)
    return required_items(profile.vendor_type, profile.entity_type)


def eligible_for_1099(vendor) -> bool | None:
    profile = profile_of(_vendor(vendor))
    if profile is None:
        return None
    return eligible_for_1099_types(profile.entity_type, profile.vendor_type)


def conflicted_person_ids(vendor_id: int) -> set[int]:
    latest: dict[int, bool] = {}
    for person_id, removed in VendorConflict.objects.filter(vendor_id=vendor_id).values_list(
        "person_id", "removed"
    ):
        latest[person_id] = removed
    return {p for p, removed in latest.items() if not removed}


def person_conflicted(user, vendor_id) -> bool:
    """D65 hook: ``user``'s Person is linked to the vendor's conflict disclosure."""
    if user is None or user is SYSTEM or not getattr(user, "is_authenticated", False):
        return False
    if not vendor_id:
        return False
    person_id = policy.roles_for(user).person_id
    return person_id is not None and person_id in conflicted_person_ids(vendor_id)


def missing_for_grandfathered(vendor) -> list[str]:
    """What an older vendor never gave the portal (the "complete onboarding" item)."""
    vendor = _vendor(vendor)
    missing = []
    if not vendor.w9_on_file:
        missing.append("W-9")
    if vendor.insurance_expires_on is None:
        missing.append("insurance certificate")
    if not vendor.email:
        missing.append("email address")
    return missing


# --- Tokens -----------------------------------------------------------------------------


def _hash_with(token: str, secret: str) -> str:
    key = hashlib.sha256(b"vendor-onboarding-link\x00" + secret.encode()).digest()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    """Keyed hash of a link token (only this is stored)."""
    return _hash_with(token, settings.SECRET_KEY)


def _candidate_hashes(token: str) -> list[str]:
    secrets_ = [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]
    return [_hash_with(token, s) for s in secrets_ if s]


def link_days() -> int:
    from community.rules import rule

    days = rule("vendor.onboarding_link_days") or 14
    return max(1, min(int(days), 60))


def find_open_link(token: str) -> OnboardingLink | None:
    """The link this token opens, if it may still be used. None otherwise
    (unknown, expired, replaced, used): callers answer every case the same."""
    if not token or len(token) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    link = (
        OnboardingLink.objects.select_related("round")
        .filter(token_hash__in=_candidate_hashes(token))
        .exclude(token_hash="")  # nosec B106 - not a password: unsent links
        .first()
    )
    if link is None or not link_usable(link):
        return None
    return link


def link_usable(link: OnboardingLink) -> bool:
    return (
        link.ended_at is None
        and link.expires_at > timezone.now()
        and link.round.status in VENDOR_EDITABLE_STATES
        and bool(link.token_hash)
    )


def record_open(link: OnboardingLink, request=None) -> None:
    OnboardingLink.objects.filter(pk=link.pk).update(
        opened_count=link.opened_count + 1, last_opened_at=timezone.now()
    )
    record_event(
        "vendor.onboarding_link_opened",
        request=request,
        target=link.round.vendor,
        metadata={"link_id": link.pk},
    )


# --- Board side: invite, links, updates -------------------------------------------------


def _feature_on() -> bool:
    from core.features import enabled

    return enabled(FEATURE)


def _require_key() -> None:
    if not vendor_crypto.configured():
        raise OnboardingError(vendor_crypto.MISSING_TEXT)


def invite_vendor(
    *, name: str, email: str, services: str, vendor_type: str, actor, request=None
) -> Vendor:
    require(actor, "vendor.onboarding_invite")
    _require_key()
    name, email = (name or "").strip(), (email or "").strip().lower()
    if not name or not email:
        raise OnboardingError("A vendor needs a name and an email address for the link.")
    if vendor_type not in VendorType.values:
        raise OnboardingError("Choose the kind of work.")
    with transaction.atomic():
        vendor = Vendor.objects.create(
            name=name[:200], email=email[:254], services=(services or "")[:200], active=False
        )
        VendorProfile.objects.create(
            vendor=vendor,
            status=ProfileStatus.INVITED,
            vendor_type=vendor_type,
            invited_by=None if actor is SYSTEM else actor,
        )
        rnd = OnboardingRound.objects.create(vendor=vendor, mode=RoundMode.FULL, created_by=actor)
        record_event(
            "vendor.invited",
            actor=_user(actor),
            request=request,
            target=vendor,
            metadata={"round_id": rnd.pk},
        )
        _new_link(rnd, actor=actor, request=request)
    return vendor


def _user(actor):
    return None if actor is SYSTEM else actor


def _new_link(rnd: OnboardingRound, *, actor, request=None) -> OnboardingLink:
    """End any open link of the round and create a new one; the worker mints
    the token and emails it after the transaction commits."""
    now = timezone.now()
    replaced = OnboardingLink.objects.filter(round=rnd, ended_at__isnull=True).update(
        ended_at=now, end_reason=LinkEnd.REPLACED
    )
    link = OnboardingLink.objects.create(
        round=rnd,
        created_by=actor if actor is not SYSTEM else rnd.created_by,
        created_at=now,
        expires_at=now + datetime.timedelta(days=link_days()),
    )
    record_event(
        "vendor.onboarding_link_created",
        actor=_user(actor),
        request=request,
        target=rnd.vendor,
        metadata={"round_id": rnd.pk, "link_id": link.pk, "replaced": replaced},
    )

    def queue():
        from operations.tasks import send_vendor_onboarding_link

        send_vendor_onboarding_link.defer(link_id=link.pk)

    transaction.on_commit(queue)
    return link


def resend_link(rnd: OnboardingRound, *, actor, request=None) -> OnboardingLink:
    require(actor, "vendor.onboarding_invite")
    _require_key()
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status not in VENDOR_EDITABLE_STATES:
            raise OnboardingError("Only a round waiting for the vendor can get a new link.")
        if not rnd.vendor.email:
            raise OnboardingError("Add the vendor's email address first.")
        return _new_link(rnd, actor=actor, request=request)


def request_update(vendor, *, items: list[str], actor, request=None) -> OnboardingRound:
    """Ask an onboarded (or older) vendor for new documents only: an expired
    certificate, a new W-9 (update mode: the form shows only these items)."""
    require(actor, "vendor.onboarding_invite")
    _require_key()
    items = [i for i in UPDATE_ITEMS if i in set(items or [])]
    if not items:
        raise OnboardingError("Choose what the vendor should update.")
    vendor = _vendor(vendor)
    if not vendor.email:
        raise OnboardingError("Add the vendor's email address first.")
    with transaction.atomic():
        Vendor.objects.select_for_update().filter(pk=vendor.pk).exists()
        if OnboardingRound.objects.filter(vendor=vendor, status__in=OPEN_ROUND_STATES).exists():
            raise OnboardingError(
                "This vendor already has an onboarding request open; finish or cancel it first."
            )
        profile = profile_of(vendor)
        if profile is None:
            # An older vendor: its profile starts active (grandfathered).
            profile = VendorProfile.objects.create(
                vendor=vendor,
                status=ProfileStatus.ACTIVE,
                vendor_type=VendorType.OTHER,
                approved_at=vendor.created_at,
                w9_received_on=None,
                coi_expires_on=vendor.insurance_expires_on,
                invited_by=_user(actor),
            )
        elif profile.approved_at is None:
            raise OnboardingError("This vendor hasn't finished onboarding yet.")
        rnd = OnboardingRound.objects.create(
            vendor=vendor, mode=RoundMode.UPDATE, items=items, created_by=actor
        )
        record_event(
            "vendor.onboarding_update_requested",
            actor=_user(actor),
            request=request,
            target=vendor,
            metadata={"round_id": rnd.pk, "items": ",".join(items)},
        )
        _new_link(rnd, actor=actor, request=request)
    return rnd


def complete_onboarding(vendor, *, vendor_type: str, actor, request=None) -> OnboardingRound:
    """An older vendor (grandfathered) is asked for the full onboarding. It
    stays active meanwhile; the approval fills in what was missing."""
    require(actor, "vendor.onboarding_invite")
    _require_key()
    vendor = _vendor(vendor)
    if vendor_type not in VendorType.values:
        raise OnboardingError("Choose the kind of work.")
    if not vendor.email:
        raise OnboardingError("Add the vendor's email address first.")
    with transaction.atomic():
        Vendor.objects.select_for_update().filter(pk=vendor.pk).exists()
        if OnboardingRound.objects.filter(vendor=vendor, status__in=OPEN_ROUND_STATES).exists():
            raise OnboardingError("This vendor already has an onboarding request open.")
        profile = profile_of(vendor)
        if profile is None:
            VendorProfile.objects.create(
                vendor=vendor,
                status=ProfileStatus.ACTIVE if vendor.active else ProfileStatus.INVITED,
                vendor_type=vendor_type,
                approved_at=vendor.created_at if vendor.active else None,
                coi_expires_on=vendor.insurance_expires_on,
                w9_received_on=None,
                invited_by=_user(actor),
            )
        else:
            VendorProfile.objects.filter(pk=profile.pk).update(vendor_type=vendor_type)
        rnd = OnboardingRound.objects.create(vendor=vendor, mode=RoundMode.FULL, created_by=actor)
        record_event(
            "vendor.invited",
            actor=_user(actor),
            request=request,
            target=vendor,
            metadata={"round_id": rnd.pk},
        )
        _new_link(rnd, actor=actor, request=request)
    return rnd


def cancel_round(rnd: OnboardingRound, *, actor, request=None) -> None:
    require(actor, "vendor.onboarding_invite")
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status not in OPEN_ROUND_STATES:
            raise OnboardingError("This request is already closed.")
        now = timezone.now()
        OnboardingRound.objects.filter(pk=rnd.pk).update(
            status=RoundStatus.CANCELLED, status_changed_at=now
        )
        OnboardingLink.objects.filter(round=rnd, ended_at__isnull=True).update(
            ended_at=now, end_reason=LinkEnd.CANCELLED
        )
        record_event(
            "vendor.onboarding_cancelled",
            actor=_user(actor),
            request=request,
            target=rnd.vendor,
            metadata={"round_id": rnd.pk},
        )


def link_url(token: str) -> str:
    return settings.PORTAL_BASE_URL + reverse("vendor_onboarding_start", args=[token])


def association_name() -> str:
    from community.models import CommunityProfile

    return CommunityProfile.get().legal_name


def send_link_email(link_id: int) -> bool:
    """Worker: mint the token (it never travels through the job queue or the
    database in clear), store its keyed hash and email the link."""
    from django.core.mail import send_mail

    token = secrets.token_urlsafe(32)  # 256 bits
    with transaction.atomic():
        link = (
            OnboardingLink.objects.select_for_update()
            .select_related("round__vendor")
            .filter(pk=link_id)
            .first()
        )
        if link is None or link.ended_at is not None or link.token_hash:
            return False
        if link.round.status not in VENDOR_EDITABLE_STATES or not link.round.vendor.email:
            return False
        OnboardingLink.objects.filter(pk=link.pk).update(
            token_hash=token_hash(token), sent_at=timezone.now()
        )
        record_event(
            "vendor.onboarding_link_sent", target=link.round.vendor, metadata={"link_id": link.pk}
        )
    name = association_name()
    expires = timezone.localtime(link.expires_at).strftime("%B %-d, %Y")
    update = link.round.mode == RoundMode.UPDATE
    body = (
        f"{name} asks you to {'update your documents on' if update else 'fill in'} its vendor "
        f"information form.\n\n"
        f"Open the form here:\n{link_url(token)}\n\n"
        f"The link works until {expires} or until you submit the form, whichever comes "
        f"first. You don't need an account. Please have your documents ready as PDF files "
        f"(for example your Form W-9 and your certificate of insurance).\n\n"
        f"Never send a tax ID number or bank details by email: upload your W-9 on the form. "
        f"The association pays vendors by check only.\n\n"
        f"If you weren't expecting this, you can ignore it."
    )
    send_mail(f"Vendor information form: {name}", body, None, [link.round.vendor.email])
    return True


# --- The vendor's side ------------------------------------------------------------------

_TIN_PATTERNS = (
    re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])"),
    re.compile(r"(?<![\d-])\d{2}-\d{7}(?![\d-])"),
    re.compile(r"(?<![\d-])\d{3}\s\d{2}\s\d{4}(?![\d])"),
    re.compile(r"(?<![\d-])\d{9}(?![\d-])"),
)
_ACCOUNT_NUMBER = re.compile(r"\d{10,}")


def looks_like_tin(text: str) -> bool:
    return any(p.search(text or "") for p in _TIN_PATTERNS)


def check_free_text(label: str, text: str, *, phone: bool = False) -> None:
    if looks_like_tin(text):
        raise OnboardingError(
            f"{label}: please don't type tax ID numbers on this form; they belong only on "
            "your W-9 upload."
        )
    if not phone and _ACCOUNT_NUMBER.search((text or "").replace(" ", "").replace("-", "")):
        raise OnboardingError(
            f"{label}: please don't type bank or account numbers; the association pays by check."
        )


def round_items(rnd: OnboardingRound, entity_type: str | None = None) -> list[str]:
    """The items this round asks for: an update's named items, else the
    vendor type's requirements (and the W-9 when 1099-eligible)."""
    if rnd.mode == RoundMode.UPDATE:
        return list(rnd.items)
    profile = profile_of(rnd.vendor)
    vtype = profile.vendor_type if profile else VendorType.OTHER
    if entity_type is None:
        entity_type = (rnd.answers or {}).get("entity_type", "")
    return required_items(vtype, entity_type or "")


def optional_items(rnd: OnboardingRound) -> list[str]:
    """Items a full onboarding offers though not required (auto insurance for
    work without vehicles, a licence copy...)."""
    if rnd.mode == RoundMode.UPDATE:
        return []
    required = set(round_items(rnd))
    return [k for k in ITEMS if k not in required]


TEXT_FIELDS = {
    "legal_name": 200,
    "dba": 200,
    "entity_other": 100,
    "contact_name": 120,
    "contact_phone": 40,
    "remit_address": 500,
    "services": 300,
    "coi_gl_carrier": 120,
    "coi_auto_carrier": 120,
    "workers_comp_carrier": 120,
    "hic_registration": 20,
    "licence_text": 500,
    "conflict_details": 1000,
}
# Fields kept as typed in the draft; validated on save and again on submit.
SNAPSHOT_FIELDS = (
    *TEXT_FIELDS,
    "entity_type",
    "contact_email",
    "coi_gl_amount_cents",
    "coi_gl_expires",
    "coi_auto_amount_cents",
    "coi_auto_expires",
    "workers_comp_mode",
    "workers_comp_expires",
    "w9_revision_confirmed",
    "conflict",
    "terms_accepted",
    "terms_version",
)


def save_answers(rnd: OnboardingRound, data: dict) -> OnboardingRound:
    """Store the vendor's draft (the form already parsed and validated each
    field). No actor: the vendor's session was checked by the view."""
    clean: dict = {}
    for key in SNAPSHOT_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key in TEXT_FIELDS:
            value = (value or "").strip()[: TEXT_FIELDS[key]]
            check_free_text(_label(key), value, phone=key == "contact_phone")
        elif isinstance(value, datetime.date):
            value = value.isoformat()
        clean[key] = value
    if clean.get("entity_type") and clean["entity_type"] not in EntityType.values:
        raise OnboardingError("Choose how the business is taxed.")
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status not in VENDOR_EDITABLE_STATES:
            raise OnboardingError("This form has already been submitted.")
        answers = dict(rnd.answers or {})
        answers.update(clean)
        OnboardingRound.objects.filter(pk=rnd.pk).update(answers=answers)
        rnd.answers = answers
    return rnd


LABELS = {
    "legal_name": "Legal name",
    "dba": "Doing business as",
    "entity_other": "Other entity type",
    "contact_name": "Contact name",
    "contact_phone": "Contact phone",
    "remit_address": "Remit-to address",
    "services": "Services",
    "coi_gl_carrier": "General liability insurer",
    "coi_auto_carrier": "Auto liability insurer",
    "workers_comp_carrier": "Workers' compensation insurer",
    "hic_registration": "Home Improvement Contractor registration",
    "licence_text": "Licences",
    "conflict_details": "Conflict of interest details",
}


def _label(key: str) -> str:
    return LABELS.get(key, key)


def current_files(rnd: OnboardingRound) -> dict[str, VendorFile]:
    """Newest upload of each kind in the round."""
    out: dict[str, VendorFile] = {}
    for f in VendorFile.objects.filter(round=rnd).order_by("uploaded_at", "id"):
        out[f.kind] = f
    return out


def latest_scan(f: VendorFile) -> str:
    scan = f.scans.first()
    return scan.result if scan else f.scan_result


def upload_file(rnd: OnboardingRound, kind: str, uploaded, *, request=None) -> VendorFile:
    """Check (PDF only, by content), scan and encrypt one vendor upload. The
    view already stopped anything over MAX_FILE_BYTES before buffering."""
    from documents.models import ScanResult
    from documents.scanning import detail_of, scan
    from documents.validation import UploadRejected, check_upload, safe_filename

    _require_key()
    if kind not in FILE_KINDS:
        raise OnboardingError("Unknown document.")
    item = FILE_KINDS[kind]
    if item not in set(round_items(rnd)) | set(optional_items(rnd)):
        raise OnboardingError("This document isn't asked for on this form.")
    if uploaded is None:
        raise OnboardingError("Choose a PDF file.")
    if uploaded.size > MAX_FILE_BYTES:
        raise OnboardingError(f"Files must be {MAX_FILE_BYTES // (1024 * 1024)} MB or smaller.")
    try:
        filetype = check_upload(uploaded, allowed={"pdf"})
    except UploadRejected as exc:
        raise OnboardingError(str(exc)) from exc
    uploaded.seek(0)
    plaintext = uploaded.read()
    uploaded.seek(0)
    outcome = scan(uploaded)
    result = outcome[0]
    if result == ScanResult.INFECTED:
        raise OnboardingError("The file was flagged by the malware scanner and was not stored.")
    from django.core.files.base import ContentFile

    vf = VendorFile(
        round=rnd,
        kind=kind,
        original_filename=safe_filename(uploaded.name, filetype.ext),
        content_type=filetype.content_type,
        size=len(plaintext),
        sha256=hashlib.sha256(plaintext).hexdigest(),
        scan_result=result,
        scan_detail=detail_of(outcome),
    )
    import uuid

    vf.storage_name = uuid.uuid4().hex
    blob = vendor_crypto.encrypt(plaintext, vf.storage_name)
    del plaintext
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status not in VENDOR_EDITABLE_STATES:
            raise OnboardingError("This form has already been submitted.")
        if VendorFile.objects.filter(round=rnd).count() >= MAX_FILES_PER_ROUND:
            raise OnboardingError(
                "This form already holds the most files it can take. Ask the association "
                "for a new link if you need to start again."
            )
        vf.file.save("upload", ContentFile(blob), save=False)
        vf.save()
        record_event(
            "vendor.onboarding_file_uploaded",
            request=request,
            target=rnd.vendor,
            metadata={"round_id": rnd.pk, "file_id": vf.pk, "kind": kind, "scan": result},
        )
        if result == ScanResult.ERROR:

            def queue():
                from operations.tasks import rescan_vendor_file

                rescan_vendor_file.configure(schedule_in={"seconds": 120}).defer(file_id=vf.pk)

            transaction.on_commit(queue)
    return vf


def rescan(file_id: int) -> str:
    """Worker: scan a stored vendor file again (decrypted in memory only)."""
    import io

    from documents.models import ScanResult
    from documents.scanning import detail_of, scan

    vf = VendorFile.objects.filter(pk=file_id).first()
    if vf is None or file_id in _disposed_file_ids():
        return "gone"
    if latest_scan(vf) in (ScanResult.CLEAN, ScanResult.INFECTED):
        return "done"
    if not vendor_crypto.configured():
        return "no key"
    with vf.file.open("rb") as fh:
        plaintext = vendor_crypto.decrypt(fh.read(), vf.storage_name)
    outcome = scan(io.BytesIO(plaintext))
    VendorFileScan.objects.create(
        file=vf, result=outcome[0], scanner=outcome[1][:50], detail=detail_of(outcome)
    )
    return outcome[0]


def _missing(rnd: OnboardingRound) -> list[str]:
    a = rnd.answers or {}
    files = current_files(rnd)
    problems = []
    if rnd.mode == RoundMode.FULL:
        for key, label in (
            ("legal_name", "your legal name"),
            ("entity_type", "how the business is taxed"),
            ("contact_name", "a contact name"),
            ("contact_email", "a contact email"),
            ("remit_address", "the address checks go to"),
            ("services", "the services you provide"),
        ):
            if not a.get(key):
                problems.append(f"Enter {label}.")
        if a.get("entity_type") == EntityType.OTHER and not a.get("entity_other"):
            problems.append("Say what kind of entity the business is.")
        if a.get("conflict") not in ("yes", "no"):
            problems.append("Answer the conflict of interest question.")
        if a.get("conflict") == "yes" and not a.get("conflict_details"):
            problems.append("Describe the relationship you disclosed.")
        if not a.get("terms_accepted"):
            problems.append("Accept the association's vendor terms.")
    for key in round_items(rnd):
        item = ITEMS[key]
        if item.file_kind and item.file_required and item.file_kind not in files:
            problems.append(f"Upload: {item.label}.")
        if key == "w9" and not a.get("w9_revision_confirmed"):
            problems.append(
                f"Confirm your W-9 is the current IRS revision ({CURRENT_W9_REVISION})."
            )
        if key in ("coi_gl", "coi_auto") and not (
            a.get(f"{key}_carrier") and a.get(f"{key}_amount_cents") and a.get(f"{key}_expires")
        ):
            problems.append(f"{item.label}: enter the insurer, the limit and the expiry date.")
        if key == "workers_comp":
            mode = a.get("workers_comp_mode")
            if mode not in ("certificate", "exempt"):
                problems.append("Say whether you carry workers' compensation or are exempt.")
            elif mode == "certificate" and not (
                a.get("workers_comp_carrier") and a.get("workers_comp_expires")
            ):
                problems.append("Workers' compensation: enter the insurer and expiry date.")
        if key == "hic_registration" and not re.fullmatch(
            r"PA\d{6}", (a.get("hic_registration") or "").upper().replace(" ", "")
        ):
            problems.append(
                "Enter your PA Home Improvement Contractor registration (PA + 6 digits)."
            )
        if key == "licence" and not a.get("licence_text"):
            problems.append("Enter your licence or registration numbers.")
    return problems


def missing(rnd: OnboardingRound) -> list[str]:
    return _missing(rnd)


def current_terms() -> tuple[int, str]:
    terms = VendorTerms.objects.order_by("-version").first()
    if terms is None:
        return 0, DEFAULT_TERMS
    return terms.version, terms.text


def submit(rnd: OnboardingRound, *, request=None) -> OnboardingSubmission:
    """The vendor submits: the round locks, a frozen snapshot is kept, the
    link is used up and the board is told."""
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status not in VENDOR_EDITABLE_STATES:
            raise OnboardingError("This form has already been submitted.")
        problems = _missing(rnd)
        if problems:
            raise OnboardingError(" ".join(problems))
        version, text = current_terms()
        answers = dict(rnd.answers or {})
        if rnd.mode == RoundMode.FULL and int(answers.get("terms_version", -1)) != version:
            raise OnboardingError(
                "The association changed its vendor terms while you were filling in the "
                "form. Please read them again and tick the box."
            )
        files = current_files(rnd)
        wanted = {ITEMS[k].file_kind for k in round_items(rnd) + optional_items(rnd)}
        snapshot_files = [
            {"id": f.pk, "kind": f.kind, "sha256": f.sha256, "size": f.size}
            for kind, f in sorted(files.items())
            if kind in wanted
        ]
        number = (rnd.submissions.aggregate(n=Max("number"))["n"] or 0) + 1
        submission = OnboardingSubmission.objects.create(
            round=rnd,
            number=number,
            answers=answers,
            files=snapshot_files,
            terms_version=version,
            terms_sha256=hashlib.sha256(text.encode()).hexdigest()
            if rnd.mode == RoundMode.FULL
            else "",
        )
        now = timezone.now()
        OnboardingRound.objects.filter(pk=rnd.pk).update(
            status=RoundStatus.SUBMITTED, status_changed_at=now
        )
        OnboardingLink.objects.filter(round=rnd, ended_at__isnull=True).update(
            ended_at=now, end_reason=LinkEnd.SUBMITTED
        )
        profile = profile_of(rnd.vendor)
        if profile is not None and profile.approved_at is None:
            VendorProfile.objects.filter(pk=profile.pk).update(
                status=ProfileStatus.SUBMITTED, status_changed_at=now
            )
        record_event(
            "vendor.onboarding_submitted",
            request=request,
            target=rnd.vendor,
            metadata={"round_id": rnd.pk, "submission_id": submission.pk},
        )
        _tell_board_submitted(rnd, submission)
    return submission


def _tell_board_submitted(rnd, submission) -> None:
    from comms.notify import send_now
    from community.services import board_emails

    url = settings.PORTAL_BASE_URL + reverse("vendor_onboarding_review", args=[rnd.pk])
    send_now(
        board_emails(),
        subject="Vendor onboarding waiting for review",
        body=(
            "A vendor submitted its onboarding form. Two board members must approve it before "
            f"the vendor is active.\n\nReview it in the portal:\n{url}\n"
        ),
        board=True,
    )


# --- Board review ------------------------------------------------------------------------


def latest_submission(rnd: OnboardingRound) -> OnboardingSubmission | None:
    return rnd.submissions.order_by("-number").first()


def approvers(submission: OnboardingSubmission) -> set[int]:
    return set(
        submission.decisions.filter(decision=Decision.APPROVE).values_list(
            "decided_by_id", flat=True
        )
    )


def approval_refusal(rnd: OnboardingRound, actor) -> str | None:
    """Why ``actor`` may not approve (shown before any button), or None."""
    if actor is SYSTEM:
        return None
    if not policy.can(actor, "vendor.onboarding_approve"):
        return "Only board members approve vendors."
    if rnd.created_by_id == actor.pk:
        return "You sent this vendor its onboarding link; two other board members approve it."
    if person_conflicted(actor, rnd.vendor_id):
        return (
            "You are named in this vendor's conflict-of-interest disclosure: other board "
            "members decide."
        )
    submission = latest_submission(rnd)
    if submission is not None and actor.pk in approvers(submission):
        return (
            "You have approved this submission; a different board member gives the second approval."
        )
    return None


def _refuse_conflicted(actor, rnd, request) -> None:
    if actor is not SYSTEM and person_conflicted(actor, rnd.vendor_id):
        from core import conflicts

        conflicts.audit_refusal(actor, rnd.vendor, area="vendor onboarding", request=request)
        raise OnboardingError(
            "You are named in this vendor's conflict-of-interest disclosure: other board "
            "members decide."
        )


def approve(rnd: OnboardingRound, *, actor, note: str = "", request=None) -> OnboardingRound:
    require(actor, "vendor.onboarding_approve")
    _refuse_conflicted(actor, rnd, request)
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status != RoundStatus.SUBMITTED:
            raise OnboardingError("Only a submitted onboarding can be approved.")
        problem = approval_refusal(rnd, actor)
        if problem:
            raise OnboardingError(problem)
        submission = latest_submission(rnd)
        try:
            with transaction.atomic():
                OnboardingDecision.objects.create(
                    submission=submission,
                    decision=Decision.APPROVE,
                    decided_by=actor,
                    reason=(note or "")[:2000],
                )
        except IntegrityError as exc:
            raise OnboardingError("You have already approved this submission.") from exc
        have = len(approvers(submission))
        record_event(
            "vendor.onboarding_decided",
            actor=actor,
            request=request,
            target=rnd.vendor,
            metadata={
                "submission_id": submission.pk,
                "decision": Decision.APPROVE,
                "have": have,
                "need": APPROVALS_NEEDED,
            },
        )
        if have >= APPROVALS_NEEDED:
            _activate(rnd, submission, request=request)
            rnd.refresh_from_db()
    return rnd


def _date(value) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return None


def _activate(rnd, submission, *, request=None) -> None:
    now = timezone.now()
    a = submission.answers or {}
    vendor = Vendor.objects.select_for_update().get(pk=rnd.vendor_id)
    profile = VendorProfile.objects.select_for_update().get(vendor=vendor)
    files = {f["kind"]: f["id"] for f in submission.files}
    items = set(round_items(rnd)) | set(optional_items(rnd))
    changes: dict = {"status": ProfileStatus.ACTIVE, "status_changed_at": now}
    if rnd.mode == RoundMode.FULL:
        changes.update(
            legal_name=a.get("legal_name", ""),
            dba=a.get("dba", ""),
            entity_type=a.get("entity_type", ""),
            remit_address=a.get("remit_address", ""),
            conflict_disclosed=a.get("conflict") == "yes",
            approved_answers=a,
        )
        if profile.approved_at is None:
            changes["approved_at"] = now
    else:
        merged = dict(profile.approved_answers or {})
        merged.update(a)
        changes["approved_answers"] = merged
    if "w9" in files:
        changes.update(
            w9_file_id=files["w9"],
            w9_received_on=timezone.localdate(submission.submitted_at),
            w9_revision=CURRENT_W9_REVISION,
        )
    if "coi_gl" in items and _date(a.get("coi_gl_expires")):
        changes["coi_expires_on"] = _date(a.get("coi_gl_expires"))
    if a.get("hic_registration"):
        changes["hic_registration"] = a["hic_registration"].upper().replace(" ", "")[:40]
    if a.get("licence_text"):
        changes["licences"] = a["licence_text"]
    VendorProfile.objects.filter(pk=profile.pk).update(**changes)
    vendor_changes: dict = {"active": True}
    if "w9" in files:
        vendor_changes["w9_on_file"] = True
    if changes.get("coi_expires_on"):
        vendor_changes["insurance_expires_on"] = changes["coi_expires_on"]
    if rnd.mode == RoundMode.FULL:
        if a.get("contact_name"):
            vendor_changes["contact_name"] = a["contact_name"][:120]
        if a.get("contact_phone"):
            vendor_changes["phone"] = a["contact_phone"][:40]
        if a.get("remit_address"):
            vendor_changes["address"] = a["remit_address"]
        if a.get("services"):
            vendor_changes["services"] = a["services"][:200]
    Vendor.objects.filter(pk=vendor.pk).update(**vendor_changes)
    OnboardingRound.objects.filter(pk=rnd.pk).update(
        status=RoundStatus.APPROVED, status_changed_at=now
    )
    record_event(
        "vendor.onboarding_activated", request=request, target=vendor, metadata={"round_id": rnd.pk}
    )


def return_to_vendor(rnd: OnboardingRound, *, reason: str, actor, request=None) -> OnboardingLink:
    """Send it back with a reason: a new link; the submission stays as history."""
    require(actor, "vendor.onboarding_approve")
    _refuse_conflicted(actor, rnd, request)
    _require_key()
    reason = (reason or "").strip()
    if not reason:
        raise OnboardingError("Say what the vendor should change.")
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status != RoundStatus.SUBMITTED:
            raise OnboardingError("Only a submitted onboarding can be returned.")
        submission = latest_submission(rnd)
        OnboardingDecision.objects.create(
            submission=submission, decision=Decision.RETURN, decided_by=actor, reason=reason[:2000]
        )
        now = timezone.now()
        OnboardingRound.objects.filter(pk=rnd.pk).update(
            status=RoundStatus.RETURNED, status_changed_at=now, return_reason=reason[:2000]
        )
        profile = profile_of(rnd.vendor)
        if profile is not None and profile.approved_at is None:
            VendorProfile.objects.filter(pk=profile.pk).update(
                status=ProfileStatus.RETURNED, status_changed_at=now
            )
        record_event(
            "vendor.onboarding_decided",
            actor=actor,
            request=request,
            target=rnd.vendor,
            metadata={"submission_id": submission.pk, "decision": Decision.RETURN},
        )
        rnd.status = RoundStatus.RETURNED
        return _new_link(rnd, actor=actor, request=request)


def decline(rnd: OnboardingRound, *, reason: str, actor, request=None) -> None:
    require(actor, "vendor.onboarding_approve")
    _refuse_conflicted(actor, rnd, request)
    reason = (reason or "").strip()
    if not reason:
        raise OnboardingError("Give a reason for declining.")
    with transaction.atomic():
        rnd = OnboardingRound.objects.select_for_update().get(pk=rnd.pk)
        if rnd.status != RoundStatus.SUBMITTED:
            raise OnboardingError("Only a submitted onboarding can be declined.")
        submission = latest_submission(rnd)
        OnboardingDecision.objects.create(
            submission=submission, decision=Decision.DECLINE, decided_by=actor, reason=reason[:2000]
        )
        now = timezone.now()
        OnboardingRound.objects.filter(pk=rnd.pk).update(
            status=RoundStatus.DECLINED, status_changed_at=now
        )
        profile = profile_of(rnd.vendor)
        if profile is not None and profile.approved_at is None:
            VendorProfile.objects.filter(pk=profile.pk).update(
                status=ProfileStatus.DECLINED, status_changed_at=now
            )
        record_event(
            "vendor.onboarding_decided",
            actor=actor,
            request=request,
            target=rnd.vendor,
            metadata={"submission_id": submission.pk, "decision": Decision.DECLINE},
        )


# --- Conflicts ---------------------------------------------------------------------------


def link_conflict(vendor, person, *, note: str = "", removed: bool = False, actor, request=None):
    """Link (or end the link of) a person to a vendor's conflict disclosure.
    Anyone who invites vendors may add a link. Ending one (EXP-002, D65) is a
    board decision with a reason, taken by a board member who is neither the
    person named nor conflicted on that vendor, never the manager; the rows
    are append-only, so the earlier link stays on record."""
    require(actor, "vendor.onboarding_invite")
    vendor = _vendor(vendor)
    if removed:
        require(actor, "vendor.onboarding_approve")
        _refuse_conflict_removal(actor, vendor, person, request)
        if not (note or "").strip():
            raise OnboardingError("Say why the conflict link no longer applies.")
    row = VendorConflict.objects.create(
        vendor=vendor, person=person, removed=removed, note=(note or "")[:200], recorded_by=actor
    )
    record_event(
        "vendor.conflict_linked",
        actor=actor,
        request=request,
        target=vendor,
        metadata={"person_id": person.pk, "removed": removed},
    )
    return row


def _refuse_conflict_removal(actor, vendor, person, request) -> None:
    if actor is SYSTEM:
        return
    own = policy.roles_for(actor).person_id == person.pk
    if own or person_conflicted(actor, vendor.pk):
        from core import conflicts

        conflicts.audit_refusal(
            actor, vendor, area="vendor onboarding", action="conflict_remove", request=request
        )
        reason = (
            "You cannot end a conflict link that names you"
            if own
            else "You are named in this vendor's conflict-of-interest disclosure"
        )
        raise OnboardingError(reason + ": another board member decides.")


def possible_conflict_people(text: str):
    """People whose full name appears in a disclosure: a prompt for the
    board, never decided automatically."""
    from community.models import Person
    from core.scoping import system_access

    text = (text or "").lower()
    if len(text) < 5:
        return []
    out = []
    with system_access("vendor conflict names"):
        for person in Person.objects.all().only("pk", "display_name"):
            name = (person.display_name or "").strip().lower()
            if len(name.split()) >= 2 and name in text:
                out.append(person)
    return out


# --- Configuration -----------------------------------------------------------------------


def set_requirement(
    *, vendor_type: str, item: str, required: bool, citation: str, actor, request=None
):
    require(actor, "vendor.onboarding_configure")
    if vendor_type not in VendorType.values or item not in ITEMS:
        raise OnboardingError("Unknown vendor type or item.")
    citation = (citation or "").strip()
    if not citation:
        raise OnboardingError("Give the citation (board resolution or policy) for this change.")
    row = VendorRequirement.objects.create(
        vendor_type=vendor_type,
        item=item,
        required=bool(required),
        citation=citation[:200],
        set_by=actor,
    )
    record_event(
        "vendor.requirement_set",
        actor=actor,
        request=request,
        metadata={"vendor_type": vendor_type, "item": item, "required": bool(required)},
    )
    return row


def save_terms(*, text: str, citation: str = "", actor, request=None) -> VendorTerms:
    require(actor, "vendor.onboarding_configure")
    text = (text or "").strip()
    if not text:
        raise OnboardingError("The terms can't be empty.")
    with transaction.atomic():
        version = (VendorTerms.objects.aggregate(v=Max("version"))["v"] or 0) + 1
        terms = VendorTerms.objects.create(
            version=version, text=text[:20000], citation=(citation or "")[:200], created_by=actor
        )
    record_event("vendor.terms_saved", actor=actor, request=request, metadata={"version": version})
    return terms


# --- Documents: viewing, history, retention ---------------------------------------------


def may_view_file(user, vf: VendorFile) -> bool:
    if vf.kind == "w9":
        return policy.can(user, "vendor.w9_view")
    return policy.can(user, "vendor.onboarding_review")


def open_file(vf: VendorFile, *, actor, request=None) -> bytes:
    """Decrypt a vendor document for an authorised download. Audited (ids
    only); refused unless the latest scan allows downloads."""
    from documents.services import scan_allows_download

    if vf.kind == "w9":
        require(actor, "vendor.w9_view")
    else:
        require(actor, "vendor.onboarding_review")
    if not vendor_crypto.configured():
        raise OnboardingError(vendor_crypto.MISSING_TEXT)
    if vf.pk in _disposed_file_ids():
        raise OnboardingError("This document was disposed of after its retention period.")
    scan = latest_scan(vf)
    if not scan_allows_download(scan):
        raise OnboardingError("This file can't be opened until the malware scan passes.")
    with vf.file.open("rb") as fh:
        try:
            data = vendor_crypto.decrypt(fh.read(), vf.storage_name)
        except vendor_crypto.VendorKeyMismatch as exc:
            raise OnboardingError(str(exc)) from exc
    action = "vendor.w9_viewed" if vf.kind == "w9" else "vendor.document_viewed"
    meta = {"file_id": vf.pk, "scan": scan}
    if vf.kind != "w9":
        meta["kind"] = vf.kind
    record_event(action, actor=actor, request=request, target=vf.round.vendor, metadata=meta)
    return data


def access_history(vendor, limit: int = 100):
    """Who opened this vendor's documents (audit rows: ids and dates only)."""
    from audit.models import AuditEvent

    vendor = _vendor(vendor)
    return (
        AuditEvent.objects.filter(
            action__in=["vendor.w9_viewed", "vendor.document_viewed"],
            target_type="operations.vendor",
            target_id=str(vendor.pk),
        )
        .select_related("actor")
        .order_by("-occurred_at")[:limit]
    )


def _today() -> datetime.date:
    return timezone.localdate()


def retention_years() -> int:
    from community.rules import rule

    return max(1, int(rule("vendor.w9_retention_years") or 4))


def retention_due(on: datetime.date | None = None) -> list[dict]:
    """W-9s past their retention: kept ``vendor.w9_retention_years`` after
    the last calendar year in which the vendor was paid while it was the
    vendor's W-9 (the last 1099 year it supports), or after the year it was
    received if never paid. Listed for two-person disposal."""
    from operations.models import InvoicePayment

    on = on or _today()
    years = retention_years()
    disposed = _disposed_file_ids()
    out = []
    w9s = list(
        VendorFile.objects.filter(kind="w9").select_related("round__vendor").order_by("uploaded_at")
    )
    by_vendor: dict[int, list[VendorFile]] = {}
    for f in w9s:
        by_vendor.setdefault(f.round.vendor_id, []).append(f)
    for vendor_id, files in by_vendor.items():
        for i, f in enumerate(files):
            if f.pk in disposed:
                continue
            start = timezone.localdate(f.uploaded_at)
            end = timezone.localdate(files[i + 1].uploaded_at) if i + 1 < len(files) else None
            payments = InvoicePayment.objects.filter(
                invoice__vendor_id=vendor_id, paid_on__gte=start, amount_cents__gt=0
            )
            if end is not None:
                payments = payments.filter(paid_on__lt=end)
            last_paid = payments.aggregate(d=Max("paid_on"))["d"]
            if (
                end is None
                and VendorProfile.objects.filter(vendor_id=vendor_id, w9_file_id=f.pk).exists()
                and Vendor.objects.filter(pk=vendor_id, active=True).exists()
            ):
                continue  # the W-9 an active vendor is paid under now
            support_year = max(last_paid.year if last_paid else start.year, start.year)
            keep_until = datetime.date(support_year + years, 12, 31)
            if on > keep_until:
                proposal = f.disposals.filter(step=DisposalStep.PROPOSE).first()
                out.append(
                    {
                        "file": f,
                        "vendor": f.round.vendor,
                        "support_year": support_year,
                        "keep_until": keep_until,
                        "proposal": proposal,
                    }
                )
    return out


def propose_disposal(vf: VendorFile, *, reason: str, actor, request=None) -> VendorFileDisposal:
    require(actor, "vendor.w9_dispose")
    if vf.pk not in {r["file"].pk for r in retention_due()}:
        raise OnboardingError("This document isn't past its retention period.")
    try:
        with transaction.atomic():
            row = VendorFileDisposal.objects.create(
                file=vf,
                step=DisposalStep.PROPOSE,
                by=actor,
                reason=(reason or "Retention ended")[:200],
            )
    except IntegrityError as exc:
        raise OnboardingError("Disposal is already proposed.") from exc
    record_event(
        "vendor.document_disposal",
        actor=actor,
        request=request,
        target=vf.round.vendor,
        metadata={"file_id": vf.pk, "step": "propose"},
    )
    return row


def confirm_disposal(vf: VendorFile, *, actor, request=None) -> VendorFileDisposal:
    require(actor, "vendor.w9_dispose")
    proposal = vf.disposals.filter(step=DisposalStep.PROPOSE).first()
    if proposal is None:
        raise OnboardingError("Another board member must propose the disposal first.")
    if proposal.by_id == actor.pk:
        raise OnboardingError("A different board member confirms the disposal.")
    if vf.pk not in {r["file"].pk for r in retention_due()}:
        raise OnboardingError("This document isn't past its retention period.")
    try:
        with transaction.atomic():
            row = VendorFileDisposal.objects.create(
                file=vf, step=DisposalStep.CONFIRM, by=actor, reason=proposal.reason
            )
    except IntegrityError as exc:
        raise OnboardingError("Already disposed of.") from exc
    vf.file.storage.delete(vf.file.name)
    record_event(
        "vendor.document_disposal",
        actor=actor,
        request=request,
        target=vf.round.vendor,
        metadata={"file_id": vf.pk, "step": "confirm"},
    )
    return row


def reencrypt_all() -> int:
    """Rewrite every stored blob under the current key (after rotation)."""
    from django.core.files.base import ContentFile

    if not vendor_crypto.configured():
        raise vendor_crypto.VendorKeyMissing()
    disposed = _disposed_file_ids()
    count = 0
    for vf in VendorFile.objects.order_by("id"):
        if vf.pk in disposed or not vf.file.name:
            continue
        with vf.file.open("rb") as fh:
            blob = fh.read()
        if vendor_crypto.key_index(blob, vf.storage_name) == 0:
            continue
        plaintext = vendor_crypto.decrypt(blob, vf.storage_name)
        storage, name = vf.file.storage, vf.file.name
        storage.delete(name)
        storage.save(name, ContentFile(vendor_crypto.encrypt(plaintext, vf.storage_name)))
        count += 1
    record_event("vendor.files_reencrypted", metadata={"count": count})
    return count


# --- Vendor file summary (PDF, no TIN) --------------------------------------------------


def vendor_file_pdf(vendor, *, actor, request=None) -> bytes:
    from core import pdf
    from core.dates import short_date

    require(actor, "vendor.onboarding_review")
    vendor = _vendor(vendor)
    profile = profile_of(vendor)
    a = (profile.approved_answers if profile else {}) or {}
    approved = (
        short_date(timezone.localdate(profile.approved_at))
        if profile is not None and profile.approved_at
        else "-"
    )
    lines = [
        ("heading", f"Vendor file: {vendor.name}"),
        (
            "small",
            f"{association_name()}. Prepared {short_date(timezone.localdate())}. "
            "No tax identification number is held outside the encrypted W-9.",
        ),
        ("gap", ""),
        ("normal", f"Status: **{_status_text(vendor)}**"),
    ]
    if profile is not None:
        lines += [
            ("normal", f"Kind of work: {profile.get_vendor_type_display()}"),
            ("normal", f"Legal name: {profile.legal_name or '-'}"),
            ("normal", f"Doing business as: {profile.dba or '-'}"),
            ("normal", f"Taxed as: {profile.get_entity_type_display() or '-'}"),
            ("normal", f"Form 1099: {_yes_no(eligible_for_1099(vendor))} ({CITATION_1099})"),
            ("normal", f"Remit-to address (checks only): {profile.remit_address or '-'}"),
            (
                "normal",
                f"W-9 on file: {_yes_no(w9_on_file(vendor))}"
                + (
                    f", received {short_date(profile.w9_received_on)} ({profile.w9_revision})"
                    if profile.w9_received_on
                    else ""
                ),
            ),
            (
                "normal",
                f"General liability: {a.get('coi_gl_carrier') or '-'}, "
                f"{_money(a.get('coi_gl_amount_cents'))}, "
                f"expires {a.get('coi_gl_expires') or '-'}",
            ),
            (
                "normal",
                f"Auto liability: {a.get('coi_auto_carrier') or '-'}, "
                f"expires {a.get('coi_auto_expires') or '-'}",
            ),
            (
                "normal",
                "Workers' compensation: "
                + {
                    "certificate": f"certificate, {a.get('workers_comp_carrier') or '-'}, "
                    f"expires {a.get('workers_comp_expires') or '-'}",
                    "exempt": "exemption statement on file",
                }.get(a.get("workers_comp_mode"), "-"),
            ),
            (
                "normal",
                f"PA Home Improvement Contractor registration: {profile.hic_registration or '-'}",
            ),
            ("normal", f"Licences: {profile.licences or '-'}"),
            (
                "normal",
                f"Conflict of interest disclosed: {'Yes' if profile.conflict_disclosed else 'No'}",
            ),
            (
                "normal",
                f"Approved: {approved}",
            ),
        ]
    else:
        lines.append(
            (
                "normal",
                "Added before vendor onboarding; missing: "
                + (", ".join(missing_for_grandfathered(vendor)) or "nothing"),
            )
        )
    lines.append(("gap", ""))
    lines.append(("note", PREPARER_CAVEAT))
    record_event("vendor.file_summary_downloaded", actor=actor, request=request, target=vendor)
    return pdf.render(lines, title=f"Vendor file {vendor.pk}")


def _money(cents) -> str:
    from core.money import format_cents

    return format_cents(int(cents)) if cents else "-"


def _yes_no(value) -> str:
    return {True: "Yes", False: "No"}.get(value, "Not known yet")


def _status_text(vendor) -> str:
    profile = profile_of(vendor)
    if profile is None:
        return "Active (added before vendor onboarding)" if vendor.active else "Inactive"
    if profile.approved_at and vendor.active:
        return "Active (approved by two board members)"
    return profile.get_status_display()


def status_text(vendor) -> str:
    return _status_text(_vendor(vendor))


# --- My items ----------------------------------------------------------------------------


def my_items(user) -> list:
    from core.dates import short_date
    from core.my_items import Item

    out = []
    if not _feature_on():
        return out
    if policy.can(user, "vendor.onboarding_approve"):
        for rnd in OnboardingRound.objects.filter(status=RoundStatus.SUBMITTED).select_related(
            "vendor"
        ):
            if approval_refusal(rnd, user):
                continue
            submission = latest_submission(rnd)
            have = len(approvers(submission)) if submission else 0
            out.append(
                Item(
                    "Vendors",
                    f"Review vendor onboarding: {rnd.vendor.name}",
                    f"{have} of {APPROVALS_NEEDED} approvals · submitted "
                    f"{short_date(timezone.localdate(rnd.status_changed_at))}",
                    reverse("vendor_onboarding_review", args=[rnd.pk]),
                    "warning",
                )
            )
    if policy.can(user, "vendor.onboarding_invite"):
        now = timezone.now()
        for rnd in OnboardingRound.objects.filter(status__in=VENDOR_EDITABLE_STATES).select_related(
            "vendor"
        ):
            if rnd.links.filter(ended_at__isnull=True, expires_at__gt=now).exists():
                continue
            out.append(
                Item(
                    "Vendors",
                    f"{rnd.vendor.name}: onboarding link expired",
                    "Send a new link or cancel the request",
                    reverse("vendor_onboarding_review", args=[rnd.pk]),
                    "warning",
                )
            )
    if policy.can(user, "vendor.manage"):
        profiled = VendorProfile.objects.values_list("vendor_id", flat=True)
        for vendor in Vendor.objects.filter(active=True).exclude(pk__in=profiled):
            gaps = missing_for_grandfathered(vendor)
            if gaps:
                out.append(
                    Item(
                        "Vendors",
                        f"Complete onboarding for {vendor.name}",
                        "Missing: " + ", ".join(gaps),
                        reverse("vendor_onboarding_vendor", args=[vendor.pk]),
                        "info",
                    )
                )
    if policy.can(user, "vendor.w9_dispose"):
        due = retention_due()
        if due:
            out.append(
                Item(
                    "Vendors",
                    f"{len(due)} W-9{'s' if len(due) != 1 else ''} past retention to review for "
                    "disposal",
                    "Two board members dispose of each",
                    reverse("vendor_w9_retention"),
                    "info",
                )
            )
    return out
