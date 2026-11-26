"""Shared fixtures: the Stripe fake and the two-lot world from test_stripe,
and the autopay world (``aw``) used by test_autopay*.py."""

import datetime

import pytest
from django.utils import timezone

from community import services as community
from core.models import FeatureSetting
from payments import autopay
from payments.tests.test_stripe import fake, w  # noqa: F401

DUE_DAY = 5


def _next_due() -> datetime.date:
    today = timezone.localdate()
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return datetime.date(year, month, DUE_DAY)


@pytest.fixture
def aw(w, monkeypatch):  # noqa: F811 (w is the fixture imported above)
    """The two-lot world of test_stripe, with dues, the feature on, and the
    calendar moved to next month's due day (enrolment always happens before
    it)."""
    board = w["board"]
    community.set_rule(key="dues.monthly_cents", value="20000", citation="Decl. 8.1", actor=board)
    community.set_rule(key="dues.due_day", value=str(DUE_DAY), citation="Decl. 8.2", actor=board)
    FeatureSetting.objects.create(key="autopay", enabled=True)
    due = _next_due()
    monkeypatch.setattr(autopay, "_today", lambda: due)
    return {**w, "due": due, "period": f"{due:%Y-%m}"}
