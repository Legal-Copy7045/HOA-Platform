from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = "ledger"
    verbose_name = "Owner ledger and finance"

    def ready(self) -> None:
        from ledger import exports  # noqa: F401  (registers the data-export section)
