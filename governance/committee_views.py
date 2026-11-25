"""Committees: the list, each committee's space, and its discussions."""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from comms import services as comms
from community.models import Person
from core import policy
from governance import services
from governance.models import Committee, CommitteeMembership, CommitteeRole, CommitteeTopic


def _committee(request, pk) -> Committee:
    committee = get_object_or_404(Committee, pk=pk)
    if not services.can_view_committee(request.user, committee):
        raise Http404
    return committee


def _flash(request, fn, ok):
    try:
        result = fn()
    except services.GovernanceError as exc:
        messages.error(request, str(exc))
        return None
    messages.success(request, ok)
    return result


@login_required
def committee_list(request):
    policy.require(request.user, "committee.view")
    if request.method == "POST":
        policy.require(request.user, "committee.manage")
        committee = _flash(
            request,
            lambda: services.create_committee(
                name=request.POST.get("name", ""),
                purpose=request.POST.get("purpose", ""),
                citation=request.POST.get("citation", ""),
                actor=request.user,
                request=request,
            ),
            "Committee created. Appoint its members next.",
        )
        if committee:
            return redirect("committee_detail", pk=committee.pk)
        return redirect("committees")
    rows = [(c, services.committee_members(c)) for c in services.committees_for(request.user)]
    return render(
        request,
        "governance/committees.html",
        {"rows": rows, "can_manage": policy.can(request.user, "committee.manage")},
    )


@login_required
def committee_detail(request, pk):
    committee = _committee(request, pk)
    from comms.models import Event
    from operations.models import BoardTask

    can_manage = policy.can(request.user, "committee.manage")
    members = services.committee_members(committee)
    return render(
        request,
        "governance/committee_detail.html",
        {
            "committee": committee,
            "members": members,
            "topics": CommitteeTopic.objects.filter(committee=committee),
            "tasks": BoardTask.objects.filter(committee=committee, status="open"),
            "events": Event.objects.for_user(request.user)
            .filter(committee=committee, cancelled=False)
            .filter(starts_at__gte=timezone.now() - datetime.timedelta(hours=6)),
            "documents": _committee_documents(request.user, committee),
            "can_work": services.can_work_in_committee(request.user, committee),
            "can_manage": can_manage,
            "people": Person.objects.for_user(request.user).order_by("display_name")
            if can_manage
            else [],
            "roles": CommitteeRole.choices,
        },
    )


def _committee_documents(user, committee):
    from documents.services import committee_documents

    return committee_documents(user, committee)


@login_required
@require_POST
def committee_action(request, pk, action):
    committee = _committee(request, pk)
    user = request.user
    if action == "appoint":
        policy.require(user, "committee.manage")
        person = get_object_or_404(Person.objects.for_user(user), pk=request.POST.get("person"))
        _flash(
            request,
            lambda: services.appoint_committee_member(
                committee,
                person=person,
                role=request.POST.get("role", "member"),
                actor=user,
                request=request,
            ),
            "Member appointed.",
        )
    elif action == "retire" or action == "reactivate":
        policy.require(user, "committee.manage")
        services.set_committee_active(
            committee, active=action == "reactivate", actor=user, request=request
        )
    elif action == "topic":
        topic = _flash(
            request,
            lambda: services.start_topic(
                committee,
                title=request.POST.get("title", ""),
                body=request.POST.get("body", ""),
                actor=user,
                request=request,
            ),
            "Discussion started.",
        )
        if topic:
            return redirect("committee_topic_detail", pk=topic.pk)
    elif action == "task":
        services.require_committee_member(user, committee)
        due = request.POST.get("due_date") or None
        try:
            due = datetime.date.fromisoformat(due) if due else None
        except ValueError:
            due = None
        from operations.services import OperationsError

        try:
            services.add_committee_task(
                committee, title=request.POST.get("title", ""), due_date=due, actor=user
            )
        except OperationsError as exc:
            messages.error(request, str(exc))
    elif action == "event":
        services.require_committee_member(user, committee)
        try:
            starts = timezone.make_aware(
                datetime.datetime.fromisoformat(request.POST.get("starts_at", ""))
            )
        except ValueError:
            messages.error(request, "Enter a date and time.")
            return redirect("committee_detail", pk=committee.pk)
        try:
            comms.create_event(
                title=request.POST.get("title", ""),
                starts_at=starts,
                location=request.POST.get("location", ""),
                committee=committee,
                actor=user,
                request=request,
            )
        except comms.CommsError as exc:
            messages.error(request, str(exc))
    else:
        raise Http404
    return redirect("committee_detail", pk=committee.pk)


@login_required
@require_POST
def committee_member_end(request, pk):
    policy.require(request.user, "committee.manage")
    membership = get_object_or_404(CommitteeMembership.objects.for_user(request.user), pk=pk)
    services.end_committee_membership(membership, actor=request.user, request=request)
    messages.success(request, "Membership ended.")
    return redirect("committee_detail", pk=membership.committee_id)


@login_required
@require_POST
def committee_task_close(request, pk, task_id):
    committee = _committee(request, pk)
    from operations.models import BoardTask
    from operations.services import OperationsError

    task = get_object_or_404(BoardTask, pk=task_id, committee=committee)
    try:
        services.close_committee_task(
            committee, task, status=request.POST.get("status", "done"), actor=request.user
        )
    except OperationsError as exc:
        messages.error(request, str(exc))
    return redirect("committee_detail", pk=committee.pk)


@login_required
def committee_topic_detail(request, pk):
    from comms import threads

    topic = threads.subject_for("committee_topic", pk, request.user)
    return render(request, "governance/committee_topic.html", {"topic": topic})
