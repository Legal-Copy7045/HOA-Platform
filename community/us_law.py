"""Federal law, and Pennsylvania statutes outside the Uniform Planned
Community Act, that bear on the community setup wizard's decisions (D51).

The Act itself is summarised per rule and per group in community/pa_law.py;
this module adds what sits around it: the federal statutes and regulations,
industry rules and other PA statutes the board should know about when it
makes a set-up choice. Entries are data keyed by wizard step
(``community.setup.STEPS``), shown in each step's "What the law says" panel.

Summaries for orientation only, not legal advice. Each names its source;
where a section number was not certain it describes the provision instead.
Counsel should confirm the current text and how it applies to the
Declaration before relying on it.
"""

from __future__ import annotations

from dataclasses import dataclass

CAVEAT = "Have the association's counsel confirm how this applies to your community."
FOOTER = (
    "General information, not legal advice. Your association's attorney should confirm how "
    "these apply to your Declaration."
)


@dataclass(frozen=True)
class LawNote:
    citation: str  # where it comes from
    title: str  # the law's short name
    summary: str  # what it says, in plain words
    meaning: str  # what it means for the choice on this page
    caveat: str = CAVEAT


FHA = "42 U.S.C. § 3604(f)(3)"

STEP_LAW: dict[str, list[LawNote]] = {
    "charge": [
        LawNote(
            "68 Pa.C.S. § 5302(a), § 5314",
            "Uniform Planned Community Act: assessments",
            "The board adopts the budget and levies assessments, divided among homes as the "
            "Declaration says.",
            "The amount and due day you record here should match the adopted budget and the "
            "Declaration's allocation; cite the resolution or section.",
        ),
    ],
    "late_fees": [
        LawNote(
            "68 Pa.C.S. § 5302(a)(11), § 5315",
            "Uniform Planned Community Act: late charges and the lien",
            "The association may charge for late payment, and unpaid assessments, late charges "
            "and fines are a lien on the home.",
            "Set a late fee only as the Declaration or a board collection policy allows, and "
            "record that citation.",
        ),
        LawNote(
            "15 U.S.C. § 1692 et seq.",
            "Fair Debt Collection Practices Act",
            "Applies to third parties collecting for the association, such as a management "
            "company or attorney; the association collecting its own assessments is generally "
            'not a "debt collector".',
            "If the manager or counsel sends collection letters, their wording and timing must "
            "follow the Act.",
        ),
        LawNote(
            "50 U.S.C. § 3901 et seq.",
            "Servicemembers Civil Relief Act",
            "Gives servicemembers protections before a default judgment and against some "
            "enforcement steps.",
            "Before taking a delinquent account to court, check whether the owner is on active "
            "duty.",
        ),
        LawNote(
            "Internal Revenue Code, Form 1099 reporting",
            "1099s for vendors",
            "Payments to some vendors over the year must be reported on a Form 1099; the IRS "
            "sets and changes the threshold.",
            "Check the current IRS threshold and set the 1099 rule to match; the portal only "
            "flags vendors, the treasurer decides.",
        ),
    ],
    "surplus": [
        LawNote(
            "68 Pa.C.S. § 5313",
            "Uniform Planned Community Act: surplus funds",
            "Says what happens to money left over at the end of the year unless the Declaration "
            "provides otherwise: paid to owners or credited against their future assessments.",
            "Follow the Declaration's surplus section; if it says the surplus is credited, leave "
            "check payments off and record the section as the citation.",
        ),
        LawNote(
            "68 Pa.C.S. § 5407",
            "Uniform Planned Community Act: resale certificates",
            "A seller gives the buyer a certificate of the association's finances and the "
            "home's account.",
            "Whether a surplus credit stays with the home or follows the seller affects what "
            "the certificate shows; the board's choice should be in a resolution or the Bylaws.",
        ),
        LawNote(
            "Pennsylvania unclaimed property law",
            "Uncashed checks",
            "Refund checks that stay uncashed may have to be reported to the state after a "
            "dormancy period.",
            "The treasurer should confirm the dormancy period and keep a list of uncashed checks.",
        ),
    ],
    "books": [
        LawNote(
            "26 U.S.C. § 528; IRS Form 1120-H",
            "Homeowners associations' federal income tax",
            "An association that meets the tests (at least 60% of gross income from members' "
            "assessments and fees, at least 90% of spending on the association's property) may "
            "file Form 1120-H; other income such as bank interest is taxed after a $100 "
            "deduction. Otherwise Form 1120 applies.",
            "The year-end tax worksheet separates exempt-function income from other income for "
            "the preparer, who confirms the form.",
        ),
        LawNote(
            "68 Pa.C.S. § 5316, § 5407",
            "Uniform Planned Community Act: records and resale certificates",
            "The association keeps financial records detailed enough for resale certificates "
            "and makes them available to owners.",
            "Closed months and year-end packages keep the books as they were; corrections are "
            "posted in the open month with a reference.",
        ),
        LawNote(
            "IRC § 6041, § 6071(c); IRS Instructions for Forms 1099-MISC and 1099-NEC; IRIS",
            "1099s for vendors",
            "Payments to some vendors over the year must be reported on Form 1099-NEC or "
            "1099-MISC by January 31, to the vendor and to the IRS (the Information Returns "
            "Intake System, IRIS, takes a CSV upload).",
            "The Tax worksheet gives the totals, an IRIS upload file and recipient copies "
            "without taxpayer numbers (the portal keeps none: add them in IRIS from the W-9). "
            "The year close stops for vendors over the threshold without a W-9.",
        ),
        LawNote(
            "15 Pa.C.S. § 146",
            "Pennsylvania annual report for corporations",
            "Nonprofit corporations file an annual report with the PA Department of State.",
            "It is put on Recurring deadlines with a reminder; confirm the due date and any "
            "other state filing with your preparer.",
        ),
    ],
    "payments": [
        LawNote(
            "12 C.F.R. § 1005.10(b)",
            "Electronic Fund Transfer Act and Regulation E",
            "Preauthorized electronic transfers from a consumer's account need the consumer's "
            "written authorization, and the consumer gets a copy.",
            "Automatic payments are authorized on Stripe's page, which collects the bank "
            "authorization; turning autopay on does not change that.",
        ),
        LawNote(
            "Nacha Operating Rules (industry rules, not law)",
            "Bank debits (ACH)",
            "Set how bank debits are authorized, retried and returned.",
            "A returned or revoked bank debit pauses automatic payments rather than retrying.",
        ),
        LawNote(
            "Card network surcharge rules (industry rules, not law)",
            "Credit card surcharge",
            "Surcharges are allowed on credit cards only, capped, and disclosed before payment; "
            "Pennsylvania does not prohibit surcharging.",
            "Keep the surcharge at or under 3% and credit cards only; your processor and counsel "
            "should confirm.",
        ),
    ],
    "notices": [
        LawNote(
            "15 U.S.C. § 7001(c)",
            "E-SIGN Act: consumer consent to electronic records",
            "Before records a law requires in writing go electronically, the person must get a "
            "disclosure, consent in a way that shows they can open them, and may withdraw "
            "consent and ask for paper.",
            "That is why official notices go by email only to owners who agreed; keep asking "
            "for consent unless counsel says otherwise.",
        ),
        LawNote(
            "73 P.S. § 2260.101 et seq.",
            "Pennsylvania Uniform Electronic Transactions Act",
            "Gives electronic records and signatures legal effect when the parties agreed to "
            "deal electronically.",
            "Supports email notices to owners who agreed to them.",
        ),
        LawNote(
            "Uniform Planned Community Act and Nonprofit Corporation Law notice provisions",
            "Notices to owners",
            "Set which notices owners must get and how; they generally allow electronic notice "
            "to an owner who has agreed to it.",
            "Owners who have not agreed get paper notices.",
        ),
        LawNote(
            "47 U.S.C. § 227",
            "Telephone Consumer Protection Act",
            "Automated texts need the recipient's prior express consent, and a STOP reply must "
            "be honoured.",
            "Text alerts are opt-in with a texted code, and STOP turns them off.",
        ),
    ],
    "arc": [
        LawNote(
            FHA,
            "Fair Housing Act: reasonable modifications and accommodations",
            "People with disabilities may make reasonable modifications and must get reasonable "
            "accommodations in rules, e.g. a ramp, an assistance animal or a parking space.",
            "Review deadlines and required submittals must leave room to grant such requests.",
        ),
        LawNote(
            "Pub. L. 109-243",
            "Freedom to Display the American Flag Act of 2005",
            "An association may not stop an owner displaying the U.S. flag on their own "
            "property, subject to reasonable time, place and manner rules.",
            "Do not require approval to fly the flag beyond reasonable rules.",
        ),
        LawNote(
            "47 C.F.R. § 1.4000",
            "FCC Over-the-Air Reception Devices rule",
            "Limits restrictions on small satellite dishes and antennas on property the owner "
            "controls.",
            "Do not make dish or antenna installs wait for approval in ways the rule forbids.",
        ),
    ],
    "violations": [
        LawNote(
            "68 Pa.C.S. § 5302(a)(11)",
            "Uniform Planned Community Act: notice and hearing",
            "The association may levy reasonable fines only after notice and an opportunity to "
            "be heard.",
            "The cure period and hearing notice you set here are that notice.",
        ),
        LawNote(
            FHA,
            "Fair Housing Act: accommodation requests",
            "A violation tied to a disability may call for a reasonable accommodation instead "
            "of a fine.",
            "Treat a request for an accommodation as such before pressing the violation.",
        ),
    ],
    "common_areas": [
        LawNote(
            FHA,
            "Fair Housing Act",
            "Residents must have equal access to common facilities, with reasonable "
            "accommodations for disabilities.",
            "Booking rules must apply equally and allow accommodations.",
        ),
        LawNote(
            "Americans with Disabilities Act",
            "ADA and private facilities",
            "The ADA generally does not cover private community facilities that are not open "
            "to the public.",
            "If areas are rented to outsiders, counsel should confirm whether that changes.",
        ),
    ],
    "governance": [
        LawNote(
            "Uniform Planned Community Act and Nonprofit Corporation Law",
            "Meetings, quorum and voting",
            "Set notice of meetings, quorum and voting unless the Bylaws say otherwise (see "
            "the Pennsylvania notes above for the Act's numbers).",
            "Record the Bylaws' own numbers with their citation where they differ.",
        ),
    ],
    "privacy": [
        LawNote(
            "73 P.S. § 2301 et seq.",
            "Pennsylvania Breach of Personal Information Notification Act",
            "Requires notifying people when certain personal information is exposed in a breach.",
            "Keeping fewer contact details for fewer years reduces what a breach could expose.",
        ),
    ],
    # D72: renewals and records retention.
    "records": [
        LawNote(
            "26 U.S.C. § 6501; IRS Publication 583",
            "Federal tax records",
            "The IRS can generally assess more tax for 3 years after a return is filed (6 years "
            "if income was substantially understated, without limit for a false return); "
            "records supporting the association's returns (Form 1120-H or 1120) should be kept "
            "at least that long.",
            "Seven years for financial records covers these periods with room to spare.",
        ),
        LawNote(
            "42 Pa.C.S. § 5525",
            "Pennsylvania limitation period for contracts",
            "A claim on a written contract must generally be brought within 4 years.",
            "Keeping contracts 7 years after they end covers that period and late-found problems.",
        ),
        LawNote(
            "Federal Rules of Civil Procedure 37(e); Pennsylvania case law on spoliation",
            "Legal holds",
            "Once litigation or a claim is reasonably expected, records that may be relevant "
            "must be preserved; destroying them can lead to sanctions.",
            "Place a legal hold on the documents (or the whole category) as soon as a dispute "
            "or claim is expected; nothing under a hold can be disposed of.",
        ),
    ],
    "server": [
        LawNote(
            "73 P.S. § 2301 et seq.",
            "Pennsylvania Breach of Personal Information Notification Act",
            "Requires notifying people when certain personal information is exposed in a breach.",
            "Encrypted backups and a locked off-site copy are part of keeping owners' data safe.",
        ),
    ],
}

# Steps whose choices no particular statute governs: said explicitly.
NO_SPECIFIC_LAW: dict[str, str] = {
    "community": "No specific law governs these details; they print on letters and statements.",
    "branding": "No specific law governs the sign-in picture.",
    "requests": "No specific law sets response times for owner questions; these are board "
    "service targets.",
    "features": "No specific law decides which optional features are on; each feature's own "
    "step notes the law that applies to it.",
    "people": "Board seats, offices and terms come from the Bylaws and the Nonprofit "
    "Corporation Law; this step only shows who holds them.",
    "review": "Each step's law notes apply to the decisions listed here.",
    "monitoring": "No specific law sets how early the server's disk warning comes; it is an "
    "operations choice.",
}


def for_step(step_key: str) -> list[LawNote]:
    return STEP_LAW.get(step_key, [])
