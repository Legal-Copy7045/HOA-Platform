"""ARC recusal for affected and neighboring homes (D87, finding R2-003):
the same party test as the request's own home. Owners on the deed, account
contacts (authorized, view-only) and tenants of an affected home today, and
whoever owned it when the request arose, are recused."""

import datetime

import pytest
from django.utils import timezone

from arc import services
from arc.models import ArcRequest, ArcState
from community import services as community
from community.models import Account, ContactKind, Role
from community.tests.factories import LotFactory, days_ago, make_member
from core.policy import SYSTEM
from core.scoping import system_access
from core.workflow import WorkflowError

pytestmark = pytest.mark.django_db


@pytest.fixture
def w():
    lot, neighbor_lot = LotFactory(), LotFactory()
    board = make_member(board=True)
    community.set_rule(key="arc.review_days", value="45", citation="Declaration §9.3", actor=board)
    return {
        "lot": lot,
        "neighbor_lot": neighbor_lot,
        "owner": make_member(lots=[lot]),
        "neighbor_owner": make_member(lots=[neighbor_lot]),
        "reviewer": make_member(roles=[Role.ARC_REVIEWER]),
        "manager": make_member(roles=[Role.MANAGER]),
    }


def _submitted(w):
    req = services.create_request(
        lot=w["lot"], title="Rear fence", description="4 ft black", actor=w["owner"]
    )
    services.ARC.apply(req, "submit", actor=w["owner"])
    services.set_affected_lots(req, [w["neighbor_lot"]], actor=w["manager"])
    return services.ARC.apply(req, "accept", actor=w["manager"])


def _contact_reviewer(w, kind):
    reviewer = make_member(roles=[Role.ARC_REVIEWER])
    with system_access("t"):
        account = Account.objects.active().get(lot=w["neighbor_lot"])
    community.add_account_contact(account, person=reviewer.person, kind=kind, actor=SYSTEM)
    return reviewer


@pytest.mark.parametrize(
    "kind", [ContactKind.AUTHORIZED, ContactKind.VIEW_ONLY, ContactKind.TENANT]
)
def test_contact_or_tenant_of_an_affected_home_is_recused(w, kind):
    req = _submitted(w)
    reviewer = _contact_reviewer(w, kind)
    assert services.is_recused(reviewer, req)
    assert services.deliberation(req, reviewer) is None
    with pytest.raises(services.ArcError, match="recused"):
        services.record_position(req, position="approve", actor=reviewer)
    with pytest.raises(WorkflowError, match="recused"):
        services.decide(req, outcome=ArcState.APPROVED, citation="x", actor=reviewer)
    assert not services.is_recused(w["reviewer"], req)


def test_owner_of_an_affected_home_when_the_request_arose_stays_recused(w):
    reviewer = make_member(roles=[Role.ARC_REVIEWER], lots=[w["neighbor_lot"]])
    req = _submitted(w)
    ArcRequest.objects.unscoped("t").filter(pk=req.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=10)
    )
    req.refresh_from_db()
    buyer = make_member()
    community.transfer_lot(
        w["neighbor_lot"], new_owners=[buyer.person], transfer_date=days_ago(1), actor=SYSTEM
    )
    assert services.is_recused(reviewer, req)  # owned the affected home then
    with pytest.raises(services.ArcError, match="recused"):
        services.record_position(req, position="approve", actor=reviewer)
    # A request made after they sold is not theirs.
    later = services.create_request(
        lot=w["lot"], title="Shed", description="8x10", actor=w["owner"]
    )
    services.ARC.apply(later, "submit", actor=w["owner"])
    services.set_affected_lots(later, [w["neighbor_lot"]], actor=w["manager"])
    assert not services.is_recused(reviewer, later)
