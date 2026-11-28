"""Stage tracker paths (D99) for votes and elections (their own state
machines, governance/window.py). States only: never who chose what."""

from core.workflow import stage, track
from governance.models import ElectionState, VoteStatus

VOTE = track(
    "governance.vote",
    [
        stage("Draft", VoteStatus.DRAFT, who="The board: open the vote"),
        stage("Voting open", VoteStatus.OPEN, who="Owners: vote"),
        stage("Closed", VoteStatus.CLOSED),
    ],
    state=lambda vote: vote.status,
    due=lambda vote: vote.closes_at if vote.status == VoteStatus.OPEN else None,
)

ELECTION = track(
    "governance.election",
    [
        stage(
            "Taking nominations",
            ElectionState.NOMINATIONS,
            who="The board: open voting once nominations are in",
        ),
        stage("Voting open", ElectionState.VOTING, who="Owners: vote"),
        stage("Voting closed", ElectionState.CLOSED, who="The board: certify the results"),
        stage("Results certified", ElectionState.CERTIFIED),
    ],
    due=lambda e: e.voting_closes_at if e.state == ElectionState.VOTING else None,
)
