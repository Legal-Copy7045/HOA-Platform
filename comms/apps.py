from django.apps import AppConfig


class CommsConfig(AppConfig):
    name = "comms"
    verbose_name = "Communications"

    def ready(self):
        from comms import exports  # noqa: F401  (registers the data-export section)
