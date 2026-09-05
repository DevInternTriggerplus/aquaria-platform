"""Report export: CSV and a printable document.

Both formats carry the filter criteria and the generation timestamp in the file
itself (R71.9). Without that a spreadsheet emailed to a third party is an
unattributable number — nobody can tell which venue, which dates or which
channel produced it, and two people comparing figures cannot see why they
disagree.

Export is a separate privilege from viewing and is audited with the filters and
the row count (R41.7), which the service does; this module only renders. Masking
has already been applied to the rows by the time they arrive here, so an export
can never reveal more than the screen it came from.

PDF is deliberately not generated. Writing a PDF encoder by hand would be a
second QR-encoder-sized project for no gain, so the printable HTML is styled for
the browser's own "Print to PDF", which produces a better-typeset document than
a hand-rolled writer would and needs no dependency.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import re as _re
from html import escape
from typing import Any

from ..core.money import format_currency

#: What a filter key should be called in an export header.
_FILTER_LABELS = {
    "date_from": "From",
    "date_to": "To",
    "date_basis": "Date basis",
    "venue": "Venue",
    "channel": "Channel",
    "product": "Product",
    "ticket_type": "Ticket type",
    "segment": "Segment",
    "pricing_group": "Pricing group",
    "promotion": "Promotion",
    "partner": "Partner",
    "payment_method": "Payment method",
    "staff": "Staff",
    "counter": "Counter",
    "device": "Device",
    "show": "Show",
    "session": "Session",
    "booking_status": "Booking status",
    "payment_status": "Payment status",
    "scan_result": "Scan result",
    "group_by": "Grouped by",
    "compare_with": "Compared with",
    "currency": "Currency",
}


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DT_RE = _re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?")
_TIME_RE = _re.compile(r"^(\d{2}):(\d{2})(?::(\d{2}))?$")


def _split_datetime(value: Any) -> tuple[str, str]:
    """Parse a business-local timestamp into (date, time) without shifting zone.

    Matches the client formatter: date as ``yyyy-Mmm-dd`` (2026-Sep-01), time as
    ``hh:mm:ss``. The value is already venue-local text from the report row, so it
    is parsed literally rather than through a datetime that would re-interpret it.
    """
    if value in (None, ""):
        return "", ""
    text = str(value).strip()
    m = _DT_RE.match(text)
    if not m:
        return text, ""
    y, mo, d, hh, mm, ss = m.groups()
    date = f"{y}-{_MONTHS[min(11, max(0, int(mo) - 1))]}-{d}"
    time = f"{hh}:{mm}:{ss or '00'}" if hh is not None else ""
    return date, time


def _fmt_date(value: Any) -> str:
    return _split_datetime(value)[0]


def _fmt_time(value: Any) -> str:
    date, time = _split_datetime(value)
    if time:
        return time
    m = _TIME_RE.match(str(value or "").strip())
    return f"{m.group(1)}:{m.group(2)}:{m.group(3) or '00'}" if m else ("" if value in (None, "") else str(value))


def _expand_columns(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split any ``datetime`` column into a Date and a Time column, matching the UI."""
    out: list[dict[str, Any]] = []
    for c in columns:
        if c.get("kind") == "datetime":
            out.append({"key": c["key"], "label": f'{c["label"]} (date)', "kind": "date", "align": "left"})
            out.append({"key": c["key"], "label": f'{c["label"]} (time)', "kind": "time", "align": "left"})
        else:
            out.append(c)
    return out


def _display(value: Any, kind: str, currency: str) -> str:
    """Format one cell for export.

    Money stays a formatted amount rather than raw minor units: a spreadsheet
    column of "125100" invites someone to read it as 125,100 baht. Dates and times
    are formatted to the venue standard (yyyy-Mmm-dd and hh:mm:ss).
    """
    if value is None or value == "":
        return ""
    if kind == "money":
        return format_currency(int(value), currency)
    if kind == "percent":
        return f"{int(value) / 100:g}%"
    if kind == "number":
        return str(int(value))
    if kind == "date":
        return _fmt_date(value)
    if kind == "time":
        return _fmt_time(value)
    if kind == "datetime":
        date, time = _split_datetime(value)
        return f"{date} {time}".strip()
    return str(value)


