"""Registry of governing rules (CLAUDE.md rule 6).

Each rule's *value* and *citation* are data in community.GoverningRule, set
by the board from the Declaration, bylaws or a recorded board decision. Code
reads them through ``rule()``; nothing here hard-codes a community's numbers.
Defaults are used only when noted, and a rule without a citation is listed
as needing attention on the board dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CENTS, DAYS, INT, PERCENT, BOOL, TEXT = "cents", "days", "int", "percent", "bool", "text"


@dataclass(frozen=True)
class RuleSpec:
    key: str
    label: str
    kind: str
    default: str | None = None  # used until the board sets a value
    group: str = "General"
    help: str = ""
    maximum: int | None = None  # a hard upper limit (card-network caps)

    # Labels and help may name the monthly charge with {term}/{Terms}
    # placeholders (community/terms.py, D42); people always see these.
    @property
    def shown_label(self) -> str:
        from community.terms import term_text

        return term_text(self.label)

    @property
    def shown_help(self) -> str:
        from community.terms import term_text

        return term_text(self.help)


def group_title(group: str, term=None) -> str:
    """The heading people see for a group: the "Dues" group is named after
    the board's word for the monthly charge ("Assessments", D42)."""
    if group == "Dues":
        from community.terms import charge_term

        return (term or charge_term()).variants()["Terms"]
    return group


