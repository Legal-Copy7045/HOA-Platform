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

import sys
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


class NotForYou(str):
    """A guard's reason that will not change for this person (say, they are
    not an owner of the home): the step is refused like any other, but
    ``Workflow.blocked()`` leaves it out instead of showing it disabled,
    since it is never theirs to take (WT-015)."""


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
    def __init__(
        self, name: str, transitions: Iterable[Transition], *, stages: Stages | None = None
    ):
        self.name = name
        self.transitions = {t.action: t for t in transitions}
        # Where it is declared (the D99 guard test skips tests' stand-ins).
        self.module = sys._getframe(1).f_globals.get("__name__", "")
        REGISTRY[name] = self
        # The stage tracker (D99): the declared path, or one derived from the
        # transitions (a guard test makes every workflow declare its own).
        self.stages = stages or Stages.derive(self)
        self.stages.workflow = self
        if self.stages.model:
            STAGES[self.stages.model] = self.stages
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
        own = self._own_matter(obj, t, actor)
        if own:
            return own
        if t.guard is not None:
            return t.guard(obj, actor)
        return None

    @staticmethod
    def _own_matter(obj, t: Transition, actor) -> str | None:
        """Own matters (D65): a staff-only step on the actor's own home's
        matter (or one they raised) is another board member's to take."""
        from core import conflicts

        if actor is SYSTEM or not conflicts.staff_permission(t.permission):
            return None
        if conflicts.own_matter(actor, obj):
            return conflicts.OwnMatter(conflicts.note_for(obj, actor))
        return None

    def available(self, obj, actor) -> list[Transition]:
        """Transitions this actor could take now (for showing buttons)."""
        return [t for t in self.transitions.values() if self._check(obj, t, actor) is None]

    def blocked(self, obj, actor) -> list[tuple[Transition, str]]:
        """Steps that fit the current state and the actor's role, but that a
        rule stops right now, with the reason (shown as a disabled step).
        Steps the person could never take (a ``NotForYou`` reason) are left
        out, so "Next steps" shows only what they may do later (WT-015)."""
        out = []
        for t in self.transitions.values():
            if obj.state not in t.sources:
                continue
            if actor is not SYSTEM and not can(actor, t.permission):
                continue
            # One's own matter (D65) shows the staff steps disabled, with why.
            reason = self._own_matter(obj, t, actor)
            if not reason and t.guard is not None:
                reason = t.guard(obj, actor)
            if reason and not isinstance(reason, NotForYou):
                out.append((t, reason))
        return out

    def apply(self, obj, action: str, *, actor, note: str = "", request=None):
        t = self.transitions.get(action)
        if t is None:
            raise WorkflowError("Unknown action.")
        if actor is not SYSTEM:
            require(actor, t.permission)
        own = self._own_matter(obj, t, actor)
        if own:
            # Audited before the step's transaction, so the record stays (D65).
            from core import conflicts

            conflicts.audit_refusal(actor, obj, area=self.name, action=action, request=request)
            raise WorkflowError(own)
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


# --- Who is acting (D64) ------------------------------------------------------------------
# Decisions taken for the association: their buttons say "... for the board"
# (for a board member) and their confirmation says the person acts for the
# association. Used by partials/workflow.html and, through the
# {% for_board %} / {% acting_confirm %} tags, by the pages with their own
# decision forms (invoices, ARC, autopay review, disbursements, account
# changes, second approvals), so the wording is the same everywhere.
DECISION_ACTIONS = frozenset(
    {
        "approve",
        "approve_with_conditions",
        "deny",
        "reject",
        "decline",
        "decide",
        "decide_appeal",
        "uphold",
        "dismiss",
        "mark_cured",
        "void",
        "adopt",
        "certify",
        "issue",
    }
)
ACTING = "You're acting for the association."
OWN_HOME_NOTE = (
    "This is about your own home, so another board member must decide it. You can read it"
    " and follow it like any owner."
)


def for_board(label: str, actor=None) -> str:
    """A decision button's label: "Approve for the board" for a board member
    (staff who are not on the board keep the plain label: they never act
    for the board, policy.MANAGER)."""
    from core import policy

    # "Mark cured (board)" said the same thing in brackets.
    label = label.removesuffix(" (board)")
    if actor is not None and policy.BOARD not in policy.roles_for(actor).groups:
        return label
    return f"{label} for the board"


def acting_confirm(text: str = "") -> str:
    """A decision's confirmation text, ending with who is acting."""
    text = (text or "").strip()
    return f"{text} {ACTING}" if text else ACTING


def is_decision(action: str) -> bool:
    return action in DECISION_ACTIONS


