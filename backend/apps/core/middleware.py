"""Request-scoped correlation id.

A customer sees a friendly message plus a short reference; the full technical
detail goes to the server log under the same reference (R66.4, R66.5). This
middleware is what ties the two together.
"""

from __future__ import annotations

import contextvars

from .ids import new_correlation_id

#: Readable by the logging filter and by the exception handler without threading
#: the request object through every call.
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)


def current_correlation_id() -> str:
    return correlation_id_var.get()


class CorrelationIdMiddleware:
    """Assign (or accept) a correlation id and echo it on the response."""

    header = "X-Correlation-Id"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get(self.header, "")
        # Only accept an inbound id that looks like ours; otherwise a client could
        # poison the logs with arbitrary text.
        cid = incoming if _looks_like_id(incoming) else new_correlation_id()
        token = correlation_id_var.set(cid)
        request.correlation_id = cid
        try:
            response = self.get_response(request)
        finally:
            correlation_id_var.reset(token)
        response[self.header] = cid
        return response


def _looks_like_id(value: str) -> bool:
    if not value or len(value) > 40:
        return False
    return value.startswith("cor_") and value[4:].isalnum()
