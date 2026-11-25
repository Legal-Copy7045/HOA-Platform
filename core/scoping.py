"""Scoped data access: the reusable pattern for every lot- or person-owned model.

CLAUDE.md: "Every model that belongs to a lot or person must be reachable only
through a scoped manager/queryset ... The default manager must not return
unscoped data."

How to use it in any app:

    class Thing(ScopedModel):
        lot = models.ForeignKey("community.Lot", ...)
        scope_lot_path = "lot"            # or e.g. "request__lot"
        # or: scope_person_path = "person"

    Thing.objects.for_user(request.user)    # what views use
    Thing.objects.unscoped("reason")        # services, explicit and greppable
    with system_access("reason"): ...       # internal code paths in bulk

Any query on ``Thing.objects`` that was not scoped one of those ways raises
``UnscopedQueryError`` when it hits the database - including reverse
relations such as ``lot.things``. Forgetting to scope fails loudly in tests
instead of leaking data.

Who sees what is decided by core.policy:

* ``lot.view_all`` holders (board, auditor) see every row;
* owners see rows for lots they own *today*, or rows about themselves;
* everyone else sees nothing.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar

from django.db import models

_system_reason: ContextVar[str | None] = ContextVar("hoa_system_access", default=None)


class UnscopedQueryError(RuntimeError):
    """A scoped model was queried without for_user(), unscoped() or system_access()."""


def internal(reason: str):
    """Decorator form of system_access for trusted service functions."""

    def decorator(fn):
        import functools

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with system_access(reason):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


@contextlib.contextmanager
def system_access(reason: str):
    """Allow unscoped queries inside this block (internal code only)."""
    if not reason:
        raise ValueError("system_access needs a reason")
    token = _system_reason.set(reason)
    try:
        yield
    finally:
        _system_reason.reset(token)


class ScopedQuerySet(models.QuerySet):
    _scope_ok = False

    def _clone(self):
        clone = super()._clone()
        clone._scope_ok = self._scope_ok
        return clone

    def _check(self) -> None:
        if not self._scope_ok and _system_reason.get() is None:
            raise UnscopedQueryError(
                f"{self.model.__name__} queried without scoping; use "
                f".for_user(user), .unscoped(reason) or system_access(reason)"
            )

    # --- scoping -------------------------------------------------------------
    def unscoped(self, reason: str) -> ScopedQuerySet:
        if not reason:
            raise ValueError("unscoped() needs a reason")
        clone = self._clone()
        clone._scope_ok = True
        return clone

    def for_user(self, user) -> ScopedQuerySet:
        from core import policy

        clone = self._clone()
        clone._scope_ok = True
        roles = policy.roles_for(user)
        if roles is policy.NO_ROLES or not roles.groups:
            return clone.none()
        if policy.can(user, "lot.view_all"):
            return clone
        return self.model.scope_for_owner(clone, roles)

    # --- every path that reaches the database --------------------------------
    def _fetch_all(self):
        if self._result_cache is None:
            self._check()
        super()._fetch_all()

    def count(self):
        self._check()
        return super().count()

    def exists(self):
        self._check()
        return super().exists()

    def contains(self, obj):
        self._check()
        return super().contains(obj)

    def aggregate(self, *args, **kwargs):
        self._check()
        return super().aggregate(*args, **kwargs)

    def iterator(self, *args, **kwargs):
        self._check()
        return super().iterator(*args, **kwargs)

    def in_bulk(self, *args, **kwargs):
        self._check()
        return super().in_bulk(*args, **kwargs)

    def update(self, **kwargs):
        self._check()
        return super().update(**kwargs)

    def delete(self):
        self._check()
        return super().delete()

    def explain(self, *args, **kwargs):
        self._check()
        return super().explain(*args, **kwargs)


class ScopedModel(models.Model):
    """Base for models that belong to a lot or a person."""

    scope_lot_path: str | None = None
    scope_person_path: str | None = None

    class Meta:
        abstract = True

    @classmethod
    def scope_for_owner(cls, queryset: ScopedQuerySet, roles) -> ScopedQuerySet:
        """Rows an owner (no lot.view_all) may see. Override to narrow further."""
        condition = models.Q(pk__in=[])
        if cls.scope_lot_path is not None:
            key = f"{cls.scope_lot_path}__in" if cls.scope_lot_path else "pk__in"
            condition |= models.Q(**{key: roles.owned_lot_ids})
        if cls.scope_person_path is not None and roles.person_id is not None:
            key = cls.scope_person_path or "pk"
            condition |= models.Q(**{key: roles.person_id})
        return queryset.filter(condition)

    # Django's own validation queries the default manager; allow it.
    def validate_unique(self, exclude=None):
        with system_access("model validation"):
            return super().validate_unique(exclude)

    def validate_constraints(self, exclude=None):
        with system_access("model validation"):
            return super().validate_constraints(exclude)
