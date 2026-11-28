"""Stage tracker paths (D99) for sign-in resets (two board members, D5) and
self-service account recovery (D58). States and times only: never a code."""

from core.workflow import outcome, stage, track, you_or


def _reset_visited(reset) -> list[str]:
    out = ["awaiting_approval"]
    if reset.approved_at:
        out.append("awaiting_code")
    return [*out, reset.status]


SIGNIN_RESET = track(
    "accounts.mfareset",
    [
        stage("Requested", "awaiting_approval", who="Another board member: approve it"),
        stage(
            "Approved: code handed over",
            "awaiting_code",
            who="The person: set up a new sign-in with the code",
        ),
        stage("Completed", "completed"),
    ],
    [
        outcome("Cancelled", "cancelled"),
        outcome("Lapsed without approval", "expired"),
        outcome("Code expired", "code_expired"),
        outcome("Code locked", "code_locked"),
    ],
    state=lambda reset: reset.status,
    visited=_reset_visited,
    due=lambda reset: (
        reset.request_expires_at
        if reset.status == "awaiting_approval"
        else reset.code_expires_at
        if reset.status == "awaiting_code"
        else None
    ),
)


def _recovery_visited(row) -> list[str]:
    out = ["waiting"]
    if row.completed_at or row.status in ("ready", "expired"):
        out.append("ready")
    return [*out, row.status]


ACCOUNT_RECOVERY = track(
    "accounts.accountrecovery",
    [
        stage(
            "Waiting period",
            "waiting",
            who="Nobody: it can be cancelled from any alert until the wait ends",
        ),
        stage(
            "Ready",
            "ready",
            who=you_or(
                "account_recovery.cancel",
                "The account holder: set up a new sign-in",
                "You: set up a new passkey or app",
            ),
        ),
        stage("Completed", "completed"),
    ],
    [outcome("Cancelled", "cancelled"), outcome("Expired", "expired")],
    state=lambda row: row.status,
    visited=_recovery_visited,
    due=lambda row: (
        row.ready_at
        if row.status == "waiting"
        else row.expires_at
        if row.status == "ready"
        else None
    ),
)
