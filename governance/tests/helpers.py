"""Shared test helpers for governance."""

from governance.models import Vote, VoteKind, VoteOption


def legacy_poll(board, title="Pond fountain?", options=("Yes", "No"), **kw):
    """An informal poll made before polls became surveys (2026-11-26): no new
    ones can be created, but old rows still work and stay readable."""
    vote = Vote.objects.create(title=title, kind=VoteKind.INFORMAL, created_by=board, **kw)
    for i, label in enumerate(options):
        VoteOption.objects.create(vote=vote, label=label, position=i)
    return vote


def window_kw(days: int = 8) -> dict:
    """A voting window that opens now and passes the shortest voting period
    (D61, default 7 days)."""
    import datetime

    from django.utils import timezone

    now = timezone.now()
    return {"opens_at": now, "closes_at": now + datetime.timedelta(days=days)}


def allow_early_close(board) -> None:
    from community import services as community

    community.set_rule(
        key="vote.allow_early_close", value="true", citation="Bylaws §2.9 (test)", actor=board
    )


def close_early(target, board):
    """Close a vote or an election before its deadline, as the board, with
    the rule that allows it switched on (D61)."""
    from governance import elections, services
    from governance.models import Vote

    allow_early_close(board)
    if isinstance(target, Vote):
        return services.close_vote(target, actor=board, reason="Test: closing early")
    return elections.close_voting(target, actor=board, reason="Test: closing early")
