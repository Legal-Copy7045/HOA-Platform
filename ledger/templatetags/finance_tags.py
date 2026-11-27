"""A small tab bar across the day-to-day money pages: the Finance overview
(D89), Owner accounts, Collections, Accounting and Reports, the same pages as
the Finance menu entries except Invoices. It shows only on those pages (and
the pages under them); every other finance page opens from the overview.
Tabs the user cannot use are left out."""

from django import template
from django.urls import reverse

from core import policy

register = template.Library()

# (label, URL name, path prefix that belongs to the tab, permission)
TABS = (
    ("Finance overview", "finance_overview", "/finance/overview", "finance.view"),
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
