"""The stage tracker (D99): one strip drawn from each workflow's own declared
path, never by hand. Guards that every workflow declares its stages and
every detail page shows them; each path's done / current / future / side
outcome; the "Next:" line; owner versus board visibility; the phone markup;
CSP (no inline script, handler or style)."""

import datetime
import pathlib
import re

import pytest
from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from community.tests.factories import LotFactory, make_member
from core import workflow
from core.models import WorkflowEvent
from core.workflow import Stages, Transition, Workflow, outcome, stage, strip

pytestmark = pytest.mark.django_db

ROOT = pathlib.Path(settings.BASE_DIR)
TODAY = datetime.date(2026, 12, 4)


def _load():
    workflow.stages_for(object())  # imports STAGE_MODULES once


def labels(s):
    return [(i.label, i.status) for i in s.items]


# --- Guards: every workflow declares its path, every detail page shows it ---------------


def test_every_workflow_declares_its_stages():
    _load()
    real = {n: wf for n, wf in workflow.REGISTRY.items() if ".tests" not in wf.module}
    assert len(real) >= 9
    for name, wf in real.items():
        path = wf.stages
        assert not path.derived, f"workflow {name!r} must declare stages= (D99)"
        assert path.model and workflow.STAGES[path.model] is path, name
        assert path.main and all(s.label.strip() for s in path.main), name
        states = set()
        for t in wf.transitions.values():
            states |= {str(s) for s in t.sources} | {str(t.target)}
        missing = states - path.known_states()
        assert not missing, f"{name}: states not on its path or outcomes: {missing}"


def test_every_module_with_a_workflow_is_loaded_by_the_tracker():
    """A new app's Workflow is found by the tracker wherever it is asked."""
    found = set()
    for py in ROOT.glob("*/**/*.py"):
        rel = py.relative_to(ROOT)
        if ".venv" in rel.parts or "tests" in rel.parts or "migrations" in rel.parts:
            continue
        if re.search(r"^\w+ = (?:Workflow|track)\(", py.read_text(encoding="utf-8"), re.M):
            found.add(".".join(rel.with_suffix("").parts))
    assert found and found <= set(workflow.STAGE_MODULES), found - set(workflow.STAGE_MODULES)


@pytest.mark.parametrize(
    ("model", "choices"),
    [
        ("operations.insuranceclaim", "operations.models_options.ClaimState"),
        ("operations.capitalproject", "operations.models_options.ProjectState"),
        ("operations.onboardinground", "operations.onboarding_models.RoundStatus"),
        ("governance.vote", "governance.models.VoteStatus"),
        ("governance.election", "governance.models.ElectionState"),
        ("arc.arcrequest", "arc.models.ArcState"),
        ("compliance.violation", "compliance.models.ViolationState"),
        ("operations.ownerrequest", "operations.models.RequestState"),
    ],
)
def test_tracked_state_machines_cover_every_state(model, choices):
    import importlib

    _load()
    module, _, name = choices.rpartition(".")
    values = set(getattr(importlib.import_module(module), name).values)
    assert values <= workflow.STAGES[model].known_states()


def test_derived_state_machines_cover_every_status():
    from core import credentials
    from ledger import payment_plans
    from operations import reimbursements

    _load()
    s = workflow.STAGES
    assert set(reimbursements.STATUS_LABELS) <= s["operations.reimbursementclaim"].known_states()
    assert set(payment_plans.STATUS_LABELS) <= s["ledger.paymentplan"].known_states()
    assert set(credentials.STATE_WORDS) <= s["core.credentialchange"].known_states()
    assert {"awaiting", "approved", "declined", "paid"} <= s[
        "ledger.disbursementbatch"
    ].known_states()
    assert {
        "awaiting_approval",
        "awaiting_code",
        "completed",
        "cancelled",
        "expired",
        "code_expired",
        "code_locked",
    } <= s["accounts.mfareset"].known_states()
    assert {"waiting", "ready", "completed", "cancelled", "expired"} <= s[
        "accounts.accountrecovery"
    ].known_states()


