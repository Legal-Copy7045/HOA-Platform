"""Official notices by email or by post (D44).

The wording an owner agrees to lives here, in one place, with a version.
When the wording changes, bump ``CONSENT_VERSION``: answers already given
keep the version they agreed to (``ContactProfile.notice_consent_version``),
so the association can show exactly what each owner accepted. The board sees
the current wording on Invitations and sign-ups.

Counsel should confirm the wording and the list of notice types before the
portal is used with real owners (see docs/decisions.md, D44).
"""

from __future__ import annotations

CONSENT_VERSION = "2026-11-26"

NOTICE_KINDS = (
    "Notices of owners' meetings and the annual meeting",
    "Budget and assessment notices",
    "Delinquency (late payment) letters",
    "Other notices the Declaration, the Bylaws or Pennsylvania law require the association "
    "to send you",
)

# The paragraphs shown on the sign-up step and on My profile.
CONSENT_TEXT = (
    "Official notices are the formal notices the association must send every owner.",
    "If you agree, we email them to you instead of mailing paper copies. This saves the "
    "association postage and printing.",
    "You can change your choice at any time on My profile. A paper copy of any notice is "
    "still available on request.",
)

YES_LABEL = "Yes, email me official notices"
NO_LABEL = "No, keep sending them by post"


def consent_text() -> str:
    """The whole wording as plain text (for the board's view and exports)."""
    kinds = "\n".join(f"- {k}" for k in NOTICE_KINDS)
    first, *rest = CONSENT_TEXT
    return "\n\n".join([first + "\n" + kinds, *rest])
