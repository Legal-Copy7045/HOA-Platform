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
# Stored as whole hundredths of a percent (basis points), typed and shown as a
# percent: "3" or "2.5" on the page is 300 or 250 in the database (WT-007).
BASIS_POINTS = "bp"


@dataclass(frozen=True)
class RuleSpec:
    key: str
    label: str
    kind: str
    default: str | None = None  # used until the board sets a value
    group: str = "General"
    help: str = ""
    maximum: int | None = None  # a hard upper limit (card-network caps)
    minimum: int | None = None  # a hard lower limit (security floors, D58)

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
            "Accept neighbor acknowledgements read from scanned forms without a person checking",
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
        # D61: every formal vote and election has a voting window.
        RuleSpec(
            "vote.min_open_days",
            "Shortest voting period for formal votes and elections (days)",
            DAYS,
            "7",
            "Voting",
            "Opening a vote or an election is refused if its closing time is sooner than this "
            "after it opens. Voting closes by itself at the closing time.",
        ),
        RuleSpec(
            "vote.allow_early_close",
            "Allow the board to close a formal vote or election before its deadline",
            BOOL,
            "false",
            "Voting",
            "Off: voting runs until the closing time (the board may extend it once, with a "
            "reason, before it passes). On: the board may close early, giving a reason.",
        ),
        RuleSpec(
            "vote.show_turnout_to_owners",
            "Show owners the turnout while voting is open",
            BOOL,
            "false",
            "Voting",
            "Off: owners see only whether their own home has voted and the deadline. On: they "
            "also see how many homes have voted. Choices are never shown while voting is open.",
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
            "Credit card surcharge (%)",
            BASIS_POINTS,
            "300",
            "Online payments",
            "Added to credit card payments only and shown before the owner pays; at most 3%. "
            "Bank and debit card payments never carry a fee.",
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
        # Automatic payments (D47, replacing D46's percent ceiling): a review
        # limit, not a fee.
        RuleSpec(
            "payments.autopay_review_cents",
            "Automatic payments above this wait for the board's review",
            CENTS,
            "50000",
            "Online payments",
            "An automatic payment (monthly or once a year, surcharge included) above this is "
            "not charged until a board member approves it; the payer sees that it is waiting.",
        ),
        # Disbursements (D52): money paid back to owners.
        RuleSpec(
            "disbursement.surplus_by_share_paid",
            "Split a surplus in proportion to what each owner paid that year",
            BOOL,
            "true",
            "Disbursements",
            "On: each owner who paid {terms} in the surplus's fiscal year gets a share in "
            "proportion to what they paid, a seller for their part of the year included. Off: "
            "an equal share for every home with an owner today. With an equal {term} per home "
            "and everyone paid up, both give the same split.",
        ),
        RuleSpec(
            "disbursement.surplus_stays_with_home",
            "A surplus credit stays with the home, not the owner who paid",
            BOOL,
            "true",
            "Disbursements",
            "On: each home's share (from everything paid on it that year, whoever owned it) "
            "is credited to the home's account today, so a buyer receives what a seller "
            "paid towards. Off: each share goes to the ownership account that paid it, a "
            "seller's old account included, from which the board refunds it.",
        ),
        RuleSpec(
            "disbursement.surplus_cash_allowed",
            "Allow paying a surplus share by check",
            BOOL,
            "false",
            "Disbursements",
            "Off: every share is a statement credit that pays the next {terms} until it is "
            "used up. On: the board may pay chosen homes by check (and, when shares follow "
            "the owner, a seller's share is paid by check).",
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
        # D58: self-service account recovery. Board security policy; the
        # floors are enforced here and again where the values are read.
        RuleSpec(
            "recovery.wait_hours",
            "Hours an account recovery waits before the person can finish it",
            INT,
            "48",
            "Account recovery",
            "Owners and residents who lost their phone and recovery codes can recover their "
            "sign-in themselves after this wait; they, the board and every address on file "
            "are told at once and anyone can cancel. At least 48, at most 336 (14 days). A "
            "change applies to recoveries started afterwards.",
            maximum=336,
            minimum=48,
        ),
        RuleSpec(
            "recovery.lock_hours",
            "Hours after a recovery before votes, proxies, contact details and automatic "
            "payments can be changed",
            INT,
            "48",
            "Account recovery",
            "For this long after a self-service recovery the account cannot vote, give a "
            "proxy, change email, phone, mailing address, notice or text choices, set up "
            "automatic payments or ask for account changes. At least 24, at most 336 (14 "
            "days). A change applies to recoveries started afterwards.",
            maximum=336,
            minimum=24,
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

# --- Finance options (D80-D85): each belongs to an optional feature and is
# shown on the setup wizard's "Interest, forecast, legal, projects and
# claims" step (community/setup.py). Interest is simple interest only: the
# portal never compounds it.
INTEREST_GROUP = "Interest on overdue balances"
LEGAL_GROUP = "Collections legal"
FORECAST_GROUP = "Cash-flow forecast"
PROJECTS_GROUP = "Capital projects"
CLAIMS_GROUP = "Insurance claims"
REGISTRY.update(
    {
        spec.key: spec
        for spec in [
            RuleSpec(
                "interest.annual_rate_bp",
                "Yearly interest rate on overdue {terms}",
                BASIS_POINTS,
                None,
                INTEREST_GROUP,
                "Only if the Declaration allows interest. Simple interest, posted monthly at "
                "one twelfth of this rate on what is past due; never compounded. Blank: no "
                "interest is charged even with the feature on.",
                maximum=2400,
            ),
            RuleSpec(
                "interest.max_annual_rate_bp",
                "Highest yearly interest rate the Declaration allows",
                BASIS_POINTS,
                None,
                INTEREST_GROUP,
                "Interest is never posted at a rate above this. Blank: only the rate above.",
                maximum=2400,
            ),
            RuleSpec(
                "interest.grace_days",
                "Days past due before a {term} earns interest",
                DAYS,
                "30",
                INTEREST_GROUP,
            ),
            RuleSpec(
                "interest.min_balance_cents",
                "Smallest past-due amount that earns interest",
                CENTS,
                "0",
                INTEREST_GROUP,
            ),
            RuleSpec(
                "interest.include_fees",
                "Charge interest on late fees and earlier interest too",
                BOOL,
                "false",
                INTEREST_GROUP,
                "Off (the default): interest only on the past-due {term} itself.",
            ),
            RuleSpec(
                "interest.posting_day",
                "Day of the month interest is posted",
                DAYS,
                "15",
                INTEREST_GROUP,
                maximum=28,
                minimum=1,
            ),
            RuleSpec(
                "interest.suspend_on_payment_plan",
                "No interest while an owner keeps to a payment plan",
                BOOL,
                "true",
                INTEREST_GROUP,
            ),
            RuleSpec(
                "legal.costs_chargeable",
                "Collection costs and attorney fees can be charged to the owner",
                BOOL,
                None,
                LEGAL_GROUP,
                "Only where the Declaration says so. Each cost is still charged by a board "
                "member, one at a time.",
            ),
            RuleSpec(
                "cashflow.collection_rate_percent",
                "Share of the {terms} expected to be collected each month (forecast)",
                PERCENT,
                None,
                FORECAST_GROUP,
                "Blank: worked out from the last 12 months' payments.",
            ),
            RuleSpec(
                "cashflow.min_operating_cents",
                "Lowest operating cash the forecast should show before it warns",
                CENTS,
                "0",
                FORECAST_GROUP,
            ),
            RuleSpec(
                "capital.second_approval_cents",
                "Capital project or change order amount needing two board approvals",
                CENTS,
                "500000",
                PROJECTS_GROUP,
            ),
            RuleSpec(
                "insurance.second_approval_cents",
                "Insurance settlement needing two board approvals to accept",
                CENTS,
                "500000",
                CLAIMS_GROUP,
            ),
        ]
    }
)

# Keys no longer in use. A value the board recorded for one stays in the
# database (rules are never deleted, their history is kept) but nothing reads
# it and the settings page does not list it.
RETIRED: dict[str, str] = {
    "disbursement.surplus_equal_shares": (
        "D67: replaced by disbursement.surplus_by_share_paid (off = equal shares) and "
        "disbursement.surplus_cash_allowed."
    ),
    "payments.autopay_ceiling_percent": (
        "D47: replaced by payments.autopay_review_cents (a dollar limit with board review)."
    ),
}


def _parse(spec: RuleSpec, raw: str | None):
    if raw in (None, ""):
        return None
    if spec.kind in (CENTS, DAYS, INT, PERCENT, BASIS_POINTS):
        return int(raw)
    if spec.kind == BOOL:
        return raw.strip().lower() in ("1", "true", "yes")
    return raw


def validate_value(key: str, raw: str) -> str:
    spec = REGISTRY[key]
    raw = (raw or "").strip()
    if raw == "":
        return raw
    if spec.kind in (CENTS, DAYS, INT, PERCENT, BASIS_POINTS):
        # ASCII digits only and a sane size (review D9): str.isdigit() also
        # accepts characters like "²" that int() then refuses on every read.
        if not re.fullmatch(r"[0-9]{1,12}", raw):
            raise ValueError(f"{spec.shown_label}: enter a whole number.")
        if spec.kind == PERCENT and int(raw) > 100:
            raise ValueError(f"{spec.shown_label}: must be 0-100.")
        if spec.maximum is not None and int(raw) > spec.maximum:
            if spec.kind == BASIS_POINTS:
                raise ValueError(f"{spec.shown_label}: at most {bp_display(spec.maximum)}%.")
            raise ValueError(f"{spec.shown_label}: at most {spec.maximum}.")
        if spec.minimum is not None and int(raw) < spec.minimum:
            raise ValueError(f"{spec.shown_label}: at least {spec.minimum}.")
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


def percent_to_bp(text: str) -> int:
    """A percent as typed ("3", "2.5", "2.75%") in whole basis points. Up to
    two decimals; anything else is refused rather than rounded."""
    raw = (text or "").strip().rstrip("%").strip()
    m = re.fullmatch(r"([0-9]{1,6})(?:\.([0-9]{0,2}))?", raw)
    if not m:
        raise ValueError("Enter a percent, e.g. 3 or 2.5.")
    whole, frac = m.group(1), (m.group(2) or "").ljust(2, "0")
    return int(whole) * 100 + int(frac)


def bp_display(bp: int | str | None) -> str:
    """Basis points as a percent without trailing zeros: 300 -> "3", 250 -> "2.5"."""
    if bp in (None, ""):
        return ""
    bp = int(bp)
    whole, frac = divmod(bp, 100)
    return f"{whole}" if frac == 0 else f"{whole}.{frac:02d}".rstrip("0")


def cents_display(cents: int | None) -> str:
    if cents is None:
        return "-"
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"