# model -> the detail page that shows its tracker
PAGES = {
    "arc.arcrequest": "arc/detail.html",
    "operations.ownerrequest": "operations/request_detail.html",
    "governance.recordsrequest": "governance/records_request_detail.html",
    "compliance.violation": "compliance/detail.html",
    "ledger.collectioncase": "ledger/collection_detail.html",
    "ledger.resalerequest": "ledger/resale_detail.html",
    "operations.workorder": "operations/work_order_detail.html",
    "operations.invoice": "operations/invoice_detail.html",
    "operations.contract": "operations/contract_detail.html",
    "operations.reimbursementclaim": "operations/reimbursement_detail.html",
    "ledger.disbursementbatch": "ledger/disbursement_detail.html",
    "governance.vote": "governance/vote_detail.html",
    "governance.election": "governance/election_detail.html",
    "ledger.paymentplan": "ledger/payment_plan_detail.html",
    "ledger.specialassessment": "ledger/special_assessment_detail.html",
    "operations.insuranceclaim": "operations/insurance_claim_detail.html",
    "operations.capitalproject": "operations/capital_project_detail.html",
    "core.credentialchange": "core/keys_change.html",
    "accounts.accountrecovery": "accounts/board/recovery.html",
    "accounts.mfareset": "accounts/board/reset_code.html",
    "operations.onboardinground": "operations/onboarding/vendor.html",
}


def test_every_tracked_item_has_its_tracker_on_its_detail_page():
    _load()
    assert set(workflow.STAGES) == set(PAGES)
    for page in PAGES.values():
        text = (ROOT / "templates" / page).read_text(encoding="utf-8")
        assert "{% workflow_stages " in text, page
    owner_page = (ROOT / "templates/accounts/recovery_status.html").read_text(encoding="utf-8")
    assert "{% workflow_stages current %}" in owner_page


def test_a_workflow_without_a_path_gets_a_sensible_default():
    wf = Workflow(
        "stages_testflow",
        [
            Transition("submit", {"draft"}, "pending", "invoice.enter", "Submit"),
            Transition("approve", {"pending"}, "approved", "invoice.approve", "Approve"),
            Transition("withdraw", {"draft", "pending"}, "withdrawn", "invoice.enter", "Withdraw"),
        ],
    )
    path = wf.stages
    assert path.derived
    assert [s.label for s in path.main] == ["Draft", "Pending", "Approved"]
    assert [o.label for o in path.outcomes] == ["Withdrawn"]


# --- Each path: done, current, future, side outcome -------------------------------------


def _arc(state, **kw):
    from arc.models import ArcRequest

    return ArcRequest(state=state, **kw)


def test_arc_in_review_and_next_actor_with_its_clock():
    s = strip(_arc("in_review", review_due_on=TODAY), make_member(board=True))
    assert labels(s) == [
        ("Draft", "done"),
        ("Submitted", "done"),
        ("In review", "current"),
        ("Decided", "future"),
        ("Completed", "future"),
    ]
    assert s.summary == "Step 3 of 5: In review"
    assert s.next_who == "The committee: decide" and s.next_due == TODAY
    html = render_to_string("partials/stages.html", {"strip": s})
    assert "Next:</strong> The committee: decide — by Dec 4, 2026" in html


def test_arc_branches_show_only_when_reached():
    s = strip(_arc("neighbors"))
    assert ("Neighbor acknowledgements", "current") in labels(s)
    s = strip(_arc("appeal"))
    assert [i.label for i in s.items][-3:] == ["Decided", "Appeal", "Completed"]
    assert "Neighbor acknowledgements" not in [i.label for i in s.items]


def test_arc_decided_stage_names_the_decision_and_completed_is_done():
    s = strip(_arc("approved_with_conditions"))
    assert s.current.label == "Decided" and s.current.detail == "Approved with conditions"
    s = strip(_arc("completed"))
    assert s.finished and not s.next_who
    html = render_to_string("partials/stages.html", {"strip": s})
    assert "is-current is-done" in html and "done, current stage" in html


