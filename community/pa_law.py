"""Pennsylvania guidance for each community setting.

For every governing rule this records where the value normally comes from,
what the Pennsylvania Uniform Planned Community Act (68 Pa.C.S. Chapter 54)
says about it, and a worked example. Where the Act itself supplies a value
when the community's documents are silent, that is a *fallback*: the portal
uses it until the board records a value with its own citation.

This is orientation for the board, not legal advice. The statute is
summarised, not quoted; confirm the current text, and whether the
Declaration or Bylaws set something stricter, with the association's
counsel before relying on any of it.
"""

from __future__ import annotations

from dataclasses import dataclass

ACT = "68 Pa.C.S."

DISCLAIMER = (
    "Summaries of the Pennsylvania Uniform Planned Community Act (68 Pa.C.S. Chapter 54) "
    "for orientation only, not legal advice. Your Declaration and Bylaws may set stricter "
    "rules; confirm with the association's counsel."
)

# Where a value usually comes from.
DECLARATION = "Declaration"
BYLAWS = "Bylaws"
RULES = "Rules and Regulations"
BOARD = "Board resolution"


@dataclass(frozen=True)
class Guide:
    source: str  # where the value usually comes from
    law: str  # what the Act says (or that it is silent)
    example: str  # a worked example, with the kind of citation to record
    fallback: str | None = None  # value used when nothing is recorded
    fallback_citation: str = ""


GROUP_NOTES: dict[str, str] = {
    "Dues": (
        f"The board adopts budgets and collects assessments ({ACT} § 5302(a)), allocated among "
        "lots as the Declaration says (§ 5207, § 5314). Unpaid assessments, late charges, fines "
        "and interest are a lien on the lot (§ 5315)."
    ),
    "Invoices": (
        "The Act does not regulate how the board approves spending; these are internal "
        "controls the board sets by resolution."
    ),
    "Architectural review": (
        "The Act does not set architectural review deadlines. They come from the Declaration "
        "(or design guidelines adopted under it); a deemed-approval rule applies only if the "
        "Declaration provides one."
    ),
    "Violations": (
        f"The association may levy reasonable fines for violations only after notice and an "
        f"opportunity to be heard ({ACT} § 5302(a)(11)). The Act does not set day counts."
    ),
    "Meetings": (
        f"The association must meet at least once a year. Notice goes to every lot owner not "
        f"less than 10 nor more than 60 days before a meeting, stating the time, place and "
        f"business ({ACT} § 5308)."
    ),
    "Records": (
        f"The association keeps financial and other records and makes them reasonably "
        f"available for owners to examine ({ACT} § 5316)."
    ),
    "Resale": (
        f"A selling owner must give the buyer a certificate; the association supplies its part "
        f"within 10 days of the owner's request. The buyer may cancel within 5 days of receiving "
        f"it ({ACT} § 5407)."
    ),
    "Voting": (
        f"Unless the Bylaws say otherwise, a meeting has a quorum if owners holding 20% of the "
        f"votes are present in person or by proxy at the start ({ACT} § 5309). Amending the "
        f"Declaration needs at least 67% of the votes, or more if it says so (§ 5219)."
    ),
    "Online payments": (
        "The Act does not regulate how owners pay. Card networks allow surcharges on credit "
        "cards only, capped at 3% and disclosed before payment; bank and debit card payments "
        "carry no fee. Counsel should confirm the wording before live payments."
    ),
    "Privacy": (
        "The Act does not say how long an association keeps owners' contact details. Records "
        f"the Act requires (financial records, minutes, ballots, {ACT} § 5316) are kept in "
        "full; contact details of people who have left are a board privacy policy."
    ),
    "Notices": (
        "Pennsylvania law generally lets an association give notice by electronic means to an "
        "owner who has agreed to receive it that way. See the notice provisions of the Uniform "
        f"Planned Community Act ({ACT} Chapter 54) and of the Nonprofit Corporation Law "
        "(15 Pa.C.S.); have counsel confirm the consent wording and which notices it covers. "
        "Owners who have not agreed get paper notices."
    ),
    "Disbursements": (
        f"Unless the Declaration says otherwise, any surplus the association has after paying "
        f"common expenses and funding reserves is paid to the owners in proportion to their "
        f"common expense liability or credited to them to reduce their future assessments "
        f'({ACT} § 5313, "Surplus funds"). Counsel should confirm what the Declaration says '
        f"and whether reserve funds count as surplus at all. Many Declarations credit the "
        f"surplus to each owner who paid a share of the common expenses, in proportion to the "
        f"share paid, and apply it to the next assessments until it is used up; that gives no "
        f"basis for paying it in cash. Whether a credit stays with the home or follows the owner "
        f"who paid (a seller) is for the Bylaws or a board resolution, and belongs on the resale "
        f"certificate. Refunding an owner's own credit (for example after a sale) is not a "
        f"surplus distribution."
    ),
    "Server warnings": (
        "Not set by the Act; an operations choice. Keeping the portal running protects the "
        f"association's records, which it must keep ({ACT} § 5316)."
    ),
    "Account recovery": (
        "Not set by the Act; a board security policy. The Act does not regulate how owners "
        "sign in. The waiting period and the lock afterwards protect votes, notices and "
        "payments if someone else gets into an owner's email."
    ),
    "Owner requests": (
        "The Act sets no response times for owner questions. These are service targets the "
        "board chooses."
    ),
}

