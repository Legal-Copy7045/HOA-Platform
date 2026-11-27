"""A candidate on the board does not choose when their own election runs
(D87, finding BUG-003): opening, extending and closing voting are refused
and audited, as certifying already was; another board member does them."""

import datetime

import pytest
from django.utils import timezone

from audit.models import AuditEvent
from community.models import Person
from community.tests.factories import LotFactory, make_member
from core import conflicts
from core.scoping import system_access
from governance import elections, window
from governance import services as gov
from governance.tests.helpers import allow_early_close, window_kw

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


@pytest.fixture
def w():
    board = make_member(board=True)
    candidate = make_member(board=True, lots=[LotFactory()])
    other = make_member(lots=[LotFactory()])
    meeting = gov.create_meeting(
        title="Annual meeting",
        kind="annual",
        location="Clubhouse",
        starts_at=timezone.now() + datetime.timedelta(days=20),
        actor=board,
    )
    e = elections.create_election(title="2027 election", seats=1, meeting=meeting, actor=board)
    elections.nominate(e, person=_person(candidate), actor=candidate)
    elections.nominate(e, person=_person(other), actor=other)
    return {"board": board, "candidate": candidate, "e": e}


def _refused(action):
    return AuditEvent.objects.filter(
        action=conflicts.AUDIT_ACTION, metadata__area="election", metadata__action=action
    ).count()


def test_a_candidate_cannot_open_their_election(w):
    with pytest.raises(elections.ElectionError, match="candidate"):
        elections.open_voting(w["e"], actor=w["candidate"], **window_kw())
    assert _refused("open") == 1
    elections.open_voting(w["e"], actor=w["board"], **window_kw())


def test_a_candidate_cannot_extend_their_election(w):
    e = elections.open_voting(w["e"], actor=w["board"], **window_kw())
    later = e.voting_closes_at + datetime.timedelta(days=14)
    with pytest.raises(window.WindowError, match="candidate"):
        window.extend(e, closes_at=later, reason="Supporters away", actor=w["candidate"])
    assert _refused("extend") == 1
    e.refresh_from_db()
    assert e.extended_at is None
    window.extend(e, closes_at=later, reason="Ballot printing delay", actor=w["board"])


def test_a_candidate_cannot_close_their_election_early(w):
    allow_early_close(w["board"])
    e = elections.open_voting(w["e"], actor=w["board"], **window_kw())
    with pytest.raises(elections.ElectionError, match="candidate"):
        elections.close_voting(e, actor=w["candidate"], reason="Enough votes")
    assert _refused("close") == 1
    e.refresh_from_db()
    assert e.state == "voting"
    elections.close_voting(e, actor=w["board"], reason="Meeting moved")
