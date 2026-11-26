"""One search box for the whole portal (Milestone 5).

Search must never show anything the signed-in person could not already open.
So this module decides nothing about access itself:

* each area registers a provider ``fn(user, query) -> list[Result]`` here
  (the providers live next to their area, in ``<app>/search.py``);
* a provider checks the same permission as the area's list page and builds
  its rows from the same scoped queryset that page uses
  (``Model.objects.for_user(user)``, ``owner_era_q``, thread access rules);
* an area whose page is switched off (core.features) is skipped.

Privacy: the query text is never logged, audited or stored, and the results
page is sent with ``Cache-Control: no-store``.
"""

from __future__ import annotations

import datetime
import importlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.db.models import Q

from core import features
from core.text import single_line

MIN_LENGTH = 2
MAX_LENGTH = 100
PER_AREA = 10
LIMIT = PER_AREA + 1  # rows a provider fetches per query: one extra shows "more"
SNIPPET_BEFORE = 60
SNIPPET_LENGTH = 180

# Modules that register providers, loaded on first use. Keep the order: it is
# the order of the groups on the results page.
PROVIDER_MODULES = (
    "comms.search",
    "documents.search",
    "governance.search",
    "community.search",
    "arc.search",
    "compliance.search",
    "operations.search",
)


@dataclass(frozen=True)
class Result:
    title: str
    url: str
    snippet: str = ""
    date: datetime.date | datetime.datetime | None = None
    detail: str = ""  # a short plain label, e.g. "Lot 12" or "Message"


@dataclass(frozen=True)
class Area:
    key: str
    label: str
    url_name: str  # the area's list page: its feature switch gates the area
    provider: Callable[[object, str], list[Result]]
    soonest_first: bool = False  # upcoming events: the next one first


@dataclass
class AreaResults:
    area: Area
    results: list[Result]
    more: bool = False
    parts: list = field(default_factory=list)


AREAS: dict[str, Area] = {}
_loaded = [False]


def register(key: str, label: str, *, url_name: str, soonest_first: bool = False):
    """Decorator: register ``fn(user, query) -> list[Result]`` for an area.

    A provider returns at most ``LIMIT`` results (one more than are shown, so
    the page can say there are more)."""

    def decorator(fn):
        if key in AREAS and AREAS[key].provider is not fn:
            raise ValueError(f"search area {key!r} registered twice")
        AREAS[key] = Area(key, label, url_name, fn, soonest_first)
        return fn

    return decorator


def load_providers() -> dict[str, Area]:
    if not _loaded[0]:
        for name in PROVIDER_MODULES:
            importlib.import_module(name)
        _loaded[0] = True
    return AREAS


# --- Query handling ----------------------------------------------------------------------


def clean_query(raw: str | None) -> str:
    """One line, single spaces; never longer than the limit is searched."""
    return single_line(raw or "")


def query_problem(q: str) -> str | None:
    if len(q) < MIN_LENGTH:
        return "short"
    if len(q) > MAX_LENGTH:
        return "long"
    return None


def matching(q: str, *fields: str) -> Q:
    """Rows where any of ``fields`` contains ``q`` (case-insensitive)."""
    condition = Q(pk__in=[])
    for name in fields:
        condition |= Q(**{f"{name}__icontains": q})
    return condition


def contains(q: str, *values) -> bool:
    needle = q.casefold()
    return any(needle in str(v or "").casefold() for v in values)


def snippet(q: str, *texts) -> str:
    """Plain text around the first match in ``texts`` (or the start of the
    first non-empty text). Escaping happens in the template, never here."""
    plain = [" ".join(str(t or "").split()) for t in texts]
    plain = [t for t in plain if t]
    if not plain:
        return ""
    pattern = re.compile(re.escape(q), re.IGNORECASE)
    for text in plain:
        found = pattern.search(text)
        if found:
            start = max(0, found.start() - SNIPPET_BEFORE)
            if start:
                space = text.find(" ", start)
                start = space + 1 if 0 <= space < found.start() else start
            piece = text[start : start + SNIPPET_LENGTH]
            return (
                ("…" if start else "") + piece + ("…" if start + SNIPPET_LENGTH < len(text) else "")
            )
    text = plain[0]
    return text[:SNIPPET_LENGTH] + ("…" if len(text) > SNIPPET_LENGTH else "")