GUIDES: dict[str, Guide] = {
    # D42: the Act's own word is the fallback; most declarations use it too.
    "charge.term": Guide(
        DECLARATION,
        f"The Act calls the owners' regular share of the common expenses an assessment "
        f"({ACT} § 5302(a)(2), § 5314). The portal uses your covenants' word everywhere.",
        "Assessment: Declaration §6.1 (or Dues, if that is the word your covenants use).",
        fallback="Assessment",
        fallback_citation=f"{ACT} § 5302(a)(2) (the Act's word)",
    ),
    "dues.monthly_cents": Guide(
        BOARD,
        f"Set by the board's adopted budget ({ACT} § 5302(a)), divided among lots by the "
        "Declaration's allocation (§ 5207). No statutory amount.",
        "$45.00 a month: Board resolution adopting the 2027 budget, under Declaration §6.",
    ),
    "dues.due_day": Guide(
        DECLARATION,
        "Not set by the Act.",
        "Due on the 1st: Declaration §6.3 (or a board collection policy).",
    ),
    "dues.auto_post": Guide(
        BOARD,
        "Not covered by the Act; a bookkeeping choice.",
        "Yes: Board resolution 2027-01, monthly assessments posted automatically.",
    ),
    "late_fee.cents": Guide(
        DECLARATION,
        f"The association may charge for late payment of assessments ({ACT} § 5302(a)(11)), "
        "and late charges form part of its lien (§ 5315). The Act sets no amount. The portal "
        "never applies them automatically until counsel has reviewed them.",
        "$25.00 per late month: collection policy adopted by Board resolution under "
        "Declaration §6.5.",
    ),
    "late_fee.auto_apply": Guide(
        BOARD,
        f"The Act allows charges for late payment ({ACT} § 5302(a)(11)) but says nothing about "
        "automation. Have counsel confirm the amount, grace period and notice before switching "
        "it on.",
        "Off until counsel has reviewed: Board resolution 2027-02 (collection policy).",
    ),
    "collections.notice_days": Guide(
        BOARD,
        f"The Act does not set a notice period for collection letters; the association's "
        f"lien for unpaid assessments arises under {ACT} § 5315. Counsel should review the "
        "collection policy and letters.",
        "10 days: collection policy, Board resolution 2027-02.",
    ),
    "collections.courtesy_reminder_day": Guide(
        BOARD,
        "Not set by the Act. A courtesy email is not a statutory notice; the printed "
        "delinquency letters are the association's formal notices.",
        "20: a courtesy email on the 20th to owners with a past-due balance.",
    ),
    "accounting.1099_threshold_cents": Guide(
        BOARD,
        "Federal, not Pennsylvania: the IRS sets when a 1099-NEC or 1099-MISC is required. "
        "For payments made in 2026 and later it is $2,000 (it was $600), adjusted for "
        "inflation from 2027. Check the IRS instructions each January.",
        "$2,000 for 2026 payments.",
    ),
    "late_fee.grace_days": Guide(
        BOARD,
        "Not set by the Act.",
        "15 days after the due date: collection policy, Board resolution 2027-02.",
    ),
    "invoice.dual_approval_cents": Guide(
        BOARD,
        "Not covered by the Act; an internal financial control.",
        "Two approvals at $500 and over: Board resolution 2026-07 (spending controls).",
    ),
    "arc.completeness_days": Guide(
        DECLARATION,
        "Not set by the Act.",
        "10 days to confirm the application is complete: Declaration §9.2 or ARC guidelines.",
    ),
    "arc.review_days": Guide(
        DECLARATION,
        "Not set by the Act.",
        "30 days to decide a complete application: Declaration §9.3.",
    ),
    "arc.deemed_approval": Guide(
        DECLARATION,
        "Applies only if the Declaration provides it; the Act has no deemed-approval rule.",
        "Yes, deemed approved if not decided in 30 days: Declaration §9.3 (only if it says so).",
    ),
    "arc.appeal_days": Guide(
        DECLARATION,
        "Not set by the Act.",
        "30 days to appeal to the board: Declaration §9.6 or ARC guidelines.",
    ),
    "arc.signoff_auto_confirm": Guide(
        BOARD,
        "Not covered by the Act. Neighbor acknowledgements are information for the reviewers; the "
        "decision stays with the committee under the Declaration.",
        "Off: a person checks each acknowledgement read from a scanned form (Board resolution).",
    ),
    "arc.signoff_respond_days": Guide(
        BOARD,
        "Not covered by the Act. Neighbor acknowledgements inform the reviewers; the review "
        "deadlines that bind the committee come from the Declaration.",
        "14 days to respond (Board resolution or ARC guidelines).",
    ),
    "violation.cure_days": Guide(
        RULES,
        "Not set by the Act.",
        "14 days to cure: Rules and Regulations §2 (enforcement policy).",
    ),
    "violation.hearing_notice_days": Guide(
        RULES,
        f"Fines need notice and an opportunity to be heard first ({ACT} § 5302(a)(11)); "
        "the Act does not say how many days' notice.",
        "10 days' written notice of the hearing: enforcement policy, Rules and Regulations §3.",
    ),
    "meeting.owner_notice_days": Guide(
        BYLAWS,
        f"At least 10 and at most 60 days before an association meeting ({ACT} § 5308). "
        "Bylaws may require more notice, never less.",
        "10 days: 68 Pa.C.S. § 5308 (or Bylaws §2.4 if they require more).",
        fallback="10",
        fallback_citation=f"{ACT} § 5308 (statutory minimum)",
    ),
    "records.response_days": Guide(
        BYLAWS,
        f"Records must be reasonably available to owners ({ACT} § 5316); this setting is the "
        "association's own response target.",
        "10 days to make records available: records policy, Board resolution 2027-03.",
    ),
    "resale.fee_cents": Guide(
        BOARD,
        f"The association prepares the certificate information ({ACT} § 5407). Ask counsel "
        "whether any statutory limit applies to the fee before setting it.",
        "Fee set by Board resolution 2027-04 (after counsel review).",
    ),
    "resale.deadline_days": Guide(
        DECLARATION,
        f"Within 10 days of the owner's request ({ACT} § 5407). The buyer may cancel within "
        "5 days of receiving the certificate.",
        "10 days: 68 Pa.C.S. § 5407.",
        fallback="10",
        fallback_citation=f"{ACT} § 5407",
    ),
    "vote.quorum_percent": Guide(
        BYLAWS,
        f"20% of the votes, present in person or by proxy, unless the Bylaws set another "
        f"quorum ({ACT} § 5309).",
        "20%: 68 Pa.C.S. § 5309 (or Bylaws §2.6 if different).",
        fallback="20",
        fallback_citation=f"{ACT} § 5309 (unless the Bylaws provide otherwise)",
    ),
    "vote.pass_percent": Guide(
        DECLARATION,
        f"Depends on the question. Amending the Declaration needs at least 67% of the votes in "
        f"the association ({ACT} § 5219); other matters follow the Declaration and Bylaws.",
        "51% of votes cast for ordinary business (Bylaws §2.7); 67% of all votes for a "
        "Declaration amendment (§ 5219).",
    ),
}

