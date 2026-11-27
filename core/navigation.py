"""The sidebar: one definition, filtered per user by the same policy table
the views enforce. Roles are computed once per request here.

Rarely used pages live on hub pages (HUBS) instead of the sidebar: the hub's
entry is shown when at least one of its pages is open to the user (and its
feature is on), counts the hub's waiting items, and is marked current on any
of its pages. A hub with only one page open to the user links straight to it.
Labels match the pages' <h1> (docs/glossary.md).
"""

from __future__ import annotations

from django.urls import NoReverseMatch, reverse

from community.terms import term_text
from core import features, policy
from core.my_items import MY_ITEMS_PERMISSIONS

# Hub pages: url name -> [(label, url name, icon, permission, description)].
HUBS = {
    # The Voting page itself lists votes, elections and surveys; these entries
    # decide who sees "Voting" and mark it current on their detail pages.
    "voting": [
        (
            "Votes",
            "votes",
            "vote",
            "vote.view",
            "Formal, binding votes of the owners.",
        ),
        (
            "Elections",
            "elections",
            "users",
            "vote.view",
            "Board elections: nominations, candidates, ballots and proxies.",
        ),
        (
            "Surveys",
            "surveys",
            "list",
            "survey.view",
            "Informal surveys and polls from the board to hear your views.",
        ),
    ],
    "community_info": [
        (
            "Frequently asked questions",
            "faq",
            "info",
            "announcement.view",
            "Quick answers about living here.",
        ),
        (
            "Board and committees",
            "board_and_committees",
            "users",
            # Every resident (owner decision 2026-11-26): the COMMUNITY audience.
            "board_committees.view",
            "Who is on the board, their offices, and the committees.",
        ),
        (
            "Owner directory",
            "directory",
            "users",
            "directory.view",
            "Neighbors who chose to share their contact details.",
        ),
        # Committees (the workspaces) open from Board and committees, where
        # each committee's name links to it (WT-008); see SUBPAGES.
    ],
    # Finance pages the board opens now and then (WT-036): the Finance section
    # keeps the daily ones.
    "finance_more": [
        (
            "Reserve plan",
            "reserve_plan",
            "chart",
            "reserves.view",
            "The reserve study's components, the funding plan and what has been adopted.",
        ),
        (
            "Online payments",
            "online_payments",
            "wallet",
            "payments.manage",
            "Card and bank payments through Stripe, payouts and automatic payments.",
        ),
        (
            "Disbursements",
            "disbursements",
            "dollar",
            "disbursement.view",
            "Surplus distributions and refunds to owners, with their checks.",
        ),
        # Books and close (D69, D71).
        (
            "Bank accounts and funds",
            "books_accounts",
            "wallet",
            "books.view",
            "The association's bank accounts, the fund each belongs to and their opening balances.",
        ),
        (
            "Bank categories and rules",
            "books_coding",
            "list",
            "books.view",
            "Categories bank lines are coded to, and the rules that code them by themselves.",
        ),
        (
            "Tax worksheet",
            "tax_worksheet",
            "file",
            "tax.view",
            "The Form 1120-H worksheet, 1099 totals with the IRIS upload file, and the filing "
            "deadlines.",
        ),
        (
            "Resale certificates",
            "resales",
            "file",
            "resale.manage",
            "Certificates for a home being sold, with the fee and the documents.",
        ),
    ],
    "settings_hub": [
        # The setup wizard (D51): board members who may change rules.
        (
            "Community setup",
            "setup_home",
            "list",
            "community.manage_rules",
            "Step by step through every set-up decision, showing today's values."
            " Run setup again at any time.",
        ),
        (
            "Community settings",
            "community_settings",
            "sliders",
            "lot.view_all",
            "{Terms}, deadlines and voting rules from the Declaration, each with its citation.",
        ),
        (
            "ARC request types",
            "arc_project_types",
            "pencil",
            "arc.configure",
            "Kinds of home improvement project and what each must include.",
        ),
        (
            "Home neighbors",
            "arc_neighbors",
            "map",
            "arc.configure",
            "Which homes are asked to acknowledge a neighbor's project.",
        ),
        (
            "Letters and certificates",
            "letters_settings",
            "mail",
            "letters.manage",
            "Wording of collection letters and resale certificates, and where to send checks.",
        ),
        (
            "Paper mail settings",
            "paper_mail_settings",
            "mail",
            "mail.print",
            "Where the address prints for window envelopes, and the USPS tracking barcode.",
        ),
        (
            "Common area booking rules",
            "reservation_settings",
            "calendar",
            "reservation.configure",
            "The common areas, which of them can be booked, and their rules.",
        ),
        (
            "Branding",
            "branding_settings",
            "sliders",
            "branding.manage",
            "The sign-in page picture.",
        ),
        (
            "Features",
            "feature_settings",
            "sliders",
            "features.manage",
            "Switch optional parts of the portal on or off.",
        ),
        (
            "Contact retention",
            "retention",
            "clock",
            "retention.manage",
            "Former owners' contact details cleared after the retention period.",
        ),
        (
            "Records retention",
            "records_retention",
            "clock",
            "records_retention.manage",
            "How long each kind of document is kept, disposal by two board members, legal holds.",
        ),
        (
            "Demo data",
            "demo_data",
            "leaf",
            "system.run_task",
            "Replace everything with a fictional demo dataset (demo and test portals only).",
        ),
    ],
}


