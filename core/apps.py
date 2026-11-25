from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "Core infrastructure"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from core.db_roles import apply_runtime_grants_on_migrate

        post_migrate.connect(apply_runtime_grants_on_migrate, dispatch_uid="core.db_grants")
