from django import template

from core import policy

register = template.Library()


@register.filter
def can(user, action: str) -> bool:
    """{% if user|can:"lot.view_all" %} - the same policy the views enforce."""
    return bool(user and user.is_authenticated and policy.can(user, action))
