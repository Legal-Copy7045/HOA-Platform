"""Hub pages that hold rarely used links, so the sidebar stays short."""

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import render

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
    "finance_more": (
        "More finance",
        "Finance pages used now and then: the reserve plan, online payments, payments back to"
        " owners and resale certificates.",
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
        {"heading": heading, "subtitle": subtitle, "links": links, **extra},
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

    links = [
        link
        for link in navigation.hub_links(request, "settings_hub")
        if link["name"] not in navigation.COVERED_BY_SETUP
    ]
    heading, _subtitle = PAGES["settings_hub"]
    return render(
        request,
        "community/setup/home.html",
        {
            **setup_front(),
            "heading": heading,
            "links": links,
            "settings_front": True,
        },
    )


@login_required
def community_info(request):
    return _hub(request, "community_info")


@login_required
def finance_more(request):
    return _hub(request, "finance_more")