def _demo_data_allowed() -> bool:
    from core import demo_reseed

    return demo_reseed.allowed()


# Hub pages shown only to some of the roles that may open them: the manager
# reaches Paper mail settings from Paper mail (its first button), so the
# manager's Settings has one page and the menu links straight to it (WT-009).
TILE_GROUPS = {"paper_mail_settings": frozenset({policy.BOARD})}

# Pages the board's Settings front (the Community setup steps, WT-004) already
# covers: still part of the hub (the Settings entry is current on them) but
# not repeated as tiles there. Features is the setup step (WT-038); Contact
# retention is linked from the Privacy step.
COVERED_BY_SETUP = frozenset({"setup_home", "feature_settings", "retention"})

# Hub pages that exist only on some deployments: listed only while the check
# passes (the page itself answers 404 otherwise). Demo data: D53.
ONLY_WHEN = {"demo_data": _demo_data_allowed}
# Hubs whose own page is where every child lands (votes, elections and
# surveys all open the Voting list), so a single child keeps the hub's label
# and links to the hub's own page filtered by this query parameter (the
# child's old URL is only a redirect there).
HUB_KEEPS_LABEL = {"voting": "type"}
# The label for a hub entry kept above when its one page has its own name
# (governance.voting_views: the heading is "Surveys" when only surveys show).
HUB_SINGLE_LABELS = {("voting", "surveys"): "Surveys"}

# Pages reached from a sidebar entry's page rather than from the sidebar: the
# entry is marked current on them and counts their waiting items. A page
# listed here is not shown again as its own entry further down the menu.
SUBPAGES = {
    # Meetings live on the Calendar (D49): a meeting page (/meetings/<pk>/)
    # marks Calendar current.
    "calendar": ["meetings"],
    "arc_list?mine=1": ["signoffs_mine"],
    "people": ["access_review"],
    "lot_list": ["invitations"],
    "tasks": ["compliance"],
    # Committee workspaces open from Board and committees (WT-008).
    "community_info": ["committees"],
    "paper_mail": ["paper_mail_settings"],
    # The incident timeline opens from System status (D74).
    "system_status": ["ops_timeline"],
    # The board reads server task history on Sysadmin activity (WT-007);
    # sysadmins, who cannot open that page, keep Server tasks in the menu.
    "sysadmin_activity": ["server_tasks"],
    # Help pages (D73): the topics, Getting started and Welcome mark Help.
    "help": ["welcome"],
}

# Entries shown only while an optional feature is off: Meetings is part of
# the Calendar (D49), but meetings are core, so with the Calendar switched off
# the Meetings list comes back.
SHOWN_WHILE_OFF = {"meetings": "calendar"}

