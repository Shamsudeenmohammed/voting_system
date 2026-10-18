from django.apps import AppConfig


class BallotsConfig(AppConfig):
    name = "apps.ballots"
    verbose_name = "Ballots"

    def ready(self):  # pragma: no cover
        from . import signals  # noqa: F401
