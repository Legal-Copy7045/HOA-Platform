"""Shared test helpers for governance."""

from governance.models import Vote, VoteKind, VoteOption


def legacy_poll(board, title="Pond fountain?", options=("Yes", "No"), **kw):
    """An informal poll made before polls became surveys (2026-11-26): no new
    ones can be created, but old rows still work and stay readable."""
    vote = Vote.objects.create(title=title, kind=VoteKind.INFORMAL, created_by=board, **kw)
    for i, label in enumerate(options):
        VoteOption.objects.create(vote=vote, label=label, position=i)
    return vote
