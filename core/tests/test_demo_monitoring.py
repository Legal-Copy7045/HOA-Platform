"""The demo's synthetic monitoring data (D74): a month of storage figures,
timeline lines and two bounces, added once, with no addresses stored."""

import pytest

from comms.models import EmailBounce
from community.tests.factories import LotFactory, make_member
from core import demo_monitoring, growth, ops_timeline
from core.models import GrowthSnapshot, OpsEvent
from core.scoping import system_access

pytestmark = pytest.mark.django_db


def test_seed_adds_monitoring_data_once():
    for i in range(12):
        make_member(lots=[LotFactory()], email=f"demo-owner-{i}@example.com")
    made = demo_monitoring.seed()
    assert made == {"snapshots": 30, "events": len(demo_monitoring.EVENTS), "bounces": 2}
    assert demo_monitoring.seed() == {"snapshots": 0, "events": 0, "bounces": 0}
    assert GrowthSnapshot.objects.count() == 30
    assert growth.estimate()["days_left"] is not None
    rows, _ = ops_timeline.entries()
    assert len(rows) == len(demo_monitoring.EVENTS)
    assert rows[0].at > rows[-1].at
    with system_access("t"):
        kinds = sorted(EmailBounce.objects.values_list("kind", "mail_kind"))
        assert kinds == [("hard", "notice"), ("soft", "statement")]
        assert not any("@" in b.address_hash for b in EmailBounce.objects.all())
    assert not any("@" in e.message for e in OpsEvent.objects.all())
