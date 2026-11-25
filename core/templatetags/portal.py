from django import template

from core import policy

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
    },
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
    },
    "neutral": {"void", "withdrawn", "cancelled", "revoked", "dropped", "unscanned"},
}
_LABELS = {
    "pending_approval": "Awaiting approval",
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
}


def status_parts(state: str, label: str | None = None) -> tuple[str, str]:
    state = (state or "").lower()
    tone = next((t for t, states in _TONES.items() if state in states), "neutral")
    text = label or _LABELS.get(state) or state.replace("_", " ").capitalize()
    return text, tone


@register.simple_tag
def status(state: str, label: str = ""):
    """{% status obj.state %} -> a coloured, human-readable badge."""
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


@register.filter
def humanize_action(action: str) -> str:
    """ "invoice.approve" -> "Invoice: approve"; "request_changes" -> "Request changes"."""
    text = (action or "").replace("_", " ")
    if "." in text:
        head, _, tail = text.partition(".")
        return f"{head.capitalize()}: {tail.replace('.', ' ')}"
    return text.capitalize()


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
        return actor.email
    groups = policy.roles_for(actor).groups
    for group, label in (
        (policy.BOARD, "the board"),
        (policy.ARC_REVIEWER, "the architectural committee"),
        (policy.MANAGER, "the management company"),
    ):
        if group in groups:
            return label
    return "an owner"
