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
    "Owner requests": (
        "The Act sets no response times for owner questions. These are service targets the "
        "board chooses."
    ),
}

GUIDES: dict[str, Guide] = {
    # D40: the Act's own word is the fallback; most declarations use it too.
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
        "Not covered by the Act. Neighbor sign-offs are information for the reviewers; the "
        "decision stays with the committee under the Declaration.",
        "Off: a person confirms each signature read from a scanned form (Board resolution).",
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
    "300 (3%, not more than the processor's credit card cost): Board resolution 2027-09, "
    "after counsel review.",
)

GUIDES["retention.former_owner_contact_days"] = Guide(
    BOARD,
    "Not covered by the Act. Only contact details are cleared: ledger, ARC, violation, vote "
    f"and audit records stay, as the association must keep its records ({ACT} § 5316).",
    "730 days (two years after leaving): Board privacy policy, resolution 2027-11.",
)

for _key in ("general", "billing", "maintenance", "common_area"):
    GUIDES[f"request.sla_days.{_key}"] = Guide(
        BOARD,
        "Not covered by the Act; a service target.",
        "5 business days for questions, 3 for maintenance: Board resolution 2027-05.",
    )


def guide(key: str) -> Guide | None:
    return GUIDES.get(key)