# (label, url name, icon, permission or tuple of alternatives; None = everyone).
# A hub's own entry takes its visibility from its pages (permission HUB).
# The owner side ("My home") and the board side ("Association" onwards) stay
# separate sections: ending a board seat removes only the board side.
HUB = "hub"
SECTIONS = [
    (
        "",
        [
            ("Dashboard", "home", "home", None),
            ("My items", "my_items", "inbox", MY_ITEMS_PERMISSIONS),
            # Owner help (D73): how it works, getting started, Ask the board.
            ("Help", "help", "help", "help.view"),
        ],
    ),
    (
        "My home",
        [
            ("My home", "my_lot", "pin", "lot.view_own"),
            ("My account", "my_account", "wallet", "ledger.view_own"),
            ("My home improvement requests (ARC)", "arc_list?mine=1", "pencil", "arc.submit"),
            ("Notices about my home", "violations?mine=1", "flag", "lot.view_own"),
            # Residents (incl. view-only contacts and tenants); former owners
            # see their requests inside My history instead.
            ("My requests", "requests?mine=1", "mail", ("lot.view_own", "request.submit")),
            ("My records requests", "records_requests?mine=1", "inbox", "records_request.submit"),
            ("My history", "my_history", "clock", "history.view_own"),
        ],
    ),
    (
        "Community",
        [
            ("Announcements", "announcements", "megaphone", "announcement.view"),
            ("Calendar", "calendar", "calendar", "calendar.view"),
            ("Meetings", "meetings", "book", "meeting.view"),
            ("Voting", "voting", "vote", HUB),
            ("Documents", "documents", "folder", "document.view_owner"),
            ("Resolutions and policies", "resolutions", "file", "resolution.view"),
            ("Budget and reserves", "owner_budget", "chart", "budget.view_owner"),
            ("Community info", "community_info", "info", HUB),
            ("Common area bookings", "reservations", "calendar", "reservation.view"),
        ],
    ),
    (
        "Association",
        [
            ("Owner requests", "requests", "mail", "request.manage"),
            ("Architectural review", "arc_list", "pencil", "arc.view_all"),
            ("Violations", "violations", "flag", "violation.view_all"),
            ("Records requests", "records_requests", "inbox", "records_request.manage"),
            ("Board tasks", "tasks", "list", "task.manage"),
            ("People", "people", "users", "person.manage"),
            # Sign-ups are a tab of it for the board (WT-002).
            ("Homes and owners", "lot_list", "map", "lot.view_all"),
            # Letters, envelopes and labels for owners who get paper (D55).
            ("Paper mail", "paper_mail", "mail", "mail.print"),
        ],
    ),
    (
        "Maintenance",
        [
            ("Work orders", "work_orders", "wrench", "finance.view"),
            ("Vendors", "vendors", "users", "finance.view"),
            ("Contracts", "contracts", "file", "finance.view"),
            # Insurance, vendor certificates and contract deadlines (D72).
            ("Renewals", "renewals", "shield", "renewals.view"),
        ],
    ),
    (
        "Finance",
        [
            ("Owner accounts", "ledger_home", "wallet", "finance.view"),
            ("Invoices", "invoices", "receipt", "finance.view"),
            ("Collections", "collections", "dollar", "finance.view"),
            ("Budgets", "budgets", "chart", "finance.view"),
            ("Accounting", "accounting", "receipt", "finance.view"),
            # Books and close (D70, D71).
            ("Month and year close", "books_close", "check-circle", "books.view"),
            ("Financial statements", "financial_statements", "chart", "statements.view"),
            ("Reports", "reports", "chart", "reports.view"),
            # Reserve plan, Online payments, Disbursements, Resale certificates.
            ("More finance", "finance_more", "dollar", HUB),
        ],
    ),
    (
        "Admin",
        [
            ("System status", "system_status", "activity", "system.view_status"),
            # Before Server tasks: for the board it holds the task history
            # (SUBPAGES), so Server tasks is not a second entry (WT-007).
            ("Sysadmin activity", "sysadmin_activity", "shield", "audit.view_sysadmin_activity"),
            # Backups, restore drills and other host tasks (D36): sysadmins.
            ("Server tasks", "server_tasks", "clock", "system.view_tasks"),
            # Sysadmins re-send and revoke (D9); the board has them on the
            # Sign-ups tab of Homes and owners, its SUBPAGE (WT-002, WT-018).
            ("Invitations", "invitations", "mail", "invite.resend"),
            ("Settings", "settings_hub", "sliders", HUB),
        ],
    ),
]
# Sections open by default; the others open when the current page is inside
# (and the browser remembers what the person last chose, static/js/app.js).
OPEN_BY_DEFAULT = {"My home", "Community"}
# Staff open their work instead: the work sections with something waiting,
# and My home and Community start closed (WT-040).
STAFF_GROUPS = frozenset(
    {policy.BOARD, policy.MANAGER, policy.ARC_REVIEWER, policy.AUDITOR, policy.SYSADMIN}
)
PERSONAL_SECTIONS = {"My home", "Community"}
# The work sections, shown together under one "Board" heading in the accent
# colour (D64): a page inside them is a board page (the "Board" badge and
# strip, base.html). Owners never see these sections, so their pages never
# carry the cue.
WORK_SECTIONS = ("Association", "Maintenance", "Finance", "Admin")


