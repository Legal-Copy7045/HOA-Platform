"""What the regular monthly charge is called (D40).

The covenants call it an "assessment"; another community may say "dues" or
"fee". The board records the word on Community settings (rule
``charge.term``, with its citation), and every page, email, letter and new
ledger entry takes it from here. Code identifiers, rule keys, URL names and
stored values keep the old "dues" names; old ledger entries keep the text
they were posted with (append-only).

Text that names the charge is written with placeholders, filled by
``term_text()``:

    {term} {terms}   lower case, singular and plural   ("assessment(s)")
    {Term} {Terms}   capitalised                        ("Assessment(s)")
    {TERM} {TERMS}   upper case, for letter headings

Templates use ``{% term "Terms" %}`` or ``{{ text|with_term }}`` (core
templatetags ``portal``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT = "Assessment"
RULE_KEY = "charge.term"

# One or two plain words, optionally "Singular / Plural".
VALID = re.compile(r"[A-Za-z][A-Za-z' -]{0,29}(\s*/\s*[A-Za-z][A-Za-z' -]{0,29})?")

_TOKEN = re.compile(r"\{(term|terms|Term|Terms|TERM|TERMS)\}")


@dataclass(frozen=True)
class Term:
    singular: str  # as the board typed it, e.g. "Assessment"
    plural: str  # e.g. "Assessments"

    def variants(self) -> dict[str, str]:
        return {
            "term": _lower(self.singular),
            "terms": _lower(self.plural),
            "Term": _cap(self.singular),
            "Terms": _cap(self.plural),
            "TERM": self.singular.upper(),
            "TERMS": self.plural.upper(),
        }

    def monthly_title(self, when) -> str:
        """A month's charge: "Assessment - December 2026" with an en dash (WT-003)."""
        return f"{_cap(self.singular)} \N{EN DASH} {when:%B %Y}"


def _cap(word: str) -> str:
    return word[:1].upper() + word[1:]


def _lower(word: str) -> str:
    # Keep acronyms ("HOA fee") as typed; lower only a leading capital.
    first = word.split(" ", 1)[0]
    if len(first) > 1 and first.isupper():
        return word
    return word[:1].lower() + word[1:]


def pluralize(word: str) -> str:
    """English plural for a one- or two-word name ("Dues" stays "Dues")."""
    if not word:
        return word
    lower = word.lower()
    if lower.endswith("s"):
        return word
    if lower.endswith(("sh", "ch", "x", "z")):
        return word + "es"
    if lower.endswith("y") and len(word) > 1 and lower[-2] not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def parse(raw: str | None) -> Term:
    """The recorded value: "Assessment", or "Singular / Plural"."""
    raw = (raw or "").strip()
    if not raw or not VALID.fullmatch(raw):
        raw = DEFAULT
    if "/" in raw:
        singular, plural = (part.strip() for part in raw.split("/", 1))
    else:
        singular, plural = raw, pluralize(raw)
    return Term(singular, plural)


def charge_term() -> Term:
    """The board's word for the monthly charge (default "Assessment")."""
    from community.rules import rule

    return parse(rule(RULE_KEY))


def term_text(text: str, term: Term | None = None) -> str:
    """Fill {term}/{Terms}/... in ``text``; anything else is left as written."""
    if not text or "{" not in text:
        return text
    values = (term or charge_term()).variants()
    return _TOKEN.sub(lambda m: values[m.group(1)], text)