# D61: the voting window. The Act sets no voting period for ballots outside a
# meeting, so no statutory fallback: the default is a board choice.
GUIDES["vote.min_open_days"] = Guide(
    BYLAWS,
    "The Act does not set how long owners must have to vote when ballots are cast outside "
    f"a meeting; the meeting notice period ({ACT} § 5308) applies to votes taken at a "
    "meeting. The Bylaws (or the Nonprofit Corporation Law, 15 Pa.C.S., for votes by "
    "ballot) may set a period: have counsel confirm it.",
    "7 days: Bylaws voting article, or Board resolution adopting electronic voting.",
)
GUIDES["vote.allow_early_close"] = Guide(
    BYLAWS,
    "Not covered by the Act. Owners rely on the deadline they were given; closing early "
    "can cut off owners who planned to vote later, so most associations do not allow it "
    "unless the Bylaws or the ballot itself says voting ends once a set number vote.",
    "Off: voting runs to the announced deadline (Bylaws voting article).",
)
GUIDES["vote.show_turnout_to_owners"] = Guide(
    BOARD,
    f"Not covered by the Act. Who voted is part of the association's records ({ACT} "
    "§ 5316) but how a home voted on a secret ballot never is. Showing the running turnout "
    "can encourage voting; some boards prefer to report it only with the result.",
    "Off: turnout is reported with the result (Board resolution on elections).",
)

