import os

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self) -> None:
        """Bridge the Django setting into the ported signing helper.

        ``apps.core.ids`` was ported verbatim from the verified implementation and
        reads ``UTP_SIGNING_KEY`` from the environment. Rather than fork that file,
        publish the Django setting into the environment once at startup, so
        ``TICKET_SIGNING_KEY`` stays the single place a deployment configures it.
        """
        from django.conf import settings

        key = getattr(settings, "TICKET_SIGNING_KEY", "") or ""
        if key:
            os.environ["UTP_SIGNING_KEY"] = key