def _criteria_lines(report_title: str, meta: dict[str, Any]) -> list[tuple[str, str]]:
    """The provenance block that heads every export."""
    lines: list[tuple[str, str]] = [("Report", report_title)]
    generated = meta.get("generated_at") or _dt.datetime.now(_dt.timezone.utc).isoformat()
    lines.append(("Generated", generated))
    if meta.get("timezone"):
        lines.append(("Times shown in", str(meta["timezone"])))
    if meta.get("currency"):
        lines.append(("Currency", str(meta["currency"])))
    if meta.get("venue_names"):
        lines.append(("Venue", ", ".join(meta["venue_names"])))
    for key, label in _FILTER_LABELS.items():
        value = (meta.get("filters") or {}).get(key)
        if value in (None, "", [], ()):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        lines.append((label, str(value)))
    if meta.get("row_count") is not None:
        lines.append(("Rows", str(meta["row_count"])))
    if meta.get("masked"):
        lines.append(("Note", "Personal data is masked for this user."))
    return lines


def to_csv(
    *,
    report_title: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
) -> str:
    """CSV with a criteria header, then a blank line, then the table.

    The header is above the data rather than in a second sheet because CSV has no
    sheets, and stripping it is a single delete for anyone who wants only the
    grid.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for label, value in _criteria_lines(report_title, meta):
        writer.writerow([label, value])
    writer.writerow([])
    cols = _expand_columns(columns)
    writer.writerow([column["label"] for column in cols])
    currency = str(meta.get("currency") or "THB")
    for row in rows:
        writer.writerow(
            [_display(row.get(column["key"]), column.get("kind", "text"), currency) for column in cols]
        )
    return buffer.getvalue()


_PRINT_STYLES = """
  :root { --ink:#102a43; --soft:#627d98; --line:#d9e2ec; --peacock:#00677f; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 28px 32px 40px; color: var(--ink); background: #fff;
    font-family: Inter,'Noto Sans','Noto Sans Thai',Arial,sans-serif; font-size: 12px;
  }
  h1 { margin: 0 0 4px; font-size: 20px; color: var(--peacock); }
  .sub { margin: 0 0 18px; color: var(--soft); font-size: 12px; }
  .criteria {
    border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px;
    margin-bottom: 18px; background: #f7fafc;
    display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 4px 22px;
  }
  .criteria div { font-size: 11px; }
  .criteria b { color: var(--soft); font-weight: 650; }
  table { width: 100%; border-collapse: collapse; }
  th {
    text-align: left; padding: 7px 8px; border-bottom: 2px solid var(--peacock);
    font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--peacock);
  }
  td { padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th.r, td.r { text-align: right; }
  tbody tr:nth-child(even) { background: #f9fbfc; }
  .empty { padding: 28px; text-align: center; color: var(--soft); }
  @page { size: A4 landscape; margin: 12mm; }
  @media print {
    body { padding: 0; }
    thead { display: table-header-group; }   /* repeat headers across pages */
    tr { break-inside: avoid; }
  }
"""


def to_printable_html(
    *,
    report_title: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    auto_print: bool = False,
) -> str:
    """A printable document. The browser's own print-to-PDF does the rest."""
    currency = str(meta.get("currency") or "THB")
    criteria = "".join(
        f"<div><b>{escape(label)}:</b> {escape(value)}</div>"
        for label, value in _criteria_lines(report_title, meta)
    )
    cols = _expand_columns(columns)
    head = "".join(
        f'<th class="{"r" if column.get("align") == "right" else ""}">{escape(column["label"])}</th>'
        for column in cols
    )
    if rows:
        body = "".join(
            "<tr>"
            + "".join(
                f'<td class="{"r" if column.get("align") == "right" else ""}">'
                f'{escape(_display(row.get(column["key"]), column.get("kind", "text"), currency))}</td>'
                for column in cols
            )
            + "</tr>"
            for row in rows
        )
        table = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    else:
        table = '<p class="empty">No rows match these filters.</p>'
    script = '<script src="/print.js" defer></script>' if auto_print else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(report_title)}</title>
<style>{_PRINT_STYLES}</style>{script}
</head>
<body>
<h1>{escape(report_title)}</h1>
<p class="sub">{escape(str(meta.get("subtitle") or ""))}</p>
<div class="criteria">{criteria}</div>
{table}
</body>
</html>
"""


__all__ = ["to_csv", "to_printable_html"]
