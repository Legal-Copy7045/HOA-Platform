from django import template
from django.urls import reverse

from core import conflicts, policy

register = template.Library()


@register.filter
def can(user, action: str) -> bool:
    """{% if user|can:"lot.view_all" %} - the same policy the views enforce."""
    return bool(user and user.is_authenticated and policy.can(user, action))


@register.filter
def cents(value) -> str:
    """{{ amount_cents|cents }} -> $1,234.56"""
    from core.money import format_cents

    return format_cents(value) if value is not None else ""


@register.simple_tag
def max_upload_bytes() -> int:
    """The most one form submission may carry (all its files together)."""
    from core.uploads import max_request_bytes

    return max_request_bytes()


@register.filter
def cents_plain(value) -> str:
    """{{ amount_cents|cents_plain }} -> 1234.56 (for form inputs)."""
    if value is None:
        return ""
    sign = "-" if value < 0 else ""
    value = abs(int(value))
    return f"{sign}{value // 100}.{value % 100:02d}"


@register.simple_tag
def icon(name: str, extra_class: str = ""):
    """{% icon "home" %} -> inline <svg> using the self-hosted sprite."""
    from django.templatetags.static import static
    from django.utils.html import format_html

    return format_html(
        '<svg class="icon {}" aria-hidden="true" focusable="false"><use href="{}#{}"></use></svg>',
        extra_class,
        static("img/icons.svg"),
        name,
    )


# Workflow and record states -> (label, tone). Unknown states fall back to a
# readable label and a neutral tone.
_TONES = {
    "success": {
        "approved",
        "confirmed",
        "approved_with_conditions",
        "paid",
        "completed",
        "fulfilled",
        "issued",
        "cured",
        "closed",
        "held",
        "adopted",
        "accepted",
        "dismissed",
        "appeal_decided",
        "done",
        "clean",
        "resolved",
        "certified",
        "success",
        "active",  # bank feed connection
        "succeeded",  # server task
    },
    "info": {"nominations"},
    "warning": {
        "pending_approval",
        "submitted",
        "in_review",
        "in_progress",
        "noticed",
        "responded",
        "hearing",
        "appeal",
        "assigned",
        "reminder_sent",
        "second_reminder",
        "payment_plan",
        "awaiting_approval",
        "awaiting_code",
        "open",
        "pending",
        "queued",
        "scheduled",
        "draft",
        "waiting",
        "voting",
        "neighbors",
        "objected",
        "needs_reauth",  # bank feed connection
        "processing",
        "requested",  # server task
        "running",  # server task
    },
    "danger": {
        "denied",
        "rejected",
        "upheld",
        "referred",
        "expired",
        "code_expired",
        "code_locked",
        "infected",
        "overdue",
        "declined",
        "failed",
        "failure",
        "mismatch",
        "unreachable",
        "refused",  # server task
    },
    "neutral": {
        "void",
        "withdrawn",
        "cancelled",
        "revoked",
        "dropped",
        "unscanned",
        "disconnected",
    },
}
_LABELS = {
    "pending_approval": "Awaiting approval",
    "needs_reauth": "Needs reconnecting",
    "in_review": "In review",
    "noticed": "Notice sent",
    "approved_with_conditions": "Approved with conditions",
    "appeal_decided": "Appeal decided",
    "reminder_sent": "Reminder sent",
    "second_reminder": "Second reminder",
    "payment_plan": "Payment plan",
    "awaiting_approval": "Awaiting approval",
    "awaiting_code": "Awaiting code",
    "code_expired": "Code expired",
    "code_locked": "Locked",
    "in_progress": "In progress",
    "submitted": "Submitted",
    "referred": "Referred to counsel",
    "neighbors": "Awaiting neighbor acknowledgements",
}


def status_parts(state: str, label: str | None = None) -> tuple[str, str]:
    state = (state or "").lower()
    tone = next((t for t, states in _TONES.items() if state in states), "neutral")
    text = label or _LABELS.get(state) or state.replace("_", " ").capitalize()
    return text, tone


@register.simple_tag
def status(state: str, label: str = ""):
    """{% status obj.state %} -> a colored, human-readable badge."""
    from django.utils.html import format_html

    text, tone = status_parts(state, label or None)
    return format_html('<span class="status tone-{}">{}</span>', tone, text)


