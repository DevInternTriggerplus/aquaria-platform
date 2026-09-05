"""Logging support: stamp every record with the request's correlation id."""

from __future__ import annotations

import logging

from .middleware import current_correlation_id


class CorrelationIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = current_correlation_id()
        return True