def work_label(groups: frozenset[str]) -> str:
    """The heading over the work sections and the page badge: "Board" for a
    board member; staff who are not on the board (manager, auditor, ARC
    reviewer, sysadmin) read "Staff", since they do not act for the board."""
    return "Board" if policy.BOARD in groups else "Staff"


def _open_by_default(title: str, count: int, staff: bool) -> bool:
    if not staff:
        return title in OPEN_BY_DEFAULT
    # Staff: chosen in build() (only the busiest work section, WT-036).
    return False


ROLE_LABELS = {
    policy.OWNER: "Owner",
    policy.BOARD: "Board",
    policy.MANAGER: "Manager",
    policy.ARC_REVIEWER: "ARC",
    policy.AUDITOR: "Auditor",
    policy.SYSADMIN: "Sysadmin",
}


def allowed(groups: frozenset[str], permission) -> bool:
    if permission is None:
        return True
    options = permission if isinstance(permission, tuple) else (permission,)
    return any(policy.PERMISSIONS[p] & groups for p in options)


def _url(name: str) -> str | None:
    url_name, _, query = name.partition("?")
    try:
        return reverse(url_name) + (f"?{query}" if query else "")
    except NoReverseMatch:
        return None


def shortcuts(request, names: list[str], groups: frozenset[str]) -> list[dict]:
    """Links to these pages (URL names) with their menu label and icon, for
    the ones this person may open and whose feature is on (the dashboard's
    role shortcuts, WT-016)."""
    known = {
        name: (label, icon, perm) for _t, items in SECTIONS for label, name, icon, perm in items
    }
    for hub in HUBS.values():
        known.update({name: (label, icon, perm) for label, name, icon, perm, _d in hub})
    out = []
    for name in names:
        label, icon, permission = known[name]
        if permission == HUB or not allowed(groups, permission):
            continue
        if not features.url_name_enabled(name.partition("?")[0], request):
            continue
        url = _url(name)
        if url:
            out.append({"label": label, "url": url, "icon": icon})
    return out


def hub_links(request, hub: str, groups: frozenset[str] | None = None) -> list[dict]:
    """The pages of a hub this person may open, with their features on. A page
    whose URL does not exist (yet) is skipped."""
    if groups is None:
        groups = policy.roles_for(request.user).groups
    out = []
    for label, name, icon, permission, description in HUBS[hub]:
        if not allowed(groups, permission):
            continue
        if name in TILE_GROUPS and not groups & TILE_GROUPS[name]:
            continue
        if not features.url_name_enabled(name.partition("?")[0], request):
            continue
        if name in ONLY_WHEN and not ONLY_WHEN[name]():
            continue
        url = _url(name)
        if url:
            out.append(
                {
                    "label": label,
                    "name": name,
                    "url": url,
                    "icon": icon,
                    "text": term_text(description),
                }
            )
    return out


def _matches(url: str, path: str, here_mine: bool) -> bool:
    base, _, query = url.partition("?")
    if base == "/":
        return path == "/"
    return path.startswith(base) and (("mine=1" in query) == here_mine)


def _item(request, groups, label, name, icon, permission, counts) -> tuple[dict, list] | None:
    """One sidebar entry and the pages it stands for, or None if hidden."""
    if permission == HUB:
        children = hub_links(request, name, groups)
        if not children:
            return None
        if len(children) > 1:
            # Pages reached from the hub's pages (Committees from Board and
            # committees) mark it current too.
            children += [{"name": n, "url": u} for n in SUBPAGES.get(name, []) if (u := _url(n))]
    else:
        if not allowed(groups, permission):
            return None
        children = [{"name": n, "url": u} for n in SUBPAGES.get(name, []) if (u := _url(n))]
    if not features.url_name_enabled(name.partition("?")[0], request):
        return None
    if name in SHOWN_WHILE_OFF and features.enabled(SHOWN_WHILE_OFF[name], request):
        return None
    url = _url(name)
    if url is None:
        return None
    if permission == HUB and len(children) == 1:
        # One page behind the hub: go straight there (a tenant's Voting is
        # Surveys; a sysadmin's Settings is Branding).
        if name in HUB_KEEPS_LABEL:
            url = f"{url}?{HUB_KEEPS_LABEL[name]}={children[0]['name']}"
            # ...except where the one page is named differently, so the menu
            # says what the page's heading says (a tenant's "Surveys", WT-018).
            label = HUB_SINGLE_LABELS.get((name, children[0]["name"]), label)
        else:
            url = children[0]["url"]
            label, icon = children[0]["label"], children[0]["icon"]
    count = counts.get(name, 0) + sum(counts.get(c["name"], 0) for c in children)
    item = {
        "label": label,
        "url": url,
        "icon": icon,
        "current": False,
        "count": count,
        "name": name,
    }
    return item, children