GUIDES["ledger.assessments_before_fees"] = Guide(
    DECLARATION,
    "The Act does not say in what order an owner's payments pay their charges. Assessments, "
    f"late charges, fines and interest are all part of the association's lien ({ACT} § 5302(a)(11) "
    "and § 5315), so the Declaration or a board collection policy usually sets the order. "
    "Oldest first is the common default; some policies pay assessments before fees.",
    "Off (oldest first): Board collection policy, resolution 2027-03. On if the Declaration "
    "says payments apply to assessments first (e.g. Declaration §7.6).",
)
GUIDES["payments.credit_card_surcharge_bp"] = Guide(
    BOARD,
    "Not covered by the Act. Card-network rules allow a surcharge on credit cards only (never "
    "debit or prepaid), no more than the cost of acceptance and at most 3%, disclosed before "
    "payment, after registering with Visa and Mastercard 30 days ahead. Counsel should confirm "
    "the disclosure wording.",
    "3% (not more than the processor's credit card cost): Board resolution 2027-09, "
    "after counsel review.",
)

GUIDES["notices.electronic_consent_required"] = Guide(
    BYLAWS,
    "Pennsylvania law generally permits notice by electronic means to an owner who has "
    f"consented to it (Uniform Planned Community Act, {ACT} Chapter 54, and the Nonprofit "
    "Corporation Law, 15 Pa.C.S.: have counsel confirm the provisions and the wording). "
    "Without consent, notices go by post.",
    "On: owners choose email or post at sign-up; Bylaws notice article, as confirmed by counsel.",
)

GUIDES["payments.autopay_review_cents"] = Guide(
    BOARD,
    "Not covered by the Act. A safeguard of the portal, not a fee: an owner authorizes "
    "automatic payment of the assessment (monthly, or twelve months at once), and a charge "
    "above this amount waits until a board member who does not pay for that home approves "
    "it. NACHA rules (bank debits) and card-network rules expect a recurring charge to stay "
    "within what the payer authorized.",
    "$500.00: Board resolution adopting automatic payments.",
)

