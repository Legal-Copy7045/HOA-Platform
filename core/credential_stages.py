"""Stage tracker path (D99) for a change on Keys and connections (D93): names
and states only, never a value."""

from core import credentials as c
from core.workflow import outcome, stage, track


def _state(change) -> str:
    return c.change_state(change).state


def _first(change) -> str:
    if change.approvals_needed:
        return c.PENDING
    return c.WAITING if change.wait_hours else c.ACTIVE


def _due(change):
    cs = c.change_state(change)
    if cs.state == c.PENDING:
        return change.expires_at
    if cs.state == c.WAITING:
        return cs.active_at
    return None


KEY_CHANGE = track(
    "core.credentialchange",
    [
        stage("Saved"),
        stage(
            "Waiting for board approval",
            c.PENDING,
            branch=True,
            who="Another board member: approve or refuse",
        ),
        stage(
            "Waiting period",
            c.WAITING,
            branch=True,
            who="Nobody: it takes effect when the wait ends (it can still be refused)",
        ),
        stage("In use", c.ACTIVE),
    ],
    [
        outcome("Refused", c.REFUSED),
        outcome("Cancelled", c.CANCELLED),
        outcome("Lapsed without approval", c.EXPIRED),
        outcome("Replaced by a newer change", c.SUPERSEDED),
        outcome("Not used: another account", c.OTHER_ACCOUNT),
    ],
    state=_state,
    visited=lambda change: [_first(change), _state(change)],
    due=_due,
)