def test_side_outcome_ends_the_strip_where_it_stopped():
    from arc import services
    from arc.models import ArcRequest

    lot = LotFactory()
    owner = make_member(lots=[lot])
    req = services.create_request(lot=lot, title="Shed", description="8x10", actor=owner)
    for frm, to in (("draft", "submitted"), ("submitted", "in_review"), ("in_review", "withdrawn")):
        WorkflowEvent.objects.create(
            workflow="arc", object_id=str(req.pk), action="x", from_state=frm, to_state=to
        )
    ArcRequest.objects.unscoped("test").filter(pk=req.pk).update(state="withdrawn")
    req.refresh_from_db()
    s = strip(req, owner)
    assert labels(s) == [
        ("Draft", "done"),
        ("Submitted", "done"),
        ("In review", "done"),
        ("Withdrawn", "outcome"),
    ]
    assert s.ended and s.summary == "Ended: Withdrawn"
    html = render_to_string("partials/stages.html", {"strip": s})
    assert 'class="stage is-outcome" aria-current="step"' in html
    assert "how it ended" in html


def test_appeal_after_a_denial_shows_the_decision_done():
    from arc.models import ArcRequest

    obj = ArcRequest(state="appeal", appeal_due_on=TODAY)
    obj.pk = 876543
    for frm, to in (
        ("draft", "submitted"),
        ("submitted", "in_review"),
        ("in_review", "denied"),
        ("denied", "appeal"),
    ):
        WorkflowEvent.objects.create(
            workflow="arc", object_id="876543", action="x", from_state=frm, to_state=to
        )
    s = strip(obj)
    assert labels(s) == [
        ("Draft", "done"),
        ("Submitted", "done"),
        ("In review", "done"),
        ("Decided", "done"),
        ("Appeal", "current"),
        ("Completed", "future"),
    ]
    assert s.next_who == "The board: decide the appeal"


def test_denied_arc_says_who_may_appeal():
    owner = make_member()
    s = strip(_arc("denied", appeal_due_on=TODAY), owner)
    assert s.ended and s.next_who == "You may appeal to the board" and s.next_due == TODAY
    s = strip(_arc("denied", appeal_due_on=TODAY), make_member(board=True))
    assert s.next_who == "The owner may appeal to the board"


def test_a_stage_skipped_in_the_history_shows_as_skipped():
    from governance.models import RecordsRequest

    obj = RecordsRequest(state="fulfilled")
    obj.pk = 987654
    WorkflowEvent.objects.create(
        workflow="records_request",
        object_id="987654",
        action="fulfil",
        from_state="submitted",
        to_state="fulfilled",
    )
    assert labels(strip(obj)) == [
        ("Submitted", "done"),
        ("In progress", "skipped"),
        ("Fulfilled", "current"),
    ]


