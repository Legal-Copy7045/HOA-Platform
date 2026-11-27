"""Synthetic owner-help data for the demo community (D73): the board's own
words on two help pages, and getting-started progress for a few demo logins
so the checklist shows partly done. Fictional only; safe to run again (it
adds nothing that is already there).

Called from the oak_creek dataset through ``manage.py seed_demo_help``; can
also be run alone on a community seeded before D73.
"""

from __future__ import annotations

from core.policy import SYSTEM

TEXTS = {
    "paying": (
        "Checks are collected from the clubhouse drop box on the 1st and the 15th.\n\n"
        "## Payment plans\n"
        "- Ask early: the board agrees a plan before any late fee is added.\n"
        "- A plan is confirmed in writing on your request.\n\n"
        "See **My account** for your statement (demo text)."
    ),
    "architectural-review": (
        "The committee meets on the second Tuesday of the month; complete requests sent a week "
        "before are on its agenda.\n\n"
        "The approved colour palette is in [Documents](/documents/) (demo text)."
    ),
}


def seed() -> str:
    from accounts.models import User
    from community import checklist
    from community import help as helpdocs
    from community.models import HelpMark

    added = 0
    for slug, body in TEXTS.items():
        if helpdocs.current_text(slug) is None:
            helpdocs.save_text(slug, body, actor=SYSTEM)
            added += 1
    # A few demo logins part-way through getting started.
    demo = list(User.objects.filter(person__display_name__endswith="(demo)").order_by("email")[:6])
    plans = [
        [(HelpMark.Kind.WELCOME, "")],
        [(HelpMark.Kind.WELCOME, ""), (HelpMark.Kind.DOCS_READ, "governing")],
        [
            (HelpMark.Kind.WELCOME, ""),
            (HelpMark.Kind.OPENED, "board"),
            (HelpMark.Kind.CONFIRMED, "contact"),
        ],
        [(HelpMark.Kind.WELCOME, ""), (HelpMark.Kind.DISMISSED, "texts")],
    ]
    marks = 0
    for user, plan in zip(demo, plans, strict=False):
        for kind, key in plan:
            marks += checklist.mark(user, kind, key)
    return f"Help: {added} board text(s), {marks} checklist mark(s)."
