"""The home picker (D96): pick a home by its street address, with a type-ahead.

One widget for every "pick a home" field. People type part of an address
(or, as a fallback, a plat lot number) and pick from the suggestions; the
form stores the lot as before. Without JavaScript it is a plain text box
that takes an exact address or lot number and, when the text does not name
exactly one home, says so and lists the closest addresses.

Owner decision (D96): homes and their owners are public within the
community, so the suggestions show every home's address and its current
owners' names to any signed-in portal user (not a former owner's read-only
login). What a form accepts is still its own queryset: the suggestions only
help typing, the form decides.
"""

from __future__ import annotations

import difflib
import re

from django import forms
from django.core.cache import cache
from django.core.exceptions import ValidationError

from community.models import Lot, home_sort_key
from core.scoping import system_access

MIN_CHARS = 2
MAX_RESULTS = 10
MAX_QUERY = 100
# Suggestions per login per ten minutes (each keystroke after a pause is one).
RATE_LIMIT = 600
RATE_WINDOW = 600
SEPARATORS = re.compile(r"[;\n]+")
_LOT_TEXT = re.compile(r"^(?:lot\s*#?\s*)?(\d{1,5})$", re.IGNORECASE)
_LEGAL = re.compile(r"^(.*)\(\s*lot\s*(\d{1,5})\s*\)$", re.IGNORECASE)


def _norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def all_homes() -> list[Lot]:
    """Every home in address order. Addresses are public in the community (D96)."""
    with system_access("home picker: addresses are public (D96)"):
        return sorted(Lot.objects.all(), key=home_sort_key)


def owner_names(lot_ids) -> dict[int, str]:
    """Current owners on the deed per home ("Pat & Sam Example"); never
    tenants, contacts or contact details (D96)."""
    from community.models import Ownership

    out: dict[int, list[str]] = {}
    with system_access("home picker: owners are public (D96)"):
        rows = (
            Ownership.objects.active()
            .filter(lot_id__in=list(lot_ids))
            .select_related("person")
            .order_by("start_date", "pk")
        )
        for o in rows:
            out.setdefault(o.lot_id, []).append(o.person.display_name)
    return {k: " & ".join(v) for k, v in out.items()}


def _duplicates(lots) -> set[str]:
    seen: dict[str, int] = {}
    for lot in lots:
        key = _norm(lot.label)
        seen[key] = seen.get(key, 0) + 1
    return {k for k, n in seen.items() if n > 1}


def picker_value(lot, duplicates: set[str] | None = None) -> str:
    """The text a picker shows for a chosen home: its address, or
    "<address> (Lot N)" in the rare case two homes share an address."""
    if lot is None:
        return ""
    if duplicates is None:
        duplicates = _duplicates(all_homes())
    return lot.legal_label if _norm(lot.label) in duplicates else lot.label


def picker_values(lots) -> str:
    dup = _duplicates(all_homes())
    return "; ".join(picker_value(lot, dup) for lot in lots)


def may_suggest(user) -> bool:
    """Any signed-in portal user with a current community role: owners,
    account contacts, tenants, committee members, the board and staff. A
    former owner's read-only login sees only their own history, and the
    sysadmin role alone gives no owner data (operations pages only), so
    neither gets suggestions."""
    from core.policy import FORMER_OWNER, SYSADMIN, roles_for

    if not getattr(user, "is_authenticated", False):
        return False
    groups = roles_for(user).groups
    return bool(groups - {FORMER_OWNER, SYSADMIN})


def rate_limited(user) -> bool:
    key = f"home-picker:{user.pk}"
    cache.add(key, 0, timeout=RATE_WINDOW)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=RATE_WINDOW)
        count = 1
    return count > RATE_LIMIT


def _rank(lot, needle: str, number: int | None, owners: str) -> int | None:
    label = _norm(lot.label)
    if number is not None and lot.number == number:
        return 0
    if label.startswith(needle):
        return 1
    if any(word.startswith(needle) for word in label.split()):
        return 2
    if needle in label:
        return 3
    if needle in _norm(lot.legal_label):
        return 4
    if owners and needle in owners.lower():
        return 5
    return None


def matching(query: str, lots, names: dict[int, str] | None = None) -> list[Lot]:
    """Homes whose address (or lot number, or current owners) matches, best first."""
    needle = _norm(query)[:MAX_QUERY]
    if not needle:
        return []
    m = _LOT_TEXT.match(needle)
    number = int(m.group(1)) if m else None
    names = names or {}
    ranked = []
    for lot in lots:
        r = _rank(lot, needle, number, names.get(lot.pk, ""))
        if r is not None:
            ranked.append((r, home_sort_key(lot), lot))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in ranked]


def suggestions(query: str, limit: int = MAX_RESULTS) -> list[dict]:
    """At most ``limit`` homes for the type-ahead: value, address, owners."""
    query = (query or "").strip()[:MAX_QUERY]
    if len(query) < MIN_CHARS:
        return []
    lots = all_homes()
    names = owner_names([lot.pk for lot in lots])
    dup = _duplicates(lots)
    return [
        {
            "value": picker_value(lot, dup),
            "label": lot.label,
            "owners": names.get(lot.pk, ""),
        }
        for lot in matching(query, lots, names)[:limit]
    ]


