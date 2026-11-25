"""People are shown by name, never by a number (owner's instruction).

Model ``__str__`` methods stay as ids on purpose: they reach logs and error
reports, which must not carry personal data. Pages use these helpers.
"""

from __future__ import annotations


def display_name(user) -> str:
    """The community name of a login: the Person's display name, else the
    part of the email before the @, never an id."""
    if user is None or not getattr(user, "pk", None):
        return ""
    cached = getattr(user, "_display_name", None)
    if cached is not None:
        return cached
    from community.models import Person
    from core.scoping import system_access

    with system_access("display name"):
        name = Person.objects.filter(user_id=user.pk).values_list("display_name", flat=True).first()
    name = name or (user.email or "").split("@")[0] or "Account holder"
    user._display_name = name
    return name
