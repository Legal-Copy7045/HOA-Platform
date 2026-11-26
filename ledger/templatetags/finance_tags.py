"""A small tab bar across the four day-to-day money pages: Owner accounts,
Collections, Accounting and Reports. It shows only on those pages (and the
pages under them); everything else in Finance and Maintenance is reached from
the sidebar. Tabs the user cannot use are left out."""

from django import template
from django.urls import reverse

from core import policy

register = template.Library()

# (label, URL name, path prefix that belongs to the tab, permission)
TABS = (
    ("Owner accounts", "ledger_home", "/finance/ledger", "finance.view"),
    ("Collections", "collections", "/finance/collections", "finance.view"),
    ("Accounting", "accounting", "/finance/accounting", "finance.view"),
    ("Reports", "reports", "/reports", "reports.view"),
)


@register.simple_tag(takes_context=True)
def finance_tabs(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    path = getattr(request, "path", "")
    tabs = []
    for label, url_name, prefix, permission in TABS:
        if not policy.can(user, permission):
            continue
        tabs.append({"label": label, "url": reverse(url_name), "current": path.startswith(prefix)})
    if not any(t["current"] for t in tabs):
        return []  # not one of these pages: no tab bar
    return tabs
