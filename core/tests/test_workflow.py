"""The shared workflow engine, exercised with a throwaway model-free stand-in."""

import pytest
from django.core.exceptions import PermissionDenied

from audit.models import AuditEvent
from community.models import Role
from community.tests.factories import make_member
from core.models import WorkflowEvent
from core.policy import SYSTEM
from core.workflow import Transition, Workflow, WorkflowError, not_by

pytestmark = pytest.mark.django_db


# apply() is exercised end to end by the invoice, ARC and violation tests,
# which drive real models; here we check the decision logic directly.


FLOW = Workflow(
    "testflow",
    [
        Transition("submit", {"draft"}, "pending", "invoice.enter", "Submit"),
        Transition(
            "approve",
            {"pending"},
            "approved",
            "invoice.approve",
            "Approve",
            guard=not_by("entered_by", "You entered this; someone else must approve."),
        ),
    ],
)


class Obj:
    def __init__(self, state, entered_by=None):
        self.state = state
        self.entered_by_id = entered_by.pk if entered_by else None


def test_available_respects_state_permission_and_guard():
    board = make_member(board=True)
    other = make_member(board=True)
    manager = make_member(roles=[Role.MANAGER])
    draft = Obj("draft", entered_by=manager)
    assert [t.action for t in FLOW.available(draft, manager)] == ["submit"]
    pending = Obj("pending", entered_by=board)
    assert FLOW.available(pending, board) == []  # guard: entrant cannot approve
    assert [t.action for t in FLOW.available(pending, other)] == ["approve"]
    assert FLOW.available(pending, manager) == []  # no permission


def test_workflow_events_are_append_only():
    event = WorkflowEvent.objects.create(
        workflow="x", object_id="1", action="a", from_state="s", to_state="t"
    )
    from core.append_only import AppendOnlyError

    with pytest.raises(AppendOnlyError):
        event.delete()


def test_actions_are_registered_for_audit():
    from audit.actions import ACTIONS

    assert "testflow.approve" in ACTIONS


def test_unknown_action_and_permission_errors():
    with pytest.raises(WorkflowError):
        FLOW.apply(Obj("draft"), "explode", actor=SYSTEM)
    with pytest.raises(PermissionDenied):
        FLOW.apply(Obj("draft"), "submit", actor=make_member())
    assert not AuditEvent.objects.filter(action__startswith="testflow").exists()