GUIDES["retention.former_owner_contact_days"] = Guide(
    BOARD,
    "Not covered by the Act. Only contact details are cleared: ledger, ARC, violation, vote "
    f"and audit records stay, as the association must keep its records ({ACT} § 5316).",
    "730 days (two years after leaving): Board privacy policy, resolution 2027-11.",
)

GUIDES["disbursement.surplus_by_share_paid"] = Guide(
    DECLARATION,
    f"Unless the Declaration provides otherwise, surplus funds are paid to the owners in "
    f"proportion to their common expense liability, or credited to them to reduce future "
    f"assessments ({ACT} § 5313). Many Declarations have their own surplus section that "
    f"credits the excess to each owner who paid a share of the common expenses, in proportion "
    f"to what they paid; a surplus is what is left after the actual common expenses and the "
    f"board's reserves. Counsel should confirm the Declaration's allocation and whether reserve "
    f"funds may be treated as surplus.",
    "On: Declaration §8.10 (surplus credited to each owner who paid a share of the common "
    "expenses, in proportion to the share paid); §8.4 and §8.14 (equal assessment per lot).",
)

GUIDES["disbursement.surplus_stays_with_home"] = Guide(
    DECLARATION,
    f"The Act does not say whether a surplus credit follows the home or the owner who paid "
    f"({ACT} § 5313 only says it is paid or credited to the owners). A Declaration that "
    f"applies the credit to the next assessments points to the home; the Bylaws or a board "
    f"resolution (and the resale certificate, {ACT} § 5407) should say which, so buyers and "
    f"sellers know at settlement.",
    "On: Declaration §8.10 (credit applied to the next monthly assessments); board resolution "
    "setting the procedure under §8.13.",
)

GUIDES["disbursement.surplus_cash_allowed"] = Guide(
    DECLARATION,
    f"The Act allows either a payment or a credit against future assessments unless the "
    f"Declaration provides otherwise ({ACT} § 5313). If the Declaration says the surplus "
    f'"shall be credited" and applied to the next assessments, a cash payment has no basis in '
    f"it: leave this off unless counsel advises otherwise, and record the reason in the board "
    f"resolution.",
    "Off: Declaration §8.10 (credited and applied to the next monthly assessments until "
    "exhausted).",
)

GUIDES["disbursement.second_approval_cents"] = Guide(
    BOARD,
    "Not covered by the Act; an internal financial control like the invoice approval rule.",
    "Two approvals at $500 and over: Board resolution 2026-07 (spending controls).",
)

GUIDES["monitoring.disk_full_warn_days"] = Guide(
    BOARD,
    "Not covered by the Act. A full disk stops the portal and its backups; an early warning "
    f"keeps the association's records ({ACT} § 5316) safe and available.",
    "60 days: Board operations policy.",
)

GUIDES["recovery.wait_hours"] = Guide(
    BOARD,
    "Not set by the Act; a board security policy. While the request waits, the person, every "
    "email address on file, their verified text number and the board are told, and any of "
    "them can cancel it. The portal never allows less than 48 hours.",
    "48 hours: Board security policy (sign-in recovery), resolution 2026-12.",
)

GUIDES["recovery.lock_hours"] = Guide(
    BOARD,
    "Not set by the Act; a board security policy. Votes and ballots stay the owner's own "
    f"(the association keeps ballots and records, {ACT} § 5316), so a recently recovered "
    "sign-in cannot cast or change them, give a proxy, or change where notices go until the "
    "lock ends. The portal never allows less than 24 hours.",
    "48 hours: Board security policy (sign-in recovery), resolution 2026-12.",
)

for _key in ("general", "billing", "maintenance", "common_area"):
    GUIDES[f"request.sla_days.{_key}"] = Guide(
        BOARD,
        "Not covered by the Act; a service target.",
        "5 business days for questions, 3 for maintenance: Board resolution 2027-05.",
    )


def guide(key: str) -> Guide | None:
    return GUIDES.get(key)