# --- Resolving typed text to a home ----------------------------------------------------


class HomeNotFound(ValueError):
    """The text names no home, or several; ``close`` are the closest addresses."""

    def __init__(self, text: str, close: list[str], ambiguous: bool = False):
        self.text = text
        self.close = close
        self.ambiguous = ambiguous
        super().__init__(self.message)

    @property
    def message(self) -> str:
        shown = self.text if len(self.text) <= 60 else self.text[:57] + "..."
        if self.ambiguous:
            head = f'"{shown}" matches more than one home.'
        else:
            head = f'No home matches "{shown}".'
        if self.close:
            close = "; ".join(self.close)
            return f"{head} Did you mean: {close}? Pick one or type the full address."
        return f"{head} Type the home's street address."


def resolve(text: str, lots) -> Lot | None:
    """The one home in ``lots`` that ``text`` names: the exact address,
    "<address> (Lot N)", a lot number or "Lot N". Blank gives None; anything
    else raises HomeNotFound with the closest addresses."""
    text = " ".join((text or "").split())[:MAX_QUERY]
    if not text:
        return None
    lots = list(lots)
    dup = _duplicates(lots)
    m = _LOT_TEXT.match(text)
    if m:
        found = [lot for lot in lots if lot.number == int(m.group(1))]
        if found:
            return found[0]
    legal = _LEGAL.match(text)
    if legal:
        found = [
            lot
            for lot in lots
            if lot.number == int(legal.group(2)) and _norm(lot.label) == _norm(legal.group(1))
        ]
        if found:
            return found[0]
    exact = [lot for lot in lots if _norm(lot.label) == _norm(text)]
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise HomeNotFound(text, [lot.legal_label for lot in exact], ambiguous=True)
    close = matching(text, lots)[:5]
    if not close:
        by_label = {lot.label: lot for lot in lots}
        hits = difflib.get_close_matches(text, list(by_label), n=5, cutoff=0.5)
        close = [by_label[h] for h in hits]
    raise HomeNotFound(text, [picker_value(lot, dup) for lot in close])


def split_many(text: str) -> list[str]:
    """Addresses separated by ";" or new lines; "14 15" (lot numbers only,
    as typed before D96) is two homes."""
    parts = []
    for part in SEPARATORS.split(text or ""):
        part = part.strip().strip(",").strip()
        if not part:
            continue
        if re.fullmatch(r"\d{1,5}(?:[\s,]+\d{1,5})+", part):
            parts.extend(re.split(r"[\s,]+", part))
        else:
            parts.append(part)
    return parts


def resolve_many(text: str, lots) -> tuple[list[Lot], list[str]]:
    """Every home the text names (no repeats) and an error per part that
    names none or several."""
    lots = list(lots)
    out: list[Lot] = []
    errors: list[str] = []
    for part in split_many(text)[:60]:
        try:
            lot = resolve(part, lots)
        except HomeNotFound as exc:
            errors.append(exc.message)
            continue
        if lot is not None and lot not in out:
            out.append(lot)
    return out, errors


# --- Form widget and fields ---------------------------------------------------------------


class HomePicker(forms.TextInput):
    """A text box with the address type-ahead (static/js/home-picker.js)."""

    template_name = "widgets/home_picker.html"
    multiple = False

    def __init__(self, attrs=None):
        base = {"autocomplete": "off", "maxlength": "200" if not self.multiple else "4000"}
        base.update(attrs or {})
        super().__init__(base)

    def get_context(self, name, value, attrs):
        ctx = super().get_context(name, value, attrs)
        ctx["widget"]["multiple"] = self.multiple
        return ctx


class HomesPicker(HomePicker):
    """Several homes: removable chips with JavaScript, "; "-separated without."""

    multiple = True


class HomeField(forms.Field):
    """Like ModelChoiceField over Lot, typed as an address (D96)."""

    widget = HomePicker
    default_error_messages = {"required": "Choose a home: type part of its address."}

    def __init__(self, *, queryset=None, **kwargs):
        self.queryset = queryset if queryset is not None else Lot.objects.none()
        kwargs.setdefault("help_text", "Type part of the street address and pick the home.")
        super().__init__(**kwargs)

    def prepare_value(self, value):
        if isinstance(value, Lot):
            return picker_value(value)
        # An initial pk (int) only: typed text such as "12" is a lot number.
        if isinstance(value, int) and not isinstance(value, bool):
            with system_access("home picker: show the chosen home"):
                lot = Lot.objects.filter(pk=int(value)).first()
            return picker_value(lot) if lot else ""
        return value

    def to_python(self, value):
        try:
            return resolve(value, self.queryset)
        except HomeNotFound as exc:
            raise ValidationError(exc.message, code="invalid_choice") from None


class HomesField(HomeField):
    widget = HomesPicker
    default_error_messages = {"required": "Choose at least one home."}

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return picker_values(v for v in value if isinstance(v, Lot))
        return super().prepare_value(value)

    def to_python(self, value):
        lots, errors = resolve_many(value or "", self.queryset)
        if errors:
            raise ValidationError(errors, code="invalid_choice")
        return lots

    def validate(self, value):
        if self.required and not value:
            raise ValidationError(self.error_messages["required"], code="required")
