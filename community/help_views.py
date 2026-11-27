"""Owner help pages (D73): Help, the "How it works" pages, Welcome, Getting
started and Ask the board. All signed-in; each checks its permission, and
the help pages (feature "owner_help") answer 404 while it is off."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from community import checklist
from community import help as helpdocs
from community.terms import term_text
from core import features, policy


def _topic_cards(request) -> list[dict]:
    out = []
    for topic in helpdocs.topics_for(request.user, request):
        out.append(
            {
                "topic": topic,
                "summary": term_text(topic.summary),
                "url": reverse("help_topic", args=[topic.slug]),
                "section": helpdocs.section_link(request.user, topic, request),
            }
        )
    return out


def _ask_context(request, topic=None) -> dict:
    return {
        "ask_url": helpdocs.ask_url(topic),
        "can_ask": policy.can(request.user, "request.submit"),
    }


@login_required
def help_index(request):
    policy.require(request.user, "help.view")
    return render(
        request,
        "community/help.html",
        {
            "topics": _topic_cards(request),
            "guide": helpdocs.menu_guide(request),
            "faq_on": features.enabled("faq", request),
            "checklist": checklist.for_user(request.user, request),
            **_ask_context(request),
        },
    )


def _topic_page(request, topic, *, form_body=None, error=""):
    can_edit = policy.can(request.user, "help.edit")
    extra = helpdocs.current_text(topic.slug)
    return render(
        request,
        "community/help_topic.html",
        {
            "topic": topic,
            "summary": term_text(topic.summary),
            "sections": helpdocs.sections(topic, request),
            "extra": extra,
            "extra_html": helpdocs.render_text(extra.body) if extra and extra.body else "",
            "section": helpdocs.section_link(request.user, topic, request),
            "can_edit": can_edit,
            "history": helpdocs.text_history(topic.slug) if can_edit else [],
            "form_body": form_body if form_body is not None else (extra.body if extra else ""),
            "edit_error": error,
            "max_text": helpdocs.MAX_TEXT,
            "others": [c for c in _topic_cards(request) if c["topic"].slug != topic.slug],
            **_ask_context(request, topic),
        },
        status=400 if error else 200,
    )


def _topic_or_404(request, slug):
    topic = helpdocs.BY_SLUG.get(slug)
    if topic is None or not helpdocs.may_open(request.user, topic, request):
        raise Http404
    return topic


@login_required
def help_topic(request, slug):
    policy.require(request.user, "help.view")
    return _topic_page(request, _topic_or_404(request, slug))


@login_required
@require_POST
def help_text_save(request, slug):
    policy.require(request.user, "help.edit")
    topic = _topic_or_404(request, slug)
    body = request.POST.get("body", "")
    try:
        helpdocs.save_text(topic.slug, body, actor=request.user, request=request)
    except helpdocs.HelpError as exc:
        # Shown again with what was typed (CLAUDE.md "Forms").
        return _topic_page(request, topic, form_body=body, error=str(exc))
    messages.success(request, "Saved. Owners see the new text now.")
    return redirect(reverse("help_topic", args=[topic.slug]) + "#from-the-board")


@login_required
def welcome(request):
    """Shown once after the guided first sign-in (and any time from Help).
    Never a blocking step: an ordinary page with links."""
    from community.models import HelpMark

    policy.require(request.user, "help.view")
    checklist.mark(request.user, HelpMark.Kind.WELCOME)
    roles = policy.roles_for(request.user)
    return render(
        request,
        "community/welcome.html",
        {
            "checklist": checklist.for_user(request.user, request),
            "owner": bool(roles.groups & policy.ACCOUNT_VIEWERS),
            "tenant": policy.TENANT in roles.groups and not roles.groups & policy.ACCOUNT_VIEWERS,
            "board": policy.BOARD in roles.groups,
            **_ask_context(request),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def getting_started(request):
    policy.require(request.user, "help.checklist")
    if request.method == "POST":
        try:
            target = checklist.act(request.user, request.POST.get("action", ""), request)
        except ValueError as exc:
            messages.error(request, str(exc))
            target = None
        except PermissionError as exc:
            raise PermissionDenied from exc
        return redirect(target or reverse("getting_started"))
    found = checklist.for_user(request.user, request)
    if found is None:
        raise Http404
    from documents.models import Category, Document

    docs = list(
        Document.objects.for_user(request.user)
        .filter(category=Category.GOVERNING, archived=False)
        .order_by("title")[:12]
    )
    return render(
        request,
        "community/getting_started.html",
        {
            "checklist": found,
            "documents": docs,
            "on_dashboard": checklist.for_dashboard(request.user, request) is not None,
            **_ask_context(request),
        },
    )


@login_required
def ask_board(request):
    """The one "Ask the board" entry (D73): opens a new owner request (of the
    kind a help page suggests) for anyone who may raise one, and explains
    what to do for everyone else. Not gated by the help feature."""
    from community.models import HelpMark
    from operations import services as operations
    from operations.models import RequestType

    user = request.user
    about = request.GET.get("about")
    about = about if about in RequestType.values else ""
    if policy.can(user, "help.checklist"):
        checklist.mark(user, HelpMark.Kind.OPENED, "ask")
    if policy.can(user, "request.submit") and operations.request_lots(user):
        url = reverse("requests") + "?mine=1&new=1"
        if about:
            url += f"&about={about}"
        return redirect(url + "#new-request")
    roles = policy.roles_for(user)
    staff = policy.can(user, "request.manage")
    board = []
    if policy.can(user, "board_committees.view"):
        from community.services import board_members_public

        board = board_members_public(actor=user)
    return render(
        request,
        "community/ask_board.html",
        {
            "view_only": policy.VIEW_ONLY in roles.groups,
            "former": policy.FORMER_OWNER in roles.groups,
            "staff": staff,
            "board": board,
            "help_on": helpdocs.enabled(request) and policy.can(user, "help.view"),
        },
    )
