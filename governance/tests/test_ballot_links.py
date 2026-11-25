"""D24: personal, single-use, signed-in ballot links; casting needs a recent
password or passkey."""

import datetime
import re

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from community.models import Person
from community.tests.factories import LotFactory, make_member
from core.scoping import system_access
from governance import elections
from governance import services as gov
from governance.models import BallotLink

pytestmark = pytest.mark.django_db


def _person(user):
    with system_access("t"):
        return Person.objects.get(user=user)


@pytest.fixture
def w(django_capture_on_commit_callbacks):
    board = make_member(board=True)
    a, b = make_member(lots=[LotFactory()]), make_member(lots=[LotFactory()])
    election = elections.create_election(title="2027 election", seats=1, actor=board)
    elections.nominate(election, person=_person(a), actor=a)
    with django_capture_on_commit_callbacks(execute=True):
        elections.open_voting(election, actor=board)
    return {"board": board, "a": a, "b": b, "e": election}


def _token_for(user):
    message = next(m for m in mail.outbox if m.to == [user.email])
    return re.search(r"/ballot/([A-Za-z0-9_-]{20,100})/", message.body).group(1)


def test_each_owner_gets_their_own_link_and_only_the_hash_is_stored(w):
    token = _token_for(w["a"])
    assert _token_for(w["b"]) != token
    with system_access("t"):
        assert not BallotLink.objects.filter(token_hash=token).exists()
        assert BallotLink.objects.filter(election=w["e"]).count() == 2


def test_link_needs_sign_in_and_only_works_for_its_owner_once(client, w):
    token = _token_for(w["a"])
    url = reverse("ballot_link", args=[token])
    anonymous = client.get(url)
    assert anonymous.status_code == 302 and "/accounts/login/" in anonymous["Location"]
    client.force_login(w["b"])  # forwarded to another owner
    assert client.get(url).status_code == 404
    client.force_login(w["a"])
    response = client.get(url)
    assert response.status_code == 302
    assert response["Location"] == reverse("election_detail", args=[w["e"].pk])
    assert client.get(url).status_code == 404  # single use


def test_link_stops_when_voting_closes(client, w):
    token = _token_for(w["a"])
    elections.close_voting(w["e"], actor=w["board"])
    client.force_login(w["a"])
    assert client.get(reverse("ballot_link", args=[token])).status_code == 404


def test_casting_needs_a_recent_password_or_passkey(client, w):
    w["a"].set_password("a long test passphrase")
    w["a"].save()
    client.force_login(w["a"])  # a session, but no recent re-authentication
    lot = elections.lots_actor_may_vote_for(w["e"], w["a"])[0]["lot"]
    cand = elections.ballot_candidates(w["e"]).get()
    response = client.post(
        reverse("election_action", args=[w["e"].pk, "ballot"]),
        {"lot": lot.pk, "candidates": [cand.pk]},
    )
    assert response.status_code == 302 and "reauthenticate" in response["Location"]
    from governance.models import ElectionParticipation

    assert not ElectionParticipation.objects.filter(election=w["e"]).exists()
    # With a fresh re-authentication the same post is recorded.
    session = client.session
    session["account_authentication_methods_user"] = str(w["a"].pk)
    session["account_authentication_methods"] = [
        {"method": "password", "at": timezone.now().timestamp(), "reauthenticated": True}
    ]
    session.save()
    client.post(
        reverse("election_action", args=[w["e"].pk, "ballot"]),
        {"lot": lot.pk, "candidates": [cand.pk]},
    )
    assert ElectionParticipation.objects.filter(election=w["e"]).exists()


def test_formal_votes_also_send_personal_links(w, django_capture_on_commit_callbacks):
    from community import services as community

    community.set_rule(key="vote.quorum_percent", value="20", citation="Bylaws", actor=w["board"])
    community.set_rule(key="vote.pass_percent", value="51", citation="Bylaws", actor=w["board"])
    vote = gov.create_vote(
        title="Repave",
        kind="formal",
        options=["Yes", "No"],
        closes_at=timezone.now() + datetime.timedelta(days=7),
        actor=w["board"],
    )
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        gov.open_vote(vote, actor=w["board"])
    assert len(mail.outbox) == 2 and "/ballot/" in mail.outbox[0].body
