from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Accounts"

    def ready(self) -> None:
        from accounts import signals

        signals.connect_optional_signals()
