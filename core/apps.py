from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        from core import checks  # noqa: F401
        from tandem.observability import register_signals
        register_signals()
