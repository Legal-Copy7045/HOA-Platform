"""Server errors in plain words (D45).

The owner is not a developer: each kind of server error is described first as
a sentence (what failed, why, who noticed, what to do), with the technical
line (exception class, code location, route) underneath for the maintainer.

Built only from the stored signature (class name, route name, status), so it
can never carry a message, path or person.
"""

from __future__ import annotations

from core import errors

CODE_BUG = "because of a bug in the portal's code"
CODE_FIX = "Needs a code fix: the portal's maintainer has the technical details below."
WAIT = "No action if it stops by itself; if it keeps happening, tell the portal's maintainer."
PEOPLE_SAW = "People saw an error page."

# Routes people do not open themselves, or whose name is not a menu label:
# route -> (what it is, who notices).
ROUTES = {
    "pwa_service_worker": (
        "The installable-app update check",
        "It runs in the background on signed-in pages, so people didn't see an error page.",
    ),
    "pwa_manifest": (
        "The installable-app description",
        "Browsers fetch it in the background, so people didn't see an error page.",
    ),
    "healthz": (
        "The server's health probe",
        "Only the server's own container check uses it, so people didn't see an error page.",
    ),
    "lot_ledger": ("A home's account page", PEOPLE_SAW),
    "home": ("The dashboard", PEOPLE_SAW),
    "account_login": ("The sign-in page", PEOPLE_SAW),
    "pay_online": ("The Pay online page", PEOPLE_SAW),
}

# Exception class (or the deliberate labels) -> (why, what to do).
CAUSES = {
    errors.HEALTHZ_DB_DOWN: (
        "because the database could not be reached",
        "Check that the database is running (System status → Database).",
    ),
    errors.EMAIL_UNAVAILABLE: (
        "because email sending is down",
        "Check Outgoing email on System status. No action if it stops by itself.",
    ),
    "EmailUnavailable": ("because email sending is down", "Check Outgoing email on System status."),
    "OperationalError": (
        "because the database could not be reached",
        "Check that the database is running (System status → Database).",
    ),
    "InterfaceError": (
        "because the database could not be reached",
        "Check that the database is running (System status → Database).",
    ),
    "TimeoutError": ("because a service the portal relies on did not answer", WAIT),
    "ConnectionError": ("because a service the portal relies on did not answer", WAIT),
    "ConnectionRefusedError": ("because a service the portal relies on did not answer", WAIT),
    "ConnectionResetError": ("because a service the portal relies on did not answer", WAIT),
    "timeout": ("because a service the portal relies on did not answer", WAIT),
    "URLError": ("because a service the portal relies on did not answer", WAIT),
    "SMTPException": ("because email sending is down", "Check Outgoing email on System status."),
    "SMTPServerDisconnected": (
        "because email sending is down",
        "Check Outgoing email on System status.",
    ),
    "ClientError": (
        "because the file store could not be reached",
        "Check File store on System status.",
    ),
    "EndpointConnectionError": (
        "because the file store could not be reached",
        "Check File store on System status.",
    ),
    "PoolKeyError": (
        "because secret ballots waiting to be mixed could not be opened with the server's key",
        "Put the previous DJANGO_SECRET_KEY in DJANGO_SECRET_KEY_FALLBACKS in .env and restart "
        "(docs/runbook.md 'Secrets'); the vote stays open and no ballot is lost until then.",
    ),
    "MemoryError": (
        "because the server ran out of memory",
        "Check Server memory on System status.",
    ),
}

DELIBERATE = {errors.HEALTHZ_DB_DOWN, errors.EMAIL_UNAVAILABLE}


def _menu_labels() -> dict[str, str]:
    """URL name -> the menu label people know (the page's <h1>)."""
    from core.navigation import HUBS, SECTIONS

    out = {}
    for _, items in SECTIONS:
        for label, name, *_ in items:
            out.setdefault(name.split("?")[0], label)
    for pages in HUBS.values():
        for label, name, *_ in pages:
            out.setdefault(name.split("?")[0], label)
    return out


def subject(route: str) -> tuple[str, str]:
    """(What failed, who noticed) for a route name."""
    if route in ROUTES:
        return ROUTES[route]
    label = _menu_labels().get(route)
    if label:
        return f'The "{label}" page', PEOPLE_SAW
    if route:
        return f"A page ({route})", PEOPLE_SAW
    return "A page", PEOPLE_SAW


def cause(exc_type: str, status: int) -> tuple[str, str]:
    """(Why, what to do) for an exception class or a returned status."""
    if exc_type in CAUSES:
        return CAUSES[exc_type]
    if exc_type.startswith("HTTP ") and int(status) in (502, 503, 504):
        return "because the portal reported itself unavailable", WAIT
    return CODE_BUG, CODE_FIX


def sentence(row: dict) -> str:
    """One plain sentence (or three) for a signature row."""
    what, who = subject(row.get("route") or "")
    why, todo = cause(row["exc_type"], row["status"])
    if row["exc_type"] == errors.EMAIL_UNAVAILABLE:
        who = "People saw a page saying their email could not be sent."
    return f"{what} failed {why}. {who} {todo}"
