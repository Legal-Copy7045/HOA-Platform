from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Core infrastructure"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from core import checks  # noqa: F401  (registers deployment checks)
        from core.db_roles import apply_runtime_grants_on_migrate

        post_migrate.connect(apply_runtime_grants_on_migrate, dispatch_uid="core.db_grants")

        import logging

        from django.conf import settings

        if settings.EMAIL_REDIRECT_ALL_TO:
            # Decision D2: make a forgotten POC setting impossible to miss.
            logging.getLogger("core").warning("email.redirect_all_active")
