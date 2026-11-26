"""The one Finance tab bar (UX review): the same tabs on every finance page,
whichever app the page belongs to. Tabs the user cannot use, or whose
optional feature is switched off, are left out."""

from django import template
from django.urls import reverse

from core import features, policy

register = template.Library()

# (label, URL name, path fragment that marks the tab current, permission, feature)
TABS = (
    ("Owner accounts", "ledger_home", "/finance/ledger", None, None),
    ("Invoices", "invoices", "/finance/invoices", None, None),
    ("Contracts", "contracts", "/finance/contracts", None, "contracts"),
    ("Work orders", "work_orders", "/finance/work-orders", None, "work_orders"),
    ("Vendors", "vendors", "/finance/vendors", None, "work_orders"),
    ("Budgets", "budgets", "/finance/budgets", None, None),
    ("Reserve plan", "reserve_plan", "/finance/reserves", "reserves.view", "reserves"),
    ("Accounting", "accounting", "/finance/accounting", None, None),
    ("Online payments", "online_payments", "/finance/online-payments", None, "online_payments"),
    ("Collections", "collections", "/finance/collections", None, None),
    ("Letters", "letters_settings", "/finance/letters", "letters.manage", None),
    ("Resale", "resales", "/resale", "resale.request", "resale"),
    ("Reports", "reports", "/reports", None, None),
)


@register.simple_tag(takes_context=True)
def finance_tabs(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    path = getattr(request, "path", "")
    tabs = []
    for label, url_name, fragment, permission, feature in TABS:
        if permission and not policy.can(user, permission):
            continue
        if feature and not features.enabled(feature, request):
            continue
        tabs.append({"label": label, "url": reverse(url_name), "current": fragment in path})
    return tabs
