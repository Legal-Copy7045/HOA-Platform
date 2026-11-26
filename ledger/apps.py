from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = "ledger"
    verbose_name = "Owner ledger and finance"

    def ready(self) -> None:
        from django.db.models.signals import post_save

        from ledger import exports  # noqa: F401  (registers the data-export section)
        from ledger.allocation import on_entry_saved
        from ledger.models import LedgerEntry

        # Open-item accounting (D32-payments): number each new charge and apply
        # money for every new entry, whichever service posted it.
        post_save.connect(on_entry_saved, sender=LedgerEntry, dispatch_uid="ledger_allocation")
