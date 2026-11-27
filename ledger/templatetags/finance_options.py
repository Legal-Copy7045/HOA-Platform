"""Template tags of the finance options (D80, D82): the interest policy line
on owners' account page and the legal panel on a collection case. Each shows
nothing while its feature is off."""

from django import template

from core import features, policy

register = template.Library()


@register.inclusion_tag("ledger/_interest_notice.html", takes_context=True)
def interest_notice(context):
    from ledger import interest

    return {"notice": interest.owner_notice(context.get("request"))}


@register.inclusion_tag("ledger/_legal_panel.html", takes_context=True)
def legal_panel(context, case):
    request = context.get("request")
    if (
        request is None
        or not features.enabled("collections_legal", request)
        or not policy.can(request.user, "legal.view")
    ):
        return {"show": False}
    from ledger.finance_option_views import legal_panel_context

    return {
        "show": True,
        "request": request,
        "csrf_token": context.get("csrf_token"),
        **legal_panel_context(request, case),
    }
