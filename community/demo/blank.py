"""The ``blank`` demo dataset: an empty portal (owner's request, 0.45.0, D98).

    python manage.py seed_demo --dataset blank

No homes, no owners, no board, no documents and no history: only the real
logins a reseed keeps (``keep_logins``: the sysadmins and other non-demo
logins, with their passkeys and authenticator apps). Community settings stay
at their defaults, so the portal opens on the Community setup wizard and the
register import, as a new community's would. Fresh start is the dataset with
44 homes, their owners and a declarant board.
"""

from __future__ import annotations

from community.demo import SeedResult


def seed() -> SeedResult:
    # Nothing to create: the reseed emptied the database and put the real
    # logins back before this runs.
    return SeedResult(
        summary=(
            "Blank portal: no homes, owners, board or history; settings at their defaults. "
            "Sign in with your own login and start with the setup wizard and the register "
            "import."
        )
    )