def build(request) -> dict:
    user = request.user
    roles = policy.roles_for(user)
    path = request.path
    here_mine = request.GET.get("mine") == "1"
    seen: set[str] = set()
    sections = []
    from core.my_items import nav_counts

    try:
        counts = nav_counts(user, request) if user.is_authenticated else {}
    except Exception:  # a count must never break the page
        counts = {}
    staff = bool(roles.groups & STAFF_GROUPS)
    best = ("", None, None)  # longest matching URL wins the "current page" mark
    for title, items in SECTIONS:
        visible = []
        for label, name, icon, permission in items:
            if name in seen:
                continue
            found = _item(request, roles.groups, label, name, icon, permission, counts)
            if found is None:
                continue
            seen.add(name)
            item, children = found
            seen.update(c["name"] for c in children)
            for candidate in [item["url"]] + [c["url"] for c in children]:
                base = candidate.partition("?")[0]
                if _matches(candidate, path, here_mine) and len(base) > len(best[0]):
                    best = (base, item, len(sections))
            visible.append(item)
        if visible:
            count = sum(i["count"] for i in visible)
            if title == "My home" and not roles.groups & policy.RESIDENTS:
                # A former owner's one section holds only My history (WT-014).
                title = "My history"
            sections.append(
                {
                    "title": title,
                    "key": title.lower().replace(" ", "-"),
                    "items": visible,
                    "open": _open_by_default(title, count, staff),
                    "count": count,
                    "work": title in WORK_SECTIONS,
                }
            )
    work = [s for s in sections if s["work"]]
    if staff and work:
        # Staff: only the work section with the most waiting opens, else the
        # first one, plus the current page's section below (WT-036: a short
        # drawer on a phone; the browser remembers what the person opens).
        busiest = max(work, key=lambda s: s["count"])
        (busiest if busiest["count"] else work[0])["open"] = True
    board_page = False
    if best[1]:
        best[1]["current"] = True
        sections[best[2]]["open"] = True
        sections[best[2]]["current"] = True
        board_page = sections[best[2]]["work"]
    return {
        "sections": sections,
        # The menu as drawn: personal sections, then the work sections under
        # one heading in the accent colour (D64).
        "personal_sections": [s for s in sections if not s["work"]],
        "work_sections": work,
        "work_label": work_label(roles.groups),
        "work_count": sum(s["count"] for s in work),
        # The page is one of the work sections' pages: base.html shows the
        # "Board" (or "Staff") badge and the accent strip (D64).
        "board_page": board_page,
        "tabbar": tabbar(sections, roles.groups),
        "roles": [ROLE_LABELS[g] for g in ROLE_LABELS if g in roles.groups],
        "person_id": roles.person_id,
    }


# Phone tab bar (WT-041, D48): the pages people open most, at the bottom of a
# phone screen, plus Menu (the drawer). Built from the menu entries this person
# already sees, so it never shows a page the menu hides. Staff get their work
# queue; owners and contacts get their account and Voting.
TABBAR_STAFF_GROUPS = frozenset({policy.BOARD, policy.MANAGER, policy.ARC_REVIEWER})
TABBAR_STAFF = ("home", "my_items", "search")
TABBAR_OWNER = ("home", "my_account", "voting")
TABBAR_FALLBACK = ("my_items", "search")


def tabbar(sections: list[dict], groups: frozenset[str]) -> list[dict]:
    """Up to three tabs (the template adds Menu): each a menu entry, or Search."""
    entries = {i["name"]: i for s in sections for i in s["items"]}
    search = {
        "label": "Search",
        "url": reverse("search"),
        "icon": "search",
        "current": False,
        "count": 0,
        "name": "search",
    }
    wanted = TABBAR_STAFF if groups & TABBAR_STAFF_GROUPS else TABBAR_OWNER
    tabs: list[dict] = []
    for name in wanted + TABBAR_FALLBACK:
        if len(tabs) == 3:
            break
        tab = search if name == "search" else entries.get(name)
        if tab is not None and tab not in tabs:
            tabs.append(tab)
    return tabs
