"""Reporting, analytics and the two dashboards (R70, R71, reports spec).

Structured so that adding a report is a declaration plus a query, never a new
screen:

* ``definitions`` — the catalog. Sections, titles, the permission each report
  needs, the filters that are relevant to it, and its columns. The navigation and
  the filter bar are rendered from this, so the UI has no per-report code.
* ``metrics`` — aggregates. Every amount is read from a stored transactional
  column, never recomputed, so a report agrees with the receipt.
* ``rows`` — the row-level queries a summary drills down into.
* ``exceptions`` — what looks wrong, with a severity and a suggested action.
* ``export`` — CSV and a printable document, each carrying its own filter
  criteria so a figure is never unattributable.
* ``service`` — the only entry point. Permission, venue scope, PII masking, cost
  masking and export auditing all live here, so no report can skip them.

The distinction the whole module turns on: **gross activity and net revenue are
different questions.** Cancelled, voided, refunded and complimentary items stay
visible in activity and are excluded from net revenue (R46.4, R70.6).
"""

from .definitions import REPORTS, REPORTS_BY_KEY, catalog
from .metrics import Metrics, Scope
from .service import ReportingService

__all__ = [
    "Metrics",
    "REPORTS",
    "REPORTS_BY_KEY",
    "ReportingService",
    "Scope",
    "catalog",
]