def highlight_parts(text: str, q: str) -> list[tuple[str, bool]]:
    """``text`` split into (piece, is_match) pairs, for the template to
    escape each piece and wrap matches in <mark>."""
    if not text:
        return []
    if not q:
        return [(text, False)]
    parts: list[tuple[str, bool]] = []
    pos = 0
    for found in re.finditer(re.escape(q), text, re.IGNORECASE):
        if found.start() > pos:
            parts.append((text[pos : found.start()], False))
        parts.append((found.group(0), True))
        pos = found.end()
    if pos < len(text):
        parts.append((text[pos:], False))
    return parts


def _sort_key(result: Result):
    value = result.date
    if value is None:
        return datetime.datetime.min.replace(tzinfo=datetime.UTC)
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)
    return datetime.datetime(value.year, value.month, value.day, tzinfo=datetime.UTC)


def newest_first(results: Iterable[Result], *, soonest: bool = False) -> list[Result]:
    return sorted(results, key=_sort_key, reverse=not soonest)


# --- Threads -----------------------------------------------------------------------------


def thread_results(kind: str, subjects, user, q: str, *, title, limit: int = LIMIT):
    """Messages on the threads of ``subjects`` that contain ``q``.

    ``subjects`` must already be the scoped queryset of what ``user`` may
    open. Internal notes appear only where comms.threads gives this user
    internal access to that subject (staff, and never a recused reviewer).
    """
    from django.urls import reverse

    from comms import threads
    from comms.models import ThreadMessage
    from core import policy

    view_internal = policy.can(user, threads.STAFF[kind][0])
    rows = ThreadMessage.objects.filter(
        subject_kind=kind, subject_id__in=subjects.values("pk"), body__icontains=q
    ).order_by("-created_at", "-id")
    if not view_internal:
        rows = rows.filter(internal=False)
    rows = list(rows[: limit * 5])
    if not rows:
        return []
    visible = {obj.pk: obj for obj in subjects.filter(pk__in={m.subject_id for m in rows})}
    access: dict[int, object] = {}
    out: list[Result] = []
    for message in rows:
        obj = visible.get(message.subject_id)
        if obj is None:
            continue
        if obj.pk not in access:
            access[obj.pk] = threads.access(kind, obj, user)
        allowed = access[obj.pk]
        if not allowed.read or (message.internal and not allowed.read_internal):
            continue
        out.append(
            Result(
                title=title(obj),
                url=reverse(threads.DETAIL_URLS[kind], args=[obj.pk]) + "#thread",
                snippet=snippet(q, message.body),
                date=message.created_at,
                detail="Internal note" if message.internal else "Message",
            )
        )
        if len(out) >= limit:
            break
    return out


# --- Running a search --------------------------------------------------------------------


def search(user, query: str, request=None) -> list[AreaResults]:
    """Every area this person may search, with its results (empty areas left out)."""
    q = clean_query(query)
    if query_problem(q):
        return []
    from core import policy

    if not policy.roles_for(user).groups:
        return []
    out = []
    for area in load_providers().values():
        if not features.url_name_enabled(area.url_name, request):
            continue
        found = newest_first(area.provider(user, q) or [], soonest=area.soonest_first)
        if not found:
            continue
        group = AreaResults(area, found[:PER_AREA], more=len(found) > PER_AREA)
        group.parts = [
            (r, highlight_parts(r.title, q), highlight_parts(r.snippet, q)) for r in group.results
        ]
        out.append(group)
    return out