def not_by(field_name: str, message: str) -> Guard:
    """Guard: the actor must not be the user in ``obj.<field_name>``."""

    def guard(obj, actor):
        if actor is not SYSTEM and getattr(obj, f"{field_name}_id", None) == actor.pk:
            return message
        return None

    return guard


# --- Where an item is in its workflow: the stage tracker (D99) ---------------------------
#
# Each workflow declares its main path (the ordered stages of the usual way
# through) and its side outcomes (Withdrawn, Denied, Cancelled...). Pages draw
# the strip from that declaration with {% workflow_stages obj %}
# (templates/partials/stages.html), never by hand, so it cannot drift from
# the transitions. Workflows on core.workflow pass ``stages=`` to Workflow;
# other state machines (an append-only model whose state is derived from its
# decisions) declare theirs with ``track()`` in a module listed in
# STAGE_MODULES. The tracker shows states only, never notes: it adds nothing
# the page does not already show the viewer (the status badge).

StateFn = Callable[[object], str]
WhoFn = Callable[[object, object], str]


@dataclass(frozen=True)
class Stage:
    """One stage of the main path.

    ``states``: the item's states that are this stage. ``branch``: off the
    usual way (neighbor acknowledgements, an appeal, a payment plan): shown
    only once the item is in it or has been. ``staff_only``: a stage people
    outside the work never see (a violation before its notice), left out for
    a viewer without the path's ``staff_permission``. ``who``: who acts while
    the item is here ("The committee"), or a function (obj, viewer) -> text,
    for the "Next:" line.
    """

    label: str
    states: frozenset[str]
    branch: bool = False
    staff_only: bool = False
    who: str | WhoFn = ""


@dataclass(frozen=True)
class Outcome:
    """A side outcome: the item stopped off the main path (Withdrawn).
    ``who``: who may still act on it (a denied request's appeal), if anyone."""

    label: str
    states: frozenset[str]
    who: str | WhoFn = ""


def stage(label: str, *states: str, branch=False, staff_only=False, who: str | WhoFn = "") -> Stage:
    return Stage(label, frozenset(str(s) for s in states), branch, staff_only, who)


def outcome(label: str, *states: str, who: str | WhoFn = "") -> Outcome:
    return Outcome(label, frozenset(str(s) for s in states), who)


def you_or(staff_permission: str, others: str, you: str = "You") -> WhoFn:
    """A "Next:" actor that reads "You" for the person the item belongs to
    and ``others`` ("The owner") for staff with ``staff_permission``."""

    def who(obj, viewer) -> str:
        if viewer is not None and can(viewer, staff_permission):
            return others
        return you

    return who


# model label ("arc.arcrequest") -> its declared path
STAGES: dict[str, Stages] = {}
# Modules that declare paths (Workflows with ``stages=`` and track()):
# loaded on the first lookup, so every path is known wherever it is asked.
STAGE_MODULES = (
    "arc.services",
    "compliance.services",
    "governance.services",
    "ledger.services",
    "operations.services",
    "operations.stages",
    "ledger.stages",
    "governance.stages",
    "accounts.stages",
    "core.credential_stages",
)
_loaded = False


class Stages:
    """A workflow's main path and side outcomes (D99)."""

    def __init__(
        self,
        model: str,
        main: Iterable[Stage],
        outcomes: Iterable[Outcome] = (),
        *,
        state: StateFn | None = None,
        visited: Callable[[object], list[str] | None] | None = None,
        due: Callable[[object], object] | None = None,
        labels: dict[str, str] | None = None,
        staff_permission: str = "",
        derived: bool = False,
    ):
        self.model = model.lower()
        self.main = list(main)
        self.outcomes = list(outcomes)
        self._state = state
        self._visited = visited
        self._due = due
        self.labels = labels or {}
        self.staff_permission = staff_permission
        self.derived = derived
        self.workflow: Workflow | None = None

    # The states this path knows (a guard test: every state is somewhere).
    def known_states(self) -> set[str]:
        out: set[str] = set()
        for part in (*self.main, *self.outcomes):
            out |= set(part.states)
        return out

    def state_of(self, obj) -> str:
        return str(self._state(obj) if self._state else obj.state)

    def visited_of(self, obj) -> list[str] | None:
        """The states the item has been in, oldest first; None if not known."""
        if self._visited is not None:
            return self._visited(obj)
        if self.workflow is not None:
            events = list(self.workflow.history(obj).values_list("from_state", "to_state"))
            if not events:
                return None
            out = [events[0][0]]
            out += [to for _f, to in events]
            return out
        return None

    @classmethod
    def derive(cls, workflow: Workflow) -> Stages:
        """A plain default for a workflow that declares no path: its states in
        the order the transitions reach them; states that end the item after
        a closing or refusing step (withdraw, cancel, deny...) are outcomes."""
        from core.templatetags.portal import is_danger, state_label

        targets = {str(t.target) for t in workflow.transitions.values()}
        sources = set()
        for t in workflow.transitions.values():
            sources |= {str(s) for s in t.sources}
        order = sorted(sources - targets) or sorted(sources)[:1]
        seen = list(order)
        i = 0
        while i < len(seen):
            for t in workflow.transitions.values():
                if seen[i] in {str(s) for s in t.sources} and str(t.target) not in seen:
                    seen.append(str(t.target))
            i += 1
        ends = {
            str(t.target)
            for t in workflow.transitions.values()
            if is_danger(t.action) and str(t.target) not in sources
        }
        main = [stage(state_label(s), s) for s in seen if s not in ends]
        outs = [outcome(state_label(s), s) for s in seen if s in ends]
        return cls("", main, outs, derived=True)