@pytest.mark.parametrize(
    ("make", "expected", "who"),
    [
        (
            lambda: __import__("governance.models", fromlist=["x"]).RecordsRequest(
                state="in_progress", due_on=TODAY
            ),
            [("Submitted", "done"), ("In progress", "current"), ("Fulfilled", "future")],
            "The board: provide the records",
        ),
        (
            lambda: __import__("ledger.models", fromlist=["x"]).CollectionCase(
                state="second_reminder"
            ),
            [
                ("Opened", "done"),
                ("First reminder", "done"),
                ("Second reminder", "current"),
                ("Final notice", "future"),
                ("Closed", "future"),
            ],
            "The manager: send the final notice",
        ),
        (
            lambda: __import__("ledger.models", fromlist=["x"]).ResaleRequest(state="submitted"),
            [("Requested", "current"), ("Being prepared", "future"), ("Issued", "future")],
            "The manager: start preparing it",
        ),
        (
            lambda: __import__("operations.models", fromlist=["x"]).WorkOrder(state="assigned"),
            [
                ("Open", "done"),
                ("Assigned", "current"),
                ("In progress", "future"),
                ("Completed", "future"),
                ("Closed", "future"),
            ],
            "The vendor: start the work",
        ),
        (
            lambda: __import__("operations.models", fromlist=["x"]).Invoice(
                state="pending_approval"
            ),
            [
                ("Entered", "done"),
                ("Waiting for approval", "current"),
                ("Approved", "future"),
                ("Paid", "future"),
            ],
            "The board: approve or reject",
        ),
        (
            lambda: __import__("operations.models", fromlist=["x"]).Contract(state="draft"),
            [("Entered", "current"), ("Approved", "future")],
            "The board: approve it",
        ),
        (
            lambda: __import__("governance.models", fromlist=["x"]).Vote(status="open"),
            [("Draft", "done"), ("Voting open", "current"), ("Closed", "future")],
            "Owners: vote",
        ),
        (
            lambda: __import__("governance.models", fromlist=["x"]).Election(state="closed"),
            [
                ("Taking nominations", "done"),
                ("Voting open", "done"),
                ("Voting closed", "current"),
                ("Results certified", "future"),
            ],
            "The board: certify the results",
        ),
        (
            lambda: __import__("operations.models_options", fromlist=["x"]).InsuranceClaim(
                state="approved"
            ),
            [
                ("Reported", "done"),
                ("Under review", "done"),
                ("Approved", "current"),
                ("Paid", "future"),
                ("Closed", "future"),
            ],
            "The insurer: pay",
        ),
        (
            lambda: __import__("operations.models_options", fromlist=["x"]).CapitalProject(
                state="in_progress"
            ),
            [
                ("Planned", "done"),
                ("Approved", "done"),
                ("In progress", "current"),
                ("Completed", "future"),
            ],
            "The board or manager: complete it",
        ),
        (
            lambda: __import__("operations.onboarding_models", fromlist=["x"]).OnboardingRound(
                status="returned"
            ),
            [
                ("Vendor filling in", "done"),
                ("Returned to the vendor", "current"),
                ("Board review", "future"),
                ("Approved", "future"),
            ],
            "The vendor: correct and send it again",
        ),
    ],
)
def test_each_path_in_progress(make, expected, who):
    s = strip(make(), make_member(board=True))
    assert labels(s) == expected
    assert s.next_who == who


@pytest.mark.parametrize(
    ("model", "field", "state", "ended"),
    [
        ("governance.models.RecordsRequest", "state", "denied", "Denied"),
        ("ledger.models.ResaleRequest", "state", "cancelled", "Cancelled"),
        ("operations.models.WorkOrder", "state", "cancelled", "Cancelled"),
        ("operations.models.Invoice", "state", "rejected", "Rejected"),
        ("operations.models.Contract", "state", "discarded", "Discarded"),
        ("compliance.models.Violation", "state", "dismissed", "Dismissed"),
        ("operations.models.OwnerRequest", "state", "withdrawn", "Withdrawn"),
        ("operations.models_options.CapitalProject", "state", "cancelled", "Cancelled"),
        ("operations.onboarding_models.OnboardingRound", "status", "declined", "Declined"),
    ],
)
def test_each_path_side_outcome(model, field, state, ended):
    import importlib

    module, _, name = model.rpartition(".")
    obj = getattr(importlib.import_module(module), name)(**{field: state})
    s = strip(obj, make_member(board=True))
    assert s.ended and s.items[-1].label == ended and s.items[-1].status == "outcome"


def test_insurance_claim_denied_names_the_appeal_deadline():
    from operations.models_options import InsuranceClaim

    s = strip(InsuranceClaim(state="denied", appeal_deadline=TODAY), make_member(board=True))
    assert s.ended and s.next_who == "The board: appeal, reopen or close it"
    assert s.next_due == TODAY