REGISTRY: dict[str, RuleSpec] = {
    spec.key: spec
    for spec in [
        # D42: what the monthly charge is called on pages, emails, letters and
        # new ledger entries. No default here: the Act's own word
        # ("assessments") is the statutory fallback in community/pa_law.py.
        RuleSpec(
            "charge.term",
            "What the regular monthly charge is called",
            TEXT,
            None,
            "Dues",
            "The word in your covenants, e.g. Assessment, Dues or Fee. For an unusual plural "
            "type both: Levy / Levies. Used on pages, emails, letters and new charges; charges "
            "already posted keep their wording.",
        ),
        RuleSpec(
            "dues.monthly_cents",
            "Monthly {term} per home",
            CENTS,
            None,
            "Dues",
            "Charged on the 1st of each month to every home's account.",
        ),
        RuleSpec("dues.due_day", "Day of the month the {term} is due", DAYS, "1", "Dues"),
        RuleSpec(
            "dues.auto_post",
            "Post the monthly {term} automatically on the 1st",
            BOOL,
            "false",
            "Dues",
            "Board decision. Otherwise the board posts each month with a button.",
        ),
        RuleSpec(
            "late_fee.cents",
            "Late fee per late month",
            CENTS,
            None,
            "Dues",
            "Posted by the board from the late list, or automatically if switched on below.",
        ),
        RuleSpec(
            "late_fee.auto_apply",
            "Post late fees automatically after the grace period",
            BOOL,
            "false",
            "Dues",
            "Off: the board reviews the late list and posts fees with a button.",
        ),
        RuleSpec(
            "collections.notice_days",
            "Days given in a delinquency notice to bring the account current",
            DAYS,
            "10",
            "Dues",
            "Printed in the first and second notices (collection policy).",
        ),
        RuleSpec(
            "collections.courtesy_reminder_day",
            "Day of the month for a courtesy email to owners with a past-due balance",
            INT,
            None,
            "Dues",
            "Blank means no courtesy emails. Formal notices are the printed letters.",
        ),
        RuleSpec(
            "accounting.1099_threshold_cents",
            "Payments to a vendor in a year at which a 1099 is likely needed",
            CENTS,
            "200000",
            "Dues",
            "Used to flag vendors on the 1099 worksheet; the treasurer decides.",
        ),
        RuleSpec(
            "late_fee.grace_days",
            "Days after the due date before a payment is late",
            DAYS,
            None,
            "Dues",
        ),
        RuleSpec(
            "invoice.dual_approval_cents",
            "Invoice amount needing two board approvals",
            CENTS,
            "50000",
            "Invoices",
            "Board decision: two approvals at $500 and over.",
        ),
        RuleSpec(
            "arc.completeness_days",
            "Days to check an ARC request is complete",
            DAYS,
            None,
            "Architectural review",
        ),
        RuleSpec(
            "arc.review_days",
            "Days to decide a complete ARC request",
            DAYS,
            None,
            "Architectural review",
        ),
        RuleSpec(
            "arc.deemed_approval",
            "Request is deemed approved if not decided in time",
            BOOL,
            None,
            "Architectural review",
        ),
        RuleSpec(
            "arc.appeal_days",
            "Days an applicant has to appeal an ARC decision",
            DAYS,
            None,
            "Architectural review",
        ),
        RuleSpec(
            "arc.signoff_auto_confirm",
            "Accept automatically read sign-offs without a person confirming",
            BOOL,
            "false",
            "Architectural review",
            "Off: a board member or the manager confirms each signature read from a scanned form.",
        ),
        RuleSpec(
            "arc.signoff_respond_days",
            "Days neighbors are asked to respond within",
            DAYS,
            None,
            "Architectural review",
            "Shown to neighbors as 'Please respond by ...'. Empty: no date is shown.",
        ),
        RuleSpec("violation.cure_days", "Days given to cure a violation", DAYS, None, "Violations"),
        RuleSpec(
            "violation.hearing_notice_days",
            "Notice before a violation hearing",
            DAYS,
            None,
            "Violations",
        ),
        RuleSpec(
            "meeting.owner_notice_days", "Notice to owners before a meeting", DAYS, None, "Meetings"
        ),
        RuleSpec(
            "records.response_days", "Days to respond to a records request", DAYS, None, "Records"
        ),
        RuleSpec("resale.fee_cents", "Resale certificate fee", CENTS, None, "Resale"),
        RuleSpec(
            "resale.deadline_days", "Days to provide a resale certificate", DAYS, None, "Resale"
        ),
        RuleSpec(
            "vote.quorum_percent", "Quorum for formal votes (% of homes)", PERCENT, None, "Voting"
        ),
        RuleSpec(
            "vote.pass_percent", "Share of votes cast needed to pass", PERCENT, None, "Voting"
        ),
        RuleSpec(
            "ledger.assessments_before_fees",
            "Apply payments to {terms} before late fees and other charges",
            BOOL,
            "false",
            "Dues",
            "Off: each payment pays the oldest charge first. On: {terms} first (oldest first), "
            "then "
            "late fees and other charges. An owner may still direct a payment to chosen charges.",
        ),
        # Decision D32-payments (replaces the D21 card and bank fees, whose
        # recorded values stay in the rule history): credit cards only.
        RuleSpec(
            "payments.credit_card_surcharge_bp",
            "Credit card surcharge (hundredths of a percent: 300 = 3%; at most 300)",
            INT,
            "300",
            "Online payments",
            "Added to credit card payments only and shown before the owner pays. Bank and "
            "debit card payments never carry a fee.",
            maximum=300,
        ),
        # D44: official notices go by email only to owners who agreed.
        RuleSpec(
            "notices.electronic_consent_required",
            "Ask owners to agree before official notices go to them by email",
            BOOL,
            "true",
            "Notices",
            "On: new owners choose email or post when they sign up, and owners who have not "
            "answered see a to-do. Official notices are always emailed only to owners who "
            "agreed; everyone else gets paper. Turning this off only stops asking.",
        ),
        # Automatic payments (D46): a safety ceiling, not a fee.
        RuleSpec(
            "payments.autopay_ceiling_percent",
            "Automatic payment ceiling, as a percent of the monthly {term} (300 = three months)",
            INT,
            "300",
            "Online payments",
            "An automatic payment above this is not charged; the board gets an item instead. "
            "Automatic payments only ever pay the month's assessment.",
        ),
        # Disbursements (D52): money paid back to owners.
        RuleSpec(
            "disbursement.surplus_equal_shares",
            "Split a surplus distribution equally per home",
            BOOL,
            "true",
            "Disbursements",
            "On: every home with an owner gets the same share (the remainder cent by cent in "
            "address order), as for an equal monthly {term}. Off if the Declaration allocates "
            "common expenses unequally: the portal then does not split a surplus.",
        ),
        RuleSpec(
            "disbursement.second_approval_cents",
            "Disbursement total needing a second board approval",
            CENTS,
            "50000",
            "Disbursements",
            "At or over this, two board members other than the one who proposed it must "
            "approve. A surplus distribution approved by an owner who receives part of it "
            "always needs two.",
        ),
        RuleSpec(
            "retention.former_owner_contact_days",
            "Days after someone leaves before their contact details are cleared",
            DAYS,
            "730",
            "Privacy",
            "Former owners and contacts: phone, mailing address, extra contact methods and "
            "directory choices are cleared; names and records are kept. 0 means never.",
        ),
        *[
            RuleSpec(
                f"request.sla_days.{key}",
                f"First response target: {label.lower()}s (business days)",
                DAYS,
                default,
                "Owner requests",
                "Board decision. Requests not answered by then are flagged as overdue.",
            )
            for key, label, default in (
                ("general", "General question", "5"),
                ("billing", "Billing question", "5"),
                ("maintenance", "Maintenance report", "3"),
                ("common_area", "Common area issue", "3"),
            )
        ],
    ]
}


