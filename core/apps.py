from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Core infrastructure"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from core import checks  # noqa: F401  (registers deployment checks)
        from core.db_roles import apply_runtime_grants_on_migrate

        post_migrate.connect(apply_runtime_grants_on_migrate, dispatch_uid="core.db_grants")

        # Anything that grants or removes a role clears the remembered roles.
        from django.apps import apps
        from django.db.models.signals import m2m_changed, post_delete, post_save

        from core.policy import invalidate_roles

        for label in (
            "community.Person",
            "community.Ownership",
            "community.BoardSeat",
            "community.OfficeHolding",
            "community.RoleAssignment",
            "community.Account",
            "community.AccountContact",
            "governance.CommitteeMembership",
            "accounts.User",
        ):
            model = apps.get_model(label)
            post_save.connect(invalidate_roles, sender=model, dispatch_uid=f"roles.save.{label}")
            post_delete.connect(invalidate_roles, sender=model, dispatch_uid=f"roles.del.{label}")
        m2m_changed.connect(invalidate_roles, dispatch_uid="roles.m2m")

        # Server error signatures (D45): also exceptions raised in middleware.
        from django.core.signals import got_request_exception

        from core.errors import on_request_exception

        got_request_exception.connect(on_request_exception, dispatch_uid="core.error_signature")

        import logging

        from django.conf import settings

        if settings.EMAIL_REDIRECT_ALL_TO:
            # Decision D2: make a forgotten POC setting impossible to miss.
            logging.getLogger("core").warning("email.redirect_all_active")