def test_derived_paths(monkeypatch):
    """Reimbursements, payment plans, disbursements, special assessments and
    key changes read their state from their own append-only decisions."""
    from core import credentials
    from core.models import CredentialChange
    from ledger import disbursements, payment_plans
    from ledger import special_assessments as sas
    from ledger.models import DisbursementBatch
    from ledger.models_finance_gaps import PaymentPlan, PlanEventKind, SpecialAssessment
    from operations import reimbursements
    from operations.models_payouts import ReimbursementClaim

    board = make_member(board=True)
    monkeypatch.setattr(reimbursements, "state", lambda c: {"status": "approved"})
    s = strip(ReimbursementClaim(), board)
    assert labels(s) == [("Submitted", "done"), ("Approved", "current"), ("Paid", "future")]
    assert s.next_who == "The board or manager: pay it"
    monkeypatch.setattr(reimbursements, "state", lambda c: {"status": "declined"})
    assert labels(strip(ReimbursementClaim(), board)) == [
        ("Submitted", "done"),
        ("Declined", "outcome"),
    ]

    monkeypatch.setattr(payment_plans, "_events", lambda p: {PlanEventKind.APPROVED: 1})
    monkeypatch.setattr(payment_plans, "status", lambda p, events=None: "defaulted")
    assert labels(strip(PaymentPlan(), board)) == [
        ("Proposed", "done"),
        ("Current", "done"),
        ("Defaulted", "outcome"),
    ]

    monkeypatch.setattr(
        disbursements, "batch_state", lambda b: disbursements.BatchState("awaiting")
    )
    s = strip(DisbursementBatch(), board)
    assert s.current.label == "Proposed" and s.next_who == "The board: approve or decline"

    monkeypatch.setattr(sas, "cancelled_from", lambda sa: None)
    monkeypatch.setattr(sas, "_posted_numbers", lambda sa: {1, 2, 3})
    assert labels(strip(SpecialAssessment(installments=3), board))[-1] == (
        "All installments charged",
        "current",
    )
    monkeypatch.setattr(sas, "cancelled_from", lambda sa: 2)
    assert strip(SpecialAssessment(installments=3), board).items[-1].label == "Cancelled"

    change = CredentialChange(approvals_needed=1, expires_at=timezone.now())
    monkeypatch.setattr(
        credentials,
        "change_state",
        lambda c: credentials.ChangeState(c, credentials.PENDING, None),
    )
    s = strip(change, board)
    assert labels(s) == [
        ("Saved", "done"),
        ("Waiting for board approval", "current"),
        ("In use", "future"),
    ]
    assert s.next_who == "Another board member: approve or refuse"
    monkeypatch.setattr(
        credentials,
        "change_state",
        lambda c: credentials.ChangeState(c, credentials.REFUSED, None),
    )
    assert labels(strip(change, board))[-1] == ("Refused", "outcome")


def test_signin_reset_and_account_recovery():
    from accounts.models import AccountRecovery, MfaReset

    later = timezone.now() + datetime.timedelta(hours=10)
    s = strip(MfaReset(request_expires_at=later), make_member(board=True))
    assert s.current.label == "Requested" and s.next_who == "Another board member: approve it"
    s = strip(MfaReset(request_expires_at=later, approved_at=timezone.now(), code_expires_at=later))
    assert labels(s)[1] == ("Approved: code handed over", "current")
    assert s.next_due == later

    owner = make_member()
    row = AccountRecovery(ready_at=timezone.now() - datetime.timedelta(hours=1), expires_at=later)
    s = strip(row, owner)
    assert labels(s) == [("Waiting period", "done"), ("Ready", "current"), ("Completed", "future")]
    assert s.next_who == "You: set up a new passkey or app" and s.next_due == later
    s = strip(row, make_member(board=True))
    assert s.next_who == "The account holder: set up a new sign-in"


# --- Visibility: the tracker shows the viewer what the page already does ----------------


def test_owner_request_next_actor_reads_you_for_the_owner():
    from operations.models import OwnerRequest

    owner = make_member()
    obj = OwnerRequest(state="waiting")
    assert strip(obj, owner).next_who == "You: reply"
    assert strip(obj, make_member(board=True)).next_who == "The owner: reply"


