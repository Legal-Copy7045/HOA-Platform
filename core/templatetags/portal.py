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
