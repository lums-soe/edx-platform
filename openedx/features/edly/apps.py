from django.apps import AppConfig


class EdlyConfig(AppConfig):
    """
    Application Configuration for EDLY.
    """
    name = 'openedx.features.edly'

    def ready(self):
        from openedx.features.edly import signals  # noqa # pylint: disable=unused-import