def test_violation_reported_stage_is_the_boards_alone(client):
    from compliance import services
    from compliance.models import ViolationState

    lot = LotFactory()
    owner = make_member(lots=[lot])
    board = make_member(board=True)
    v = services.report(lot=lot, category="parking", description="Boat", actor=board)
    s = strip(v, board)
    assert s.current.label == "Reported"
    assert s.next_who == "The board: send the notice or dismiss it"
    services.VIOLATION.apply(v, "send_notice", actor=board)
    v.refresh_from_db()
    assert v.state == ViolationState.NOTICED
    assert "Reported" in [i.label for i in strip(v, board).items]
    owner_strip = strip(v, owner)
    assert owner_strip.items[0].label == "Notice sent"
    assert "Reported" not in [i.label for i in owner_strip.items]
    assert owner_strip.next_who == "You: fix it or respond"

    client.force_login(owner)
    html = client.get(reverse("violation_detail", args=[v.pk])).content.decode()
    assert 'data-testid="stages"' in html
    tracker = html[
        html.index('data-testid="stages"') : html.index(
            "</section>", html.index('data-testid="stages"')
        )
    ]
    assert "Reported" not in tracker and "Step 1 of" in tracker
    client.force_login(board)
    html = client.get(reverse("violation_detail", args=[v.pk])).content.decode()
    assert "Step 2 of" in html


def test_owner_and_board_see_the_arc_tracker_on_the_page(client):
    from arc import services

    lot = LotFactory()
    owner = make_member(lots=[lot])
    req = services.create_request(lot=lot, title="Deck", description="Cedar", actor=owner)
    client.force_login(owner)
    html = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert html.index('data-testid="stages"') < html.index("<h2")  # at the top
    assert "Step 1 of 5: Draft" in html and "You: finish and submit it" in html
    client.force_login(make_member(board=True))
    html = client.get(reverse("arc_detail", args=[req.pk])).content.decode()
    assert "The owner: finish and submit it" in html
    # A stranger still gets no page, so no tracker either.
    client.force_login(make_member(lots=[LotFactory()]))
    assert client.get(reverse("arc_detail", args=[req.pk])).status_code == 404


# --- Markup: accessible, phone collapse, CSP ---------------------------------------------


def test_markup_is_an_ordered_list_with_aria_current_and_a_phone_summary():
    s = strip(_arc("in_review"))
    html = render_to_string("partials/stages.html", {"strip": s})
    assert html.count('aria-current="step"') == 2  # once in each of the two lists
    assert '<ol class="stages-strip"' in html and '<ol class="stages-list"' in html
    assert '<details class="stages-compact"' in html
    assert "<summary>Step 3 of 5: In review</summary>" in html
    assert "(done)" in html and "(not yet)" in html and "(current stage)" in html
    css = (ROOT / "static/css/site.css").read_text(encoding="utf-8")
    assert re.search(
        r"@media \(max-width: 640px\) \{ \.stages-strip \{ display: none; \} "
        r"\.stages-compact \{ display: block; \} \}",
        css,
    )


def test_tracker_is_csp_safe():
    for state in ("draft", "in_review", "withdrawn", "completed", "denied"):
        html = render_to_string("partials/stages.html", {"strip": strip(_arc(state))})
        assert "<script" not in html
        assert not re.search(r"\son[a-z]+=", html)
        assert "style=" not in html
        assert "javascript:" not in html


def test_nothing_is_drawn_for_an_untracked_object():
    assert render_to_string("partials/stages.html", {"strip": None}).strip() == ""
    assert strip(object()) is None


def test_a_path_never_shows_notes():
    """States only: a workflow step's note never reaches the tracker."""
    from operations.models import WorkOrder

    obj = WorkOrder(state="in_progress")
    obj.pk = 424242
    WorkflowEvent.objects.create(
        workflow="work_order",
        object_id="424242",
        action="start",
        from_state="assigned",
        to_state="in_progress",
        note="internal: vendor late again",
    )
    html = render_to_string("partials/stages.html", {"strip": strip(obj)})
    assert "vendor late" not in html


def test_stage_declarations_are_plain_data():
    path = Stages("x.y", [stage("A", "a"), stage("B", "b")], [outcome("Gone", "gone")])
    assert path.known_states() == {"a", "b", "gone"}
