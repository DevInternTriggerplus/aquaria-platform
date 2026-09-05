"""Development settings. Never use for anything reachable from the internet."""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = True
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "0.0.0.0"]

# Browsable API is genuinely useful while wiring the Next and Flutter clients.
REST_FRAMEWORK = {  # noqa: F405
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}