@register.filter
def state_label(state: str) -> str:
    return status_parts(state)[0]


_DANGER_ACTIONS = {
    "void",
    "cancel",
    "reject",
    "deny",
    "dismiss",
    "withdraw",
    "return_incomplete",
    "refer",
    "uphold",
    "revoke",
    "decline",
}


@register.filter
def is_danger(action: str) -> bool:
    """Workflow steps that close or refuse something get a confirm and red styling."""
    return action in _DANGER_ACTIONS


@register.simple_tag(takes_context=True)
def feature_on(context, url_name: str) -> bool:
    """{% feature_on "elections" as on %}: is this page's feature switched on?"""
    from core import features

    return features.url_name_enabled(url_name, context.get("request"))


@register.filter
def list_messages(messages) -> dict:
    """Split flashed messages into errors (announced at once) and the rest."""
    errors, others = [], []
    for message in messages:
        (errors if "error" in (message.tags or "").split() else others).append(message)
    return {"errors": errors, "others": others}


@register.filter
def short_date(value) -> str:
    """{{ d|short_date }}: "Nov 3" this month, "Nov 3, 2027" otherwise."""
    from core.dates import short_date as _short

    return _short(value)


@register.filter
def humanize_action(action: str) -> str:
    """ "invoice.approve" -> "Invoice: approve"; "request_changes" -> "Request changes"."""
    text = (action or "").replace("_", " ")
    if "." in text:
        head, _, tail = text.partition(".")
        return f"{head.capitalize()}: {tail.replace('.', ' ')}"
    return text.capitalize()


@register.filter
def audit_label(action: str) -> str:
    """An audit action in plain words, from its registered description
    ("Person added to the community register"), else humanized (WT-022)."""
    from audit.actions import ACTIONS

    spec = ACTIONS.get(action or "")
    return spec.description if spec else humanize_action(action)


@register.filter
def step_label(event) -> str:
    """A workflow history entry as people see it: the step's own label
    ("Submit for review"), not its code name ("submit")."""
    from core.workflow import REGISTRY

    workflow = REGISTRY.get(getattr(event, "workflow", ""))
    step = workflow.transitions.get(event.action) if workflow else None
    return step.label if step else humanize_action(getattr(event, "action", ""))


@register.simple_tag(takes_context=True)
def actor_label(context, actor) -> str:
    """Who took a workflow step, as an owner may see it (review A4).

    Board, managers and auditors see the login; owners see a role, never a
    staff member's or a previous owner's email address.
    """
    from core import policy

    if actor is None:
        return "automatic"
    viewer = context.get("user")
    if viewer is not None and getattr(viewer, "pk", None) == actor.pk:
        return "you"
    if viewer is not None and policy.can(viewer, "lot.view_all"):
        from core.people import display_name

        return display_name(actor)
    groups = policy.roles_for(actor).groups
    for group, label in (
        (policy.BOARD, "the board"),
        (policy.ARC_REVIEWER, "the architectural committee"),
        (policy.MANAGER, "the management company"),
    ):
        if group in groups:
            return label
    return "an owner"


@register.inclusion_tag("partials/thread.html", takes_context=True)
def thread_panel(context, kind, obj, reply_hint=""):
    """The message thread on a request, ARC request, violation or work order.
    ``obj`` was already fetched through its scoping by the page's view.
    ``reply_hint``: for the owner side, show this instead of the message box
    (a page with one formal reply box, e.g. a notice awaiting a response)."""
    from comms import threads

    user = context["user"]
    return {
        "reply_hint": reply_hint,
        "kind": kind,
        "obj": obj,
        "messages_list": threads.messages_for(kind, obj, user),
        "acc": threads.access(kind, obj, user),
        "csrf_token": context.get("csrf_token"),
        "user": user,
    }


@register.inclusion_tag("partials/follow_up.html", takes_context=True)
def follow_up_form(context, kind, obj):
    perms = {
        "violation": "violation.manage",
        "collection": "collections.manage",
        "work_order": "workorder.manage",
    }
    user = context["user"]
    return {
        "kind": kind,
        "obj": obj,
        # Own matters (D65): no staff follow-up date on one's own home's matter.
        "allowed": conflicts.acting_as_staff(user, obj, perms[kind]),
        "csrf_token": context.get("csrf_token"),
    }


