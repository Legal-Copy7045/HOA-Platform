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
            "68 Pa.C.S. § 5313",
            "Uniform Planned Community Act: surplus funds",
            "Says what happens to money left over at the end of the year unless the Declaration "
            "provides otherwise.",
            "Relevant if the board refunds or credits a surplus; follow the Declaration.",
        ),
        LawNote(
            "Pennsylvania unclaimed property law",
            "Uncashed checks",
            "Refund or vendor checks that stay uncashed may have to be reported to the state "
            "after a dormancy period.",
            "The treasurer should confirm the dormancy period and keep a list of uncashed checks.",
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
}


def for_step(step_key: str) -> list[LawNote]:
    return STEP_LAW.get(step_key, [])