def _parse(spec: RuleSpec, raw: str | None):
    if raw in (None, ""):
        return None
    if spec.kind in (CENTS, DAYS, INT, PERCENT):
        return int(raw)
    if spec.kind == BOOL:
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


def validate_value(key: str, raw: str) -> str:
    spec = REGISTRY[key]
    raw = (raw or "").strip()
    if raw == "":
        return raw
    if spec.kind in (CENTS, DAYS, INT, PERCENT):
        # ASCII digits only and a sane size (review D9): str.isdigit() also
        # accepts characters like "²" that int() then refuses on every read.
        if not re.fullmatch(r"[0-9]{1,12}", raw):
            raise ValueError(f"{spec.shown_label}: enter a whole number.")
        if spec.kind == PERCENT and int(raw) > 100:
            raise ValueError(f"{spec.shown_label}: must be 0-100.")
        if spec.maximum is not None and int(raw) > spec.maximum:
            raise ValueError(f"{spec.shown_label}: at most {spec.maximum}.")
    if spec.kind == BOOL and raw.lower() not in ("true", "false", "yes", "no", "1", "0"):
        raise ValueError(f"{spec.shown_label}: enter yes or no.")
    if spec.kind == TEXT:
        from community.terms import VALID

        if not VALID.fullmatch(raw):
            raise ValueError(
                f"{spec.shown_label}: one or two plain words (letters only), e.g. Assessment, "
                "or Levy / Levies."
            )
    return raw


def _resolve(key: str) -> tuple[str | None, str, str]:
    """(raw value, citation, origin) where origin is "recorded", "default",
    "pa_law" (a statutory fallback, community/pa_law.py) or "unset"."""
    from community.models import GoverningRule
    from community.pa_law import guide

    spec = REGISTRY[key]
    row = GoverningRule.objects.filter(key=key).first()
    citation = row.citation if row else ""
    if row and row.value != "":
        return row.value, citation, "recorded"
    if spec.default is not None:
        return spec.default, citation, "default"
    g = guide(key)
    if g and g.fallback is not None:
        return g.fallback, g.fallback_citation, "pa_law"
    return None, citation, "unset"


def rule(key: str):
    """The rule's value (parsed): the board's recorded value, else the default,
    else the Pennsylvania statutory fallback, else None."""
    raw, _citation, _origin = _resolve(key)
    return _parse(REGISTRY[key], raw)


def rule_with_citation(key: str) -> tuple[object, str]:
    raw, citation, _origin = _resolve(key)
    return _parse(REGISTRY[key], raw), citation


def rule_origin(key: str) -> str:
    return _resolve(key)[2]


def rules_needing_attention() -> list[RuleSpec]:
    """Rules with no value, or a value but no citation. A statutory fallback
    carries its own citation, so it does not need attention (though the
    board should still check its documents don't set something stricter)."""
    from community.models import GoverningRule
    from community.pa_law import guide

    rows = {r.key: r for r in GoverningRule.objects.all()}
    missing = []
    for spec in REGISTRY.values():
        row = rows.get(spec.key)
        if row and row.value != "":
            if not row.citation:
                missing.append(spec)
            continue
        if spec.default is not None:
            if not (row and row.citation):
                missing.append(spec)
            continue
        g = guide(spec.key)
        if not (g and g.fallback is not None):
            missing.append(spec)
    return missing


def cents_display(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"