@register.inclusion_tag("partials/quick_actions.html", takes_context=True)
def quick_actions(context):
    """Staff shortcuts (core.my_items.quick_actions), e.g. "Record payments received"."""
    from core.my_items import quick_actions as actions_for

    return {"actions": actions_for(context["user"])}


@register.inclusion_tag("partials/home_title.html")
def home_title(lot, level="h1", show_lot_ref=True):
    """A home as a heading (D40): the address is the title, the plat lot
    number a muted subtitle in ``.lot-ref`` (the only place a page outside
    the formal documents may show "Lot N")."""
    return {"lot": lot, "level": level, "show_lot_ref": show_lot_ref}


@register.filter
def get_item(mapping, key):
    """{{ dict|get_item:key }} for dictionaries keyed by a variable."""
    try:
        return mapping.get(key)
    except AttributeError:
        return None


def _charge_term(context):
    """The board's word for the monthly charge, read once per render (D42)."""
    from community.terms import charge_term

    cache = context.render_context
    if "charge_term" not in cache:
        cache["charge_term"] = charge_term()
    return cache["charge_term"]


@register.simple_tag(takes_context=True)
def term(context, form: str = "Term") -> str:
    """{% term %} -> Assessment; {% term "terms" %} -> assessments. Forms:
    term, terms, Term, Terms, TERM, TERMS (community/terms.py)."""
    return _charge_term(context).variants().get(form, "")


@register.simple_tag(takes_context=True)
def with_term(context, text: str) -> str:
    """{% with_term "Monthly {term} per home" %}: fills the placeholders."""
    from community.terms import term_text

    return term_text(str(text), _charge_term(context))


@register.simple_tag(takes_context=True)
def hub_crumb(context, hub):
    """The breadcrumb link to a hub ("Settings / "), or nothing when the hub
    would show this person only the page they are on: the menu links straight
    to a one-page hub, so the crumb does too (WT-009)."""
    from django.utils.html import format_html

    from core import navigation
    from core.hub_views import PAGES

    request = context.get("request")
    if request is None:
        return ""
    if hub in navigation.CRUMB_PAGES:
        # A parent page that is not a hub (the Finance overview, D89).
        label, permission = navigation.CRUMB_PAGES[hub]
        if not policy.can(request.user, permission):
            return ""
        target = navigation.CRUMB_TARGETS.get(hub, hub)
        return format_html('<a href="{}">{}</a> / ', reverse(target), label)
    if len(navigation.hub_links(request, hub)) < 2 and not (
        hub == "settings_hub" and policy.can(request.user, "community.manage_rules")
    ):
        return ""
    return format_html('<a href="{}">{}</a> / ', reverse(hub), PAGES[hub][0])


# --- Who is acting (D64): decision buttons and confirmations ------------------------------


@register.simple_tag(takes_context=True)
def for_board(context, label):
    """{% for_board "Approve" %}: "Approve for the board" for a board member."""
    from core import workflow

    return workflow.for_board(label, context.get("user"))


@register.simple_tag
def acting_confirm(text=""):
    """{% acting_confirm "Approve this invoice for payment?" %}: the text plus
    "You're acting for the association." """
    from core import workflow

    return workflow.acting_confirm(text)


@register.filter
def is_own_matter(reason) -> bool:
    """A blocked step's reason is the own-matter rule (D65): the step is
    shown disabled under the one own-home note, without repeating it."""
    return isinstance(reason, conflicts.OwnMatter)


@register.simple_tag
def own_home_note():
    """The note shown, before any button, on a decision about the board
    member's own home: another board member must decide (the service still
    refuses it)."""
    from core import workflow

    return workflow.OWN_HOME_NOTE


@register.simple_tag(takes_context=True)
def step_button(context, t):
    """A workflow step's button label (partials/workflow.html)."""
    from core import workflow

    if workflow.is_decision(t.action):
        return workflow.for_board(t.label, context.get("user"))
    return t.label


@register.simple_tag
def step_confirm(t):
    """A workflow step's confirmation: its own text, the generic one for a
    step that closes or refuses something, and for a decision who is acting."""
    from core import workflow

    text = t.confirm or (f"{t.label}? This can't be undone." if is_danger(t.action) else "")
    if workflow.is_decision(t.action):
        return workflow.acting_confirm(text or f"{t.label}?")
    return text
