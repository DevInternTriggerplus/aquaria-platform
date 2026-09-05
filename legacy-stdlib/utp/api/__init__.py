"""HTTP layer.

The API is a thin transport over the service layer. It authenticates, applies the
security controls from :mod:`utp.security`, and maps domain errors to friendly
responses — it does not contain business rules, because every rule must hold for the
kiosk, the POS, a partner integration and a test equally (R42.1).
"""

from .server import ApiApplication, create_server, serve

__all__ = ["ApiApplication", "create_server", "serve"]
