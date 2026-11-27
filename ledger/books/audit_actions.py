"""Audit actions of the books and close (D69-D71), registered when the ledger
app loads. Ids, kinds, counts, dates and amounts only: never a bank
description, a name or an address."""

from audit.actions import _spec, register

SPECS = [
    _spec(
        "books.account_saved",
        "Bank account put in the books",
        "connection_id",
        "fund",
        "primary",
        "opening_set",
    ),
    _spec("books.category_saved", "Books category saved", "category_id", "kind", "active"),
    _spec(
        "books.line_coded",
        "Bank line coded to a category",
        "line_id",
        "category_id",
        "rule_id",
        "correction",
    ),
    _spec("books.line_uncoded", "Bank line coding undone", "line_id", "entry_id"),
    _spec("books.rules_applied", "Coding rules applied to new bank lines", "count"),
    _spec("books.rule_saved", "Coding rule saved", "rule_id", "category_id", "sign"),
    _spec("books.rule_ended", "Coding rule ended", "rule_id"),
    _spec("books.balance_entered", "Statement balance entered", "connection_id", "as_of"),
    _spec(
        "books.exception_noted",
        "Close exception explained",
        "period",
        "check",
        "item",
        "amount_cents",
        "needs_agreement",
    ),
    _spec(
        "books.exception_agreed",
        "Close exception agreed by a second board member",
        "period",
        "check",
        "amount_cents",
    ),
    _spec("books.period_closed", "Books period closed", "period", "kind", "how"),
    _spec("books.reopen_requested", "Reopening a closed period requested", "period", "kind"),
    _spec("books.reopen_approved", "Reopening a closed period approved", "period", "kind"),
    _spec("books.reopen_declined", "Reopening a closed period declined", "period", "kind"),
    _spec("books.auto_close_ran", "Automatic close ran", "coded", "matched", "closed"),
    _spec(
        "books.statements_exported", "Financial statements downloaded", "period", "format", "closed"
    ),
    _spec(
        "books.package_built",
        "Year-end package built",
        "fiscal_year",
        "files",
        "documents",
        "skipped",
        "automatic",
        "closed",
    ),
    _spec(
        "books.package_downloaded", "Year-end package downloaded", "fiscal_year", "files", "size"
    ),
    _spec("tax.exported", "Tax worksheet or 1099 file downloaded", "year", "format", "count"),
    _spec("tax.deadlines_added", "Tax and state filings put on Recurring deadlines", "count"),
    _spec(
        "accounting.quickbooks_exported", "QuickBooks journal downloaded", "start", "end", "rows"
    ),
]


def register_all() -> None:
    for spec in SPECS:
        register(spec)
