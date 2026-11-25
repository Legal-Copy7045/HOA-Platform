"""A small, explicit workflow engine.

    INVOICE = Workflow("invoice", [
        Transition("submit", {"draft"}, "pending_approval", "invoice.enter", "Submit"),
        Transition("approve", {"pending_approval"}, "approved", "invoice.approve", "Approve",
                   guard=not_the_entrant),
    ])
    INVOICE.apply(invoice, "approve", actor=request.user, note="", request=request)

Rules:

* the object has a ``state`` CharField; only ``apply`` changes it;
* each transition names its permission (core.policy) and an optional guard
  returning an error message (e.g. separation of duties, recusal);
* every transition writes an append-only WorkflowEvent and an audit event
  ``<workflow>.<action>`` (registered automatically).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from audit import actions as audit_actions
from audit.services import record_event
from core.policy import SYSTEM, can, require


class WorkflowError(ValueError):
    """A transition that is not allowed now. The message is safe to show."""


Guard = Callable[[object, object], str | None]


@dataclass(frozen=True)
class Transition:
    action: str
    sources: frozenset[str] | set[str]
    target: str
    permission: str
    label: str
    guard: Guard | None = None
    note_required: bool = False
    after: Callable[[object, object], None] | None = field(default=None, compare=False)
    # What the confirmation dialog says, naming what happens next (UX review).
    # Steps that close or refuse something get a generic one if this is empty.
    confirm: str = ""


# Every workflow by name, so a history entry can show its step's label.
REGISTRY: dict[str, Workflow] = {}


class Workflow:
    def __init__(self, name: str, transitions: Iterable[Transition]):
        self.name = name
        self.transitions = {t.action: t for t in transitions}
        REGISTRY[name] = self
        for t in self.transitions.values():
            audit_actions.register(
                audit_actions.ActionSpec(
                    f"{name}.{t.action}",
                    f"{name}: {t.label}",
                    frozenset({"object_id", "from", "to"}),
                )
            )

    def _check(self, obj, t: Transition, actor) -> str | None:
        if obj.state not in t.sources:
            return f"Not possible while {obj.state.replace('_', ' ')}."
        if actor is not SYSTEM and not can(actor, t.permission):
            return "You do not have permission for this step."
        if t.guard is not None:
            return t.guard(obj, actor)
        return None

    def available(self, obj, actor) -> list[Transition]:
        """Transitions this actor could take now (for showing buttons)."""
        return [t for t in self.transitions.values() if self._check(obj, t, actor) is None]

    def blocked(self, obj, actor) -> list[tuple[Transition, str]]:
        """Steps that fit the current state and the actor's role, but that a
        rule stops right now, with the reason (shown as a disabled step)."""
        out = []
        for t in self.transitions.values():
            if obj.state not in t.sources or t.guard is None:
                continue
            if actor is not SYSTEM and not can(actor, t.permission):
                continue
            reason = t.guard(obj, actor)
            if reason:
                out.append((t, reason))
        return out

    def apply(self, obj, action: str, *, actor, note: str = "", request=None):
        t = self.transitions.get(action)
        if t is None:
            raise WorkflowError("Unknown action.")
        if actor is not SYSTEM:
            require(actor, t.permission)
        if t.note_required and not note.strip():
            raise WorkflowError("Please add a note explaining this step.")
        from core.models import WorkflowEvent

        with transaction.atomic():
            obj = type(obj)._base_manager.select_for_update().get(pk=obj.pk)
            problem = self._check(obj, t, actor)
            if problem:
                raise WorkflowError(problem)
            old = obj.state
            obj.state = t.target
            fields = ["state"]
            if hasattr(obj, "state_changed_at"):
                obj.state_changed_at = timezone.now()
                fields.append("state_changed_at")
            obj.save(update_fields=fields)
            user = None if actor is SYSTEM else actor
            WorkflowEvent.objects.create(
                workflow=self.name,
                object_id=str(obj.pk),
                action=action,
                from_state=old,
                to_state=t.target,
                actor=user,
                note=note.strip()[:4000],
            )
            record_event(
                f"{self.name}.{action}",
                actor=user,
                request=request,
                target=obj,
                metadata={"object_id": str(obj.pk), "from": old, "to": t.target},
            )
            if t.after is not None:
                t.after(obj, actor)
        return obj

    def history(self, obj):
        from core.models import WorkflowEvent

        return WorkflowEvent.objects.filter(
            workflow=self.name, object_id=str(obj.pk)
        ).select_related("actor")


def not_by(field_name: str, message: str) -> Guard:
    """Guard: the actor must not be the user in ``obj.<field_name>``."""

    def guard(obj, actor):
        if actor is not SYSTEM and getattr(obj, f"{field_name}_id", None) == actor.pk:
            return message
        return None

    return guard