def track(model: str, main, outcomes=(), **kwargs) -> Stages:
    """Declare the path of a state machine that is not a core.workflow
    Workflow (its state derived from append-only decisions)."""
    path = Stages(model, main, outcomes, **kwargs)
    STAGES[path.model] = path
    return path


def stages_for(obj) -> Stages | None:
    global _loaded
    if not _loaded:
        import importlib

        for name in STAGE_MODULES:
            importlib.import_module(name)
        _loaded = True
    meta = getattr(obj, "_meta", None)
    return STAGES.get(meta.label_lower) if meta is not None else None


@dataclass
class StageView:
    label: str
    status: str  # done | current | future | skipped | outcome | earlier (a branch left)
    detail: str = ""


@dataclass
class Strip:
    items: list[StageView]
    step: int  # 1-based position of the current stage among the shown ones
    total: int
    current: StageView | None
    ended: bool  # a side outcome
    finished: bool  # at the last stage of the main path
    next_who: str = ""
    next_due: object = None

    @property
    def summary(self) -> str:
        if self.current is None:
            return ""
        if self.ended:
            return f"Ended: {self.current.label}"
        return f"Step {self.step} of {self.total}: {self.current.label}"


def strip(obj, viewer=None) -> Strip | None:
    """Where ``obj`` is on its path, as ``viewer`` may see it (D99)."""
    path = stages_for(obj)
    if path is None:
        return None
    staff = not path.staff_permission or (viewer is not None and can(viewer, path.staff_permission))
    main = [s for s in path.main if staff or not s.staff_only]
    current = path.state_of(obj)
    visited = path.visited_of(obj)
    been = set(visited or ()) | {current}

    def index_of(state: str) -> int | None:
        for i, s in enumerate(main):
            if state in s.states:
                return i
        return None

    ended = next((o for o in path.outcomes if current in o.states), None)
    if ended is not None:
        # The stage it stopped at: the last main-path state before the outcome.
        reached = None
        for st in reversed(visited or ()):
            idx = index_of(st)
            if idx is not None:
                reached = idx
                break
        upto = -1 if reached is None else reached
        items = []
        for s in main[: upto + 1]:
            if s.branch and not (been & s.states):
                continue
            items.append(StageView(s.label, "done"))
        cur = StageView(ended.label, "outcome")
        items.append(cur)
        who = ended.who(obj, viewer) if callable(ended.who) else ended.who
        due = path._due(obj) if (path._due and who) else None
        return Strip(items, len(items), len(items), cur, True, False, who or "", due)

    at = index_of(current)
    if at is None:  # a state the viewer's path leaves out (staff only)
        return None
    items = []
    step = 0
    cur = None
    for i, s in enumerate(main):
        if s.branch and i != at and not (been & s.states):
            continue
        if i < at:
            status = "done"
            if visited is not None and s.states and not (been & s.states):
                status = "skipped"
        elif i == at:
            status = "current"
        elif s.branch and been & s.states:
            # A detour the item took and came back from (a payment plan that
            # ended, the case back at its reminder): behind it, not ahead
            # (review BUG-003, 0.46.0).
            status = "earlier"
        else:
            status = "future"
        detail = "Earlier" if status == "earlier" else ""
        if i == at and len(s.states) > 1:
            label = path.labels.get(current)
            if label is None:
                from core.templatetags.portal import state_label

                label = state_label(current)
            detail = "" if label == s.label else label
        view = StageView(s.label, status, detail)
        items.append(view)
        if i == at:
            cur = view
            step = len(items)
    finished = at == len(main) - 1
    who = ""
    due = None
    if not finished:
        here = main[at]
        who = here.who(obj, viewer) if callable(here.who) else here.who
        due = path._due(obj) if (path._due and who) else None
    return Strip(items, step, len(items), cur, False, finished, who or "", due)
