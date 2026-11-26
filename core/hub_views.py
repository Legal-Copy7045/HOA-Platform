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
}


def _hub(request, name):
    links = navigation.hub_links(request, name)
    if not links:
        groups = policy.roles_for(request.user).groups
        if any(navigation.allowed(groups, row[3]) for row in navigation.HUBS[name]):
            raise Http404  # allowed, but every page here is switched off
        raise PermissionDenied
    heading, subtitle = PAGES[name]
    return render(
        request,
        "core/hub.html",
        {"heading": heading, "subtitle": subtitle, "links": links},
    )


@login_required
def settings_hub(request):
    return _hub(request, "settings_hub")


@login_required
def community_info(request):
    return _hub(request, "community_info")
