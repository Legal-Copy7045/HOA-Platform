"""Owner help (D73): "How it works" pages that read today's rules, the
board's own words on each, the guide to the menu, and where a new login goes
after the sign-up steps.

The association is self-managed, so every "how do I..." lands on a volunteer
board member. These pages answer the common ones in plain words, with the
numbers read from the governing rules (community.rules) every time a page is
drawn, so they are never out of date, and each number carries its citation.

Nothing here decides access: each topic names the permissions that may open
it (any one of them), and the views and the search provider
(community.help_search) check them. The board's text is stored append-only
(``HelpText``) and rendered by ``render_text``: escaped first, then a small
markdown subset (paragraphs, "- " lists, "## " headings, **bold**, and links
to https:// or portal paths). Raw HTML is never passed through.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.db import transaction
from django.urls import NoReverseMatch, reverse
from django.utils.html import escape
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe

from community.terms import term_text
from core import policy

FEATURE = "owner_help"
MAX_TEXT = 5000


class HelpError(ValueError):
    pass


@dataclass
class Section:
    heading: str
    paras: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Topic:
    slug: str
    title: str  # the page's <h1> (docs/glossary.md)
    summary: str  # one line on the Help page and in search
    permissions: tuple[str, ...]  # any one opens the page
    # The owner's section page for this topic: (label, url name with query).
    section: tuple[str, str]
    # The board's section page, for staff without a home.
    staff_section: tuple[str, str] | None = None
    # An optional feature that must be on (else the topic is hidden).
    feature: str = ""
    # The kind of owner request "Ask the board" preselects from this page.
    ask_about: str = "general"


TOPICS: list[Topic] = [
    Topic(
        "paying",
        "How paying works",
        "The monthly {term}, when it is due, late fees, ways to pay and your statements.",
        ("ledger.view_own", "finance.view"),
        ("My account", "my_account"),
        ("Owner accounts", "ledger_home"),
        ask_about="billing",
    ),
    Topic(
        "architectural-review",
        "How architectural review works",
        "Asking to change the outside of your home: the steps, what you do at each and the "
        "deadlines.",
        ("arc.submit", "lot.view_own", "arc.view_all"),
        ("My home improvement requests (ARC)", "arc_list?mine=1"),
        ("Architectural review", "arc_list"),
    ),
    Topic(
        "violations",
        "How violation notices work",
        "What a notice about your home means, the time to fix it, responding and hearings.",
        ("lot.view_own", "violation.view_all"),
        ("My violations", "violations?mine=1"),
        ("Violations", "violations"),
        feature="violations",
    ),
    Topic(
        "voting-and-meetings",
        "How voting and meetings work",
        "Meeting notices, formal votes and elections, proxies, and surveys.",
        ("vote.view", "meeting.view"),
        ("Voting", "voting"),
    ),
    Topic(
        "requests",
        "How requests work",
        "Asking the board a question or reporting a problem, and asking to see the "
        "association's records.",
        ("request.view_own", "request.manage", "records_request.submit"),
        ("My requests", "requests?mine=1"),
        ("Owner requests", "requests"),
    ),
]
BY_SLUG = {t.slug: t for t in TOPICS}


# --- Who may open what -------------------------------------------------------------------


def enabled(request=None) -> bool:
    from core import features

    return features.enabled(FEATURE, request)


def may_open(user, topic: Topic, request=None) -> bool:
    from core import features

    if not policy.can(user, "help.view"):
        return False
    if topic.feature and not features.enabled(topic.feature, request):
        return False
    return any(policy.can(user, p) for p in topic.permissions)


def topics_for(user, request=None) -> list[Topic]:
    return [t for t in TOPICS if may_open(user, t, request)]


def _url(name: str) -> str | None:
    base, _, query = name.partition("?")
    try:
        return reverse(base) + (f"?{query}" if query else "")
    except NoReverseMatch:
        return None


_STAFF = frozenset({policy.BOARD, policy.MANAGER, policy.AUDITOR, policy.ARC_REVIEWER})


def section_link(user, topic: Topic, request=None) -> tuple[str, str] | None:
    """(label, url) of the section page this topic explains, for this person:
    the owner's page when they have a home, else the board's."""
    from core import features

    groups = policy.roles_for(user).groups
    choices = []
    if groups & policy.RESIDENTS:
        choices.append(topic.section)
    if topic.staff_section and groups & _STAFF:
        choices.append(topic.staff_section)
    from core import navigation

    # The menu's own permission for that page: an authorized or view-only
    # contact is a resident but may not open "My home improvement requests"
    # (arc.submit is the owner's), so they are not sent there.
    menu_perms = {name: perm for _t, items in navigation.SECTIONS for _l, name, _i, perm in items}
    for label, name in choices:
        if not features.url_name_enabled(name.partition("?")[0], request):
            continue
        perm = menu_perms.get(name)
        if perm not in (None, navigation.HUB) and not navigation.allowed(groups, perm):
            continue
        url = _url(name)
        if url:
            return label, url
    return None


def ask_url(topic: Topic | None = None) -> str:
    url = reverse("ask_board")
    if topic and topic.ask_about != "general":
        url += f"?about={topic.ask_about}"
    return url


# --- Reading the rules --------------------------------------------------------------------


def _cite(citation: str) -> str:
    return f" ({citation})" if citation else ""


def _rule(key: str):
    from community.rules import rule_with_citation

    value, citation = rule_with_citation(key)
    return value, _cite(citation)


def _money(cents) -> str:
    from core.money import format_cents

    return format_cents(cents)


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


NOT_SET = "The board has not recorded this yet; ask the board if you need it."


def _paying(request=None) -> list[Section]:
    out = []
    amount, amount_cite = _rule("dues.monthly_cents")
    due_day, due_cite = _rule("dues.due_day")
    first = Section(term_text("Your {term}"))
    if amount:
        first.paras.append(
            term_text("The {term} is ") + f"{_money(amount)} a month for every home{amount_cite}."
        )
    else:
        first.paras.append(term_text("The monthly {term}: ") + NOT_SET)
    if due_day:
        first.paras.append(f"It is due on the {_ordinal(int(due_day))} of each month{due_cite}.")
    first.paras.append(
        term_text(
            "Each month's {term} appears on My account as its own charge, named "
            "\N{LEFT DOUBLE QUOTATION MARK}{Term} \N{EN DASH} <month>"
            "\N{RIGHT DOUBLE QUOTATION MARK}, with what is still unpaid on it."
        )
    )
    out.append(first)

    late = Section("If a payment is late")
    grace, grace_cite = _rule("late_fee.grace_days")
    fee, fee_cite = _rule("late_fee.cents")
    auto, _auto_cite = _rule("late_fee.auto_apply")
    notice, notice_cite = _rule("collections.notice_days")
    if grace is not None:
        late.paras.append(
            f"A payment is late once it is more than {_days(grace)} past the due date{grace_cite}."
        )
    if fee:
        how = (
            "It is added automatically after that grace period."
            if auto
            else "The board reviews the list of late accounts before adding it."
        )
        late.paras.append(f"A late fee of {_money(fee)} is charged for each late month{fee_cite}.")
        late.paras.append(how)
    else:
        late.paras.append("Late fee: " + NOT_SET)
    if notice:
        late.paras.append(
            "If an account stays behind, the board sends a written notice giving "
            f"{_days(notice)} to bring it up to date{notice_cite}. Ask the board early if "
            "you need a payment plan: it is easier for everyone."
        )
    order, order_cite = _rule("ledger.assessments_before_fees")
    late.paras.append(
        term_text("Payments pay {terms} first (oldest first), then late fees and other charges")
        + f"{order_cite}."
        if order
        else "Each payment pays the oldest open charge first."
    )
    out.append(late)

    ways = Section("Ways to pay")
    from payments.services import online_payments_enabled, surcharge_bp

    if online_payments_enabled(request):
        from community.rules import bp_display

        bp = surcharge_bp()
        card = (
            f"or by credit card with a {bp_display(bp)}% surcharge, shown before you pay"
            if bp
            else "or by credit card"
        )
        ways.paras.append(
            "Pay online from My account with Pay online: by bank account or debit card with no "
            f"fee, {card}. Payment happens on Stripe's secure page; the portal never sees your "
            "card or bank numbers."
        )
    ways.paras.append(
        "You can also pay by check: your statement says who to make it out to and where to send it."
    )
    from payments.autopay import autopay_enabled, review_cents

    if autopay_enabled(request):
        ways.items.append(
            term_text(
                "Automatic payments: on My account, choose Pay {terms} automatically to have "
                "each month's {term} paid on its due day, or the next twelve months once a "
                "year. Late fees and other charges are never paid automatically. You can turn "
                "it off at any time, and it ends when the home is sold."
            )
        )
        ways.items.append(
            f"An automatic payment above {_money(review_cents())} waits until a board member "
            "who does not pay for your home approves it."
        )
    out.append(ways)

    statements = Section("Statements")
    statements.paras.append(
        term_text(
            "When the month's {term} is posted, owners and the contacts who can see the "
            "account get a statement email with the amount due and the statement as a PDF. "
            "Untick it on My profile if you would rather not. My account always has your "
            "statement and every charge and payment."
        )
    )
    out.append(statements)
    return out


def _arc(request=None) -> list[Section]:
    from arc.models import ArcState

    labels = dict(ArcState.choices)
    complete, complete_cite = _rule("arc.completeness_days")
    review, review_cite = _rule("arc.review_days")
    deemed, deemed_cite = _rule("arc.deemed_approval")
    appeal, appeal_cite = _rule("arc.appeal_days")
    neighbors, neighbors_cite = _rule("arc.signoff_respond_days")
    intro = Section("When you need approval")
    intro.paras.append(
        "Most changes to the outside of a home (fences, sheds, decks, paint colours, solar "
        "panels, trees and the like) need the Architectural Review Committee's approval "
        "before work starts. Each kind of project lists what to include: plans, photos, "
        "colours or a contractor's quote."
    )
    steps = Section("The steps, and what you do at each")
    steps.items.append(
        f"{labels['draft']}: describe the project on My home improvement requests (ARC) and "
        "add plans or photos. Nothing is sent until you send it."
    )
    neighbor_line = (
        f"{labels['neighbors']}: if the project needs them, your neighbors are asked to "
        "confirm they have seen it"
    )
    if neighbors:
        neighbor_line += f", within {_days(neighbors)}{neighbors_cite}"
    steps.items.append(
        neighbor_line + ". An acknowledgement never decides the request; the review starts by "
        "itself once none are waiting."
    )
    steps.items.append(
        "Checked for completeness: the board checks the request has everything it needs"
        + (f", within {_days(complete)}{complete_cite}" if complete else "")
        + ". If something is missing you are asked for it, and the review clock starts once "
        "it is complete."
    )
    review_line = f"{labels['in_review']}: the committee decides"
    if review:
        review_line += f" within {_days(review)} of the request being complete{review_cite}"
    review_line += "."
    if deemed:
        review_line += (
            " If no decision is made in that time, the request is approved automatically"
            f"{deemed_cite}."
        )
    steps.items.append(review_line)
    steps.items.append(
        f"Decision: {labels['approved']}, {labels['approved_with_conditions'].lower()} or "
        f"{labels['denied'].lower()}, with a letter that cites the governing section. Follow "
        "any conditions when you do the work."
    )
    if appeal:
        steps.items.append(
            f"Appeal: if you disagree, you may appeal within {_days(appeal)} of the decision"
            f"{appeal_cite}. The board decides the appeal."
        )
    else:
        steps.items.append("Appeal: if you disagree, ask the board how to appeal.")
    steps.items.append(f"{labels['completed']}: tell the board when the work is finished.")
    tips = Section("Good to know")
    tips.paras.append(
        "Reviewers who live next door, or whose home is affected, take no part in your "
        "review. Every version of what you send is kept."
    )
    if not (complete and review):
        tips.paras.append("Review deadlines: " + NOT_SET)
    return [intro, steps, tips]


def _violations(request=None) -> list[Section]:
    cure, cure_cite = _rule("violation.cure_days")
    hearing, hearing_cite = _rule("violation.hearing_notice_days")
    notice = Section("The notice")
    notice.paras.append(
        "If something at a home seems to break the covenants or rules, the board sends the "
        "owners a notice saying what it is and which rule it is under. It appears on My "
        "violations and, if you chose it, by email or post."
    )
    fix = Section("Fixing it")
    if cure:
        fix.paras.append(
            f"You normally have {_days(cure)} to fix it{cure_cite}; the notice gives the "
            "date (“Fix by”)."
        )
    else:
        fix.paras.append("Time to fix it: the notice gives the date. " + NOT_SET)
    fix.paras.append(
        "Once it is fixed, say so on the notice's page (a photo helps). The board marks it cured."
    )
    respond = Section("Responding and hearings")
    respond.paras.append(
        "If you think the notice is wrong, or need more time, reply on the notice's page. "
        "You may ask for a hearing before the board"
        + (f"; you get at least {_days(hearing)} notice of it{hearing_cite}" if hearing else "")
        + "."
    )
    respond.paras.append(
        "After hearing from you the board decides: cured, dismissed or upheld. You are told "
        "the decision in writing."
    )
    fines = Section("Fines")
    fines.paras.append(
        "The portal does not charge fines. Any fine follows the governing documents and only "
        "after you have had the chance to be heard, and the board tells you in writing."
    )
    return [notice, fix, respond, fines]


def _voting(request=None) -> list[Section]:
    from core import features

    out = []
    notice, notice_cite = _rule("meeting.owner_notice_days")
    meetings = Section("Meetings")
    meetings.paras.append(
        "Meetings are on the Calendar with the place, or a link to join online, and the "
        "agenda."
        + (
            f" Owners get at least {_days(notice)} notice of an owners' meeting{notice_cite}."
            if notice
            else ""
        )
        + " Minutes appear on the meeting's page once the board approves them."
    )
    out.append(meetings)
    if features.enabled("votes", request) or features.enabled("elections", request):
        quorum, quorum_cite = _rule("vote.quorum_percent")
        passing, pass_cite = _rule("vote.pass_percent")
        days, days_cite = _rule("vote.min_open_days")
        turnout, _ = _rule("vote.show_turnout_to_owners")
        votes = Section("Formal votes and elections")
        votes.items.append(
            "One ballot per home. Owners on the deed vote; if a home has several owners, any "
            "one of them may cast its ballot."
        )
        if quorum:
            votes.items.append(f"Quorum: {quorum}% of homes must vote{quorum_cite}.")
        if passing:
            votes.items.append(
                f"A vote passes with {passing}% of the votes cast{pass_cite}, unless the "
                "governing documents need more for that question."
            )
        if days:
            votes.items.append(
                f"Voting stays open at least {_days(days)}{days_cite} and closes by itself "
                "at the deadline. Homes that have not voted get a reminder about two days "
                "before."
            )
        votes.items.append(
            "Nobody sees how the votes are going until voting closes. Secret ballots are kept "
            "apart from who voted."
            + (" While voting is open you can see how many homes have voted." if turnout else "")
        )
        votes.items.append(
            "Can't take part? Give a proxy from the meeting's page: someone who votes for "
            "your home at that meeting."
        )
        out.append(votes)
    if features.enabled("surveys", request):
        surveys = Section("Surveys")
        surveys.paras.append(
            "Surveys are the board asking your views. They are not votes and decide nothing; "
            "anonymous ones keep your answers apart from your name."
        )
        out.append(surveys)
    return out


def _requests(request=None) -> list[Section]:
    from core import features
    from operations.models import RequestType

    ask = Section("Asking the board")
    ask.paras.append(
        "Use Ask the board (on every help page and the Help menu) for a question, a billing "
        "query, something that needs fixing or a problem in a common area. Choose the kind, "
        "say what it is about and add a photo if it helps."
    )
    for value, label in RequestType.choices:
        days, cite = _rule(f"request.sla_days.{value}")
        if days:
            ask.items.append(
                f"{label}: the board aims to reply within {days} business "
                f"day{'' if days == 1 else 's'}{cite}."
            )
    ask.paras.append(
        "Replies come on the request's page and you get an email with a link. If the board "
        "needs more from you the request says “Waiting on you” and shows on your "
        "dashboard."
    )
    out = [ask]
    who = Section("Who can ask")
    who.paras.append(
        "Owners, authorized contacts and tenants can ask. View-only contacts read the "
        "requests on the account they follow; an owner or authorized contact asks for them."
    )
    out.append(who)
    if features.enabled("records_requests", request):
        days, cite = _rule("records.response_days")
        records = Section("Seeing the association's records")
        records.paras.append(
            "Pennsylvania law lets owners inspect the association's records. Ask on My "
            "records requests, saying which records you want to see."
            + (f" The board responds within {_days(days)}{cite}." if days else "")
        )
        out.append(records)
    return out


BUILDERS = {
    "paying": _paying,
    "architectural-review": _arc,
    "violations": _violations,
    "voting-and-meetings": _voting,
    "requests": _requests,
}


def sections(topic: Topic, request=None) -> list[Section]:
    return BUILDERS[topic.slug](request)


def plain_text(topic: Topic, request=None) -> str:
    """Everything a topic page says, as one string (search)."""
    parts = [topic.title, term_text(topic.summary)]
    for s in sections(topic, request):
        parts += [s.heading, *s.paras, *s.items]
    extra = current_text(topic.slug)
    if extra:
        parts.append(extra.body)
    return "\n".join(parts)


# --- The board's own words ----------------------------------------------------------------


def current_text(slug: str):
    from community.models import HelpText

    return HelpText.objects.filter(topic=slug).order_by("-saved_at", "-id").first()


def text_history(slug: str, limit: int = 10) -> list:
    from community.models import HelpText

    return list(
        HelpText.objects.filter(topic=slug)
        .select_related("saved_by")
        .order_by("-saved_at", "-id")[:limit]
    )


def save_text(slug: str, body: str, *, actor, request=None):
    """A new version of the board's text on a help page (append-only)."""
    from audit.services import record_event
    from community.models import HelpText

    policy.require(actor, "help.edit")
    if slug not in BY_SLUG:
        raise HelpError("Unknown help page.")
    body = (body or "").replace("\r\n", "\n").strip()
    if len(body) > MAX_TEXT:
        raise HelpError(f"Please keep it under {MAX_TEXT:,} characters.")
    current = current_text(slug)
    if current is not None and current.body == body:
        raise HelpError("Nothing changed.")
    if current is None and not body:
        raise HelpError("Write something to add to the page.")
    with transaction.atomic():
        row = HelpText.objects.create(
            topic=slug, body=body, saved_by=None if actor is policy.SYSTEM else actor
        )
        record_event(
            "help.text_saved",
            actor=None if actor is policy.SYSTEM else actor,
            request=request,
            target=row,
            metadata={"topic": slug, "cleared": not body},
        )
    return row


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]\n]{1,200})\]\(((?:https://|/)[^\s()<>\"']{0,500})\)")


def _inline(line: str) -> str:
    """One escaped line with **bold** and [links](https://... or /path)."""
    out, pos = [], 0
    for m in _LINK.finditer(line):
        out.append(_bold(escape(line[pos : m.start()])))
        href = m.group(2)
        if not _safe_href(href):
            out.append(_bold(escape(m.group(0))))
        else:
            rel = ' rel="noopener noreferrer"' if href.startswith("https://") else ""
            out.append(f'<a href="{escape(href)}"{rel}>{_bold(escape(m.group(1)))}</a>')
        pos = m.end()
    out.append(_bold(escape(line[pos:])))
    return "".join(out)


def _safe_href(href: str) -> bool:
    """A link the help text may carry (EXP-005): an https:// address with a
    host, or a path on this site. Never scheme-relative (``//host``), never
    a backslash (browsers read ``/\\host`` as ``//host``), never whitespace,
    control or invisible formatting characters."""
    if not href or "\\" in href:
        return False
    if any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in href):
        return False
    if href.startswith("https://"):
        parts = urlsplit(href)
        return parts.scheme == "https" and bool(parts.hostname)
    if href.startswith("/") and not href.startswith("//"):
        return url_has_allowed_host_and_scheme(href, allowed_hosts=None)
    return False


def _bold(escaped: str) -> str:
    return _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", escaped)


def render_text(body: str) -> str:
    """The board's text as safe HTML: every character escaped, then
    paragraphs, "- " lists, "## " headings, **bold** and links."""
    html: list[str] = []
    items: list[str] = []
    para: list[str] = []

    def flush():
        if para:
            html.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()
        if items:
            html.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for raw in (body or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith(("- ", "* ")):
            if para:
                flush()
            items.append(line[2:].strip())
        elif line.startswith("#"):
            flush()
            html.append(f"<h3>{_inline(line.lstrip('#').strip())}</h3>")
        else:
            if items:
                flush()
            para.append(line)
    flush()
    return mark_safe("".join(html))  # noqa: S308  # nosec B308 B703: built only from escaped text


# --- After the sign-up steps ---------------------------------------------------------------


def after_setup_url(user, request=None) -> str:
    """Where the guided first sign-in ends: the welcome page once (when help
    is on and this login may see it), else the dashboard. Never a blocking
    step: the welcome page is an ordinary signed-in page with links."""
    import datetime

    from django.utils import timezone

    from community import checklist

    joined = getattr(user, "date_joined", None)
    # Only for a login made recently (the checklist's window, at least 30
    # days): an owner who signed up long before D73 is not sent there.
    recent = joined and timezone.now() - joined <= datetime.timedelta(
        days=max(checklist.window_days(), 30)
    )
    if (
        recent
        and enabled(request)
        and policy.can(user, "help.view")
        and not checklist.welcome_seen(user)
    ):
        return reverse("welcome")
    return reverse("home")


# --- The guide to the menu ----------------------------------------------------------------

# One line per menu section, from docs/glossary.md ("Menu sections").
SECTION_DESCRIPTIONS = {
    "": "Your dashboard and everything waiting on you.",
    "My home": "Your own home: its account, requests and notices.",
    "My history": "Your own records from when you owned a home here.",
    "Community": "News, the calendar, voting, documents and the budget, for everyone here.",
    "Association": "The board's work on owners' requests, reviews, notices and homes.",
    "Maintenance": "Work orders, vendors and contracts.",
    "Finance": "Owner accounts, invoices, collections, budgets and reports.",
    "Admin": "The portal itself: status, server tasks and settings.",
}

# One line per menu page (docs/glossary.md); pages without one show their name only.
PAGE_DESCRIPTIONS = {
    "home": "To-dos first, then your balance, news and what's coming up.",
    "my_items": "Everything waiting on you, grouped by area.",
    "help": "How the portal works, and how to ask the board.",
    "my_lot": "Your home and the people on your account.",
    "my_account": "Your statement, balance, charges and payments.",
    "arc_list?mine=1": "Asking to change the outside of your home, and neighbors' projects.",
    "violations?mine=1": "Covenant or rule violations at your home, their notices, your replies.",
    "requests?mine=1": "Your questions and reports to the board, with the replies, and your "
    "requests to inspect the association's records.",
    "my_history": "Your statements and requests from your time as an owner.",
    "announcements": "News from the board.",
    "calendar": "Meetings and community events.",
    "meetings": "Board and owners' meetings, agendas and minutes.",
    "voting": "Formal votes, board elections and surveys.",
    "documents": "The Declaration, bylaws, rules, minutes and forms.",
    "resolutions": "Board resolutions and the policies in force.",
    "owner_budget": "This year's budget and the reserve plan's summary.",
    "community_info": "Frequently asked questions, the board and committees, the directory.",
    "reservations": "Booking the community's common areas.",
    "requests": "Owners' questions and reports, with response targets.",
    "arc_list": "Every home improvement request and its review.",
    "violations": "Covenant and rule notices to owners.",
    "records_requests": "Owners' requests to inspect records.",
    "tasks": "The board's own to-do list and recurring deadlines.",
    "people": "Everyone in the community register, with their roles.",
    "lot_list": "Every home and its owners; sign-ups.",
    "paper_mail": "Official notices by post for owners who get paper.",
    "work_orders": "Maintenance jobs for vendors.",
    "vendors": "The association's suppliers.",
    "contracts": "Vendor contracts and renewal dates.",
    "ledger_home": "Every home's account and balance.",
    "invoices": "Vendor bills, approvals and payments.",
    "collections": "Accounts that are behind, letters and cases.",
    "budgets": "Budgets by year.",
    "accounting": "Journal exports, deposits and reconciliation.",
    "reports": "Financial and activity reports.",
    "finance_overview": "Every finance part at a glance, with what waits on you in each.",
    "system_status": "Health checks, updates and backups.",
    "sysadmin_activity": "What sysadmins did, for the board to see.",
    "server_tasks": "Backups, restore drills and other server tasks.",
    "invitations": "Invitations to sign in, with re-send and revoke.",
    "settings_hub": "Community setup and the settings pages.",
}


def menu_guide(request) -> list[dict]:
    """The menu as this person sees it, each section and page with its one
    line: the "What's where" part of the Help page."""
    nav = getattr(request, "_help_nav", None)
    if nav is None:
        from core import navigation

        nav = navigation.build(request)
    out = []
    for section in nav["sections"]:
        out.append(
            {
                "title": section["title"] or "Start",
                "text": SECTION_DESCRIPTIONS.get(section["title"], ""),
                "work": section["work"],
                "items": [
                    {
                        "label": item["label"],
                        "url": item["url"],
                        "icon": item["icon"],
                        "text": PAGE_DESCRIPTIONS.get(item["name"], ""),
                    }
                    for item in section["items"]
                ],
            }
        )
    return out
