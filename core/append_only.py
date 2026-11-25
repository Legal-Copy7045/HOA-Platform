"""Reusable append-only enforcement (CLAUDE.md rule 5, decision D8).

Three layers, all required for a table holding financial, legal, decision or
audit records:

1. Model layer: ``AppendOnlyModel`` refuses updates and deletes, and its
   queryset refuses bulk update/delete.
2. Trigger: ``append_only_trigger_operations(table)`` in the model's migration
   installs BEFORE UPDATE/DELETE/TRUNCATE triggers that raise.
3. Grants: core.db_roles revokes UPDATE/DELETE/TRUNCATE from the runtime role
   for every concrete ``AppendOnlyModel`` after each migrate.

A test (core/tests/test_append_only_coverage.py) fails if any
AppendOnlyModel table lacks its triggers.

Corrections are new records that reference the one they correct.
"""

from __future__ import annotations

from django.db import migrations, models
from django.db.models.signals import class_prepared


class AppendOnlyError(Exception):
    """Raised when code tries to change or remove an append-only record."""


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError(f"{self.model.__name__} records cannot be updated")

    def delete(self):
        raise AppendOnlyError(f"{self.model.__name__} records cannot be deleted")

    def bulk_update(self, objs, fields, batch_size=None):
        raise AppendOnlyError(f"{self.model.__name__} records cannot be updated")

    def update_or_create(self, *args, **kwargs):
        raise AppendOnlyError(f"{self.model.__name__} records cannot be updated")

    def bulk_create(self, objs, *args, **kwargs):
        if kwargs.get("update_conflicts") or (len(args) >= 3 and args[2]):
            raise AppendOnlyError(f"{self.model.__name__} records cannot be updated")
        return super().bulk_create(objs, *args, **kwargs)


class AppendOnlyModel(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise AppendOnlyError(f"{type(self).__name__} records cannot be updated")
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(f"{type(self).__name__} records cannot be deleted")


_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION core_reject_append_only_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only table %: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;
"""


def append_only_trigger_operations(table: str) -> list[migrations.RunSQL]:
    """Migration operations installing the append-only triggers on ``table``."""
    row = f"{table}_append_only_row"
    stmt = f"{table}_append_only_truncate"
    return [
        migrations.RunSQL(_FUNCTION_SQL, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(
            f'CREATE TRIGGER "{row}" BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION core_reject_append_only_change();",
            reverse_sql=f'DROP TRIGGER IF EXISTS "{row}" ON "{table}";',
        ),
        migrations.RunSQL(
            f'CREATE TRIGGER "{stmt}" BEFORE TRUNCATE ON "{table}" '
            "FOR EACH STATEMENT EXECUTE FUNCTION core_reject_append_only_change();",
            reverse_sql=f'DROP TRIGGER IF EXISTS "{stmt}" ON "{table}";',
        ),
    ]


def _scoped_append_only():
    from core.scoping import ScopedModel, ScopedQuerySet

    class ScopedAppendOnlyQuerySet(AppendOnlyQuerySet, ScopedQuerySet):
        """Append-only and scoped: refuses changes, and refuses unscoped reads."""

    class ScopedAppendOnlyModel(AppendOnlyModel, ScopedModel):
        objects = ScopedAppendOnlyQuerySet.as_manager()

        class Meta:
            abstract = True

    return ScopedAppendOnlyQuerySet, ScopedAppendOnlyModel


ScopedAppendOnlyQuerySet, ScopedAppendOnlyModel = _scoped_append_only()


def _append_only_base_manager(sender, **kwargs) -> None:
    """Make Django's internal "base manager" refuse changes too (review D7).

    ``Model._base_manager`` is a plain QuerySet by default, so
    ``Model._base_manager.filter(...).update(...)`` skipped the model layer
    (the database trigger still refused it). Scoped models keep the default,
    because related-object access goes through the base manager.
    """
    if (
        issubclass(sender, AppendOnlyModel)
        and not sender._meta.abstract
        and not getattr(sender, "scope_lot_path", None)
        and not getattr(sender, "scope_person_path", None)
        and not hasattr(sender, "scope_for_owner")
    ):
        sender._meta.base_manager_name = "objects"


class_prepared.connect(_append_only_base_manager)
