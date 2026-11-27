"""CLAUDE.md stays short and loses nothing (D90, the owner's standard section 4).

It is loaded into every Claude session, so it keeps only what every change
needs (under 8 KB) and lists `docs/claude/<topic>.md` for the rest. When it was
cut down, every line moved verbatim into those files; this test keeps that
true: every backticked name and decision reference of the old CLAUDE.md
(core/tests/data/claude_md_tokens.txt) is still in CLAUDE.md or docs/claude/.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CLAUDE = ROOT / "CLAUDE.md"
DOCS = ROOT / "docs" / "claude"

NEVER = [
    "- Never store or request real owner personal data, real Stripe keys, or production secrets "
    "in this environment.",
    "- Never disable authorization, MFA, or audit logging to make a test or demo pass.",
    "- Never edit or delete financial, decision, vote, or audit records in place.",
    "- Never weaken a security setting without explaining the trade-off and getting approval "
    "first.",
    "- Never expose Postgres or the object store to the public internet.",
]


def _tokens() -> list[str]:
    path = Path(__file__).parent / "data" / "claude_md_tokens.txt"
    return [ln for ln in path.read_text().splitlines() if ln and not ln.startswith("# ")]


def test_claude_md_stays_under_8_kb():
    assert len(CLAUDE.read_bytes()) < 8192


def test_never_section_is_kept_verbatim():
    text = CLAUDE.read_text()
    section = text.split("## Never\n", 1)[1]
    for line in NEVER:
        assert line in section


def test_every_reference_file_is_listed_and_every_listed_file_exists():
    text = CLAUDE.read_text()
    listed = set(re.findall(r"^- `([a-z-]+\.md)`:", text, re.M))
    present = {p.name for p in DOCS.glob("*.md")}
    assert listed == present


def test_nothing_of_the_old_claude_md_was_lost():
    text = CLAUDE.read_text() + "\n".join(p.read_text() for p in sorted(DOCS.glob("*.md")))
    tokens = _tokens()
    assert len(tokens) > 800
    missing = [t for t in tokens if t not in text]
    assert not missing, f"no longer in CLAUDE.md or docs/claude/: {missing[:20]}"
