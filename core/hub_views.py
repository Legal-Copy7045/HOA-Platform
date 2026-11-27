"""Hub pages that hold rarely used links, so the sidebar stays short."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render

from core import navigation, policy

PAGES = {
    "settings_hub": (
        "Settings",
        "Set-up pages the board uses now and then. Each page is open only to the roles"
        " that manage it.",
    ),
    "community_info": (
        "Community info",
        "Answers to common questions, who is on the board and committees, and the owner directory.",
    ),
}


def _hub(request, name, template="core/hub.html", **extra):
    links = navigation.hub_links(request, name)
    if not links:
        groups = policy.roles_for(request.user).groups
        if any(navigation.allowed(groups, row[3]) for row in navigation.HUBS[name]):
            raise Http404  # allowed, but every page here is switched off
        raise PermissionDenied
    heading, subtitle = PAGES[name]
    return render(
        request,
        template,
        {
            "heading": heading,
            "subtitle": subtitle,
            "links": links,
            # Items for the hub page itself, beside the tiles' own counts (D89).
            "own_count": navigation.waiting_counts(request).get(name, 0),
            **extra,
        },
    )


@login_required
def settings_hub(request):
    """Settings. For the board the front is Community setup (WT-004): where
    the run stands and every step, then "Change one thing" links to the
    editors the steps do not contain. Features is the setup step itself and
    Contact retention is linked from the Privacy step (WT-038), so neither is
    a tile here. Other roles get the plain hub."""
    if not policy.can(request.user, "community.manage_rules"):
        return _hub(request, "settings_hub")
    from community.setup_views import setup_front

    every = navigation.hub_links(request, "settings_hub")
    links = [link for link in every if link["name"] not in navigation.COVERED_BY_SETUP]
    # "Finish community setup" and the items of pages the steps cover (rules
    # missing a value, two-person coverage) wait on the setup card, so the
    # page's numbers add up to the menu bubble (D89).
    setup_count = navigation.waiting_counts(request).get("settings_hub", 0) + sum(
        link["count"] for link in every if link["name"] in navigation.COVERED_BY_SETUP
    )
    heading, _subtitle = PAGES["settings_hub"]
    return render(
        request,
        "community/setup/home.html",
        {
            **setup_front(),
            "heading": heading,
            "links": links,
            "setup_count": setup_count,
            "settings_front": True,
        },
    )


@login_required
def community_info(request):
    return _hub(request, "community_info")


@login_required
def finance_more(request):
    """The More finance hub became the Finance overview (D89)."""
    policy.require(request.user, "finance.view")
    return redirect("finance_overview")
