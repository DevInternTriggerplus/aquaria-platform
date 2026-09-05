"""Storage layer.

SQLite is used deliberately rather than as a placeholder. The properties the
requirements actually demand — a single authoritative serialization point for
capacity decrements (R10.5), CHECK constraints that make oversell impossible,
partial unique indexes for seat uniqueness (R57.9), and triggers that block
deletion of financial history (R46.6) — are all available here and are exercised
by the test suite. The repository API below is intentionally narrow so the same
service code can be pointed at PostgreSQL/RDS in production by swapping this
module: ``BEGIN IMMEDIATE`` becomes ``SELECT ... FOR UPDATE`` and partial unique
indexes carry across unchanged.

Concurrency model
-----------------
One connection per thread (SQLite connections are not thread-safe). Writers
serialize on the database write lock, which is precisely the "single
authoritative mechanism" R10.5 asks for. ``compare_and_increment`` performs the
capacity decrement as a conditional UPDATE so that losing a race is detected by
``rowcount == 0`` rather than by reading-then-writing.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import schema as schema_module
from .clock import Clock, SystemClock, to_iso

Row = sqlite3.Row


class IntegrityViolation(Exception):
    """Raised when a database constraint refuses a write.

    Callers translate this into the right domain error: a CHECK failure on
    ``sessions.confirmed`` means "just sold out", a partial-unique-index failure
    on ``seat_reservations`` means "seat just taken", and a trigger ABORT means
    "protected record".
    """

    def __init__(self, message: str, *, original: Exception | None = None) -> None:
        super().__init__(message)
        self.original = original

    @property
    def is_protected_record(self) -> bool:
        text = str(self)
        return "protected_record" in text or "append_only_record" in text

    @property
    def is_capacity(self) -> bool:
        text = str(self).lower()
        return "confirmed" in text or "capacity" in text

    @property
    def is_seat_conflict(self) -> bool:
        text = str(self)
        return "ux_seat_reservation_confirmed" in text or "ux_seat_hold_active" in text


class Database:
    """Thread-safe façade over a SQLite database."""

    def __init__(self, path: str = ":memory:", *, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if path in (":memory:", ""):
            # An ephemeral *file* rather than a shared-cache in-memory database.
            # Shared-cache memory databases use table-level locks that ignore
            # ``busy_timeout`` and fail fast with SQLITE_LOCKED, which would make
            # multi-threaded contention tests report spurious errors instead of
            # exercising the real serialization path. A temp file in WAL mode has
            # exactly the locking semantics production will have.
            self._temp_dir = tempfile.TemporaryDirectory(
                prefix="utp-db-", ignore_cleanup_errors=True
            )
            self._dsn = str(Path(self._temp_dir.name) / "utp.sqlite3")
        else:
            self._dsn = path
        self._uri = False
        self._keepalive: sqlite3.Connection | None = None

    # ------------------------------ connections ------------------------------ #

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._dsn,
            uri=self._uri,
            timeout=30.0,
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,
        )
        conn.row_factory = Row
        conn.execute("PRAGMA foreign_keys = ON")
        # A generous busy timeout is what turns "two channels raced" from an error
        # into a queue: the loser waits for the write lock instead of failing.
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - best effort
                    pass
            self._connections.clear()
        self._keepalive = None
        self._local = threading.local()
        if self._temp_dir is not None:
            try:
                self._temp_dir.cleanup()
            except OSError:  # pragma: no cover - Windows file locks
                pass
            self._temp_dir = None

    # ------------------------------- migration ------------------------------- #

    def migrate(self) -> int:
        """Apply the schema. Idempotent."""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in schema_module.all_statements():
                conn.execute(statement)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (schema_module.SCHEMA_VERSION, to_iso(self.clock.now())),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return schema_module.SCHEMA_VERSION

    # ------------------------------ transactions ----------------------------- #

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Run a unit of work.

        ``immediate=True`` acquires the write lock up front. Every capacity or
        seat mutation uses it, which is what makes concurrent confirmations from
        ONLINE, KIOSK, COUNTER, PARTNER and STAFF serialize (R10.5, R10.10).

        Nested use joins the outer transaction rather than starting a new one, so
        a service method can be called standalone or as part of a larger
        operation without changing its atomicity guarantees.
        """
        conn = self.connection
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth = depth
            return

        self._begin(conn, immediate=immediate)
        self._local.depth = 1
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            self._local.depth = 0
            raise
        else:
            conn.execute("COMMIT")
            self._local.depth = 0

    def _begin(self, conn: sqlite3.Connection, *, immediate: bool, attempts: int = 6) -> None:
        """Open a transaction, waiting out contention on the write lock.

        ``busy_timeout`` already makes SQLite wait, but a short bounded retry keeps
        heavy simultaneous checkout traffic from surfacing a lock error to a
        customer when the correct behaviour is simply to queue.
        """
        statement = "BEGIN IMMEDIATE" if immediate else "BEGIN"
        delay = 0.01
        for attempt in range(attempts):
            try:
                conn.execute(statement)
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc).lower():
                    raise
                if attempt == attempts - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.5)

    @property
    def in_transaction(self) -> bool:
        return bool(getattr(self._local, "depth", 0))

    # --------------------------------- reads --------------------------------- #

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[Row]:
        return list(self.connection.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Row | None:
        cursor = self.connection.execute(sql, params)
        return cursor.fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # -------------------------------- writes --------------------------------- #

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> sqlite3.Cursor:
        try:
            return self.connection.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise IntegrityViolation(str(exc), original=exc) from exc
        except sqlite3.OperationalError as exc:
            # Trigger ABORT surfaces as OperationalError in some builds.
            message = str(exc)
            if "protected_record" in message or "append_only_record" in message or "immutable" in message:
                raise IntegrityViolation(message, original=exc) from exc
            raise

    def insert(self, table: str, values: Mapping[str, Any]) -> str:
        """Insert a row, JSON-encoding any dict/list value automatically."""
        prepared = {k: encode(v) for k, v in values.items()}
        columns = ", ".join(prepared)
        placeholders = ", ".join(f":{c}" for c in prepared)
        self.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", prepared)
        return str(prepared.get("id", ""))

    def update(self, table: str, row_id: str, values: Mapping[str, Any], *, tenant_id: str | None = None) -> int:
        prepared = {k: encode(v) for k, v in values.items()}
        assignments = ", ".join(f"{c} = :{c}" for c in prepared)
        params = dict(prepared)
        params["_id"] = row_id
        sql = f"UPDATE {table} SET {assignments} WHERE id = :_id"
        if tenant_id is not None:
            sql += " AND tenant_id = :_tenant"
            params["_tenant"] = tenant_id
        return self.execute(sql, params).rowcount

    def compare_and_increment(
        self,
        table: str,
        row_id: str,
        *,
        counter: str,
        delta: int,
        limit_column: str | None = None,
        tenant_id: str | None = None,
        extra_predicate: str | None = None,
        extra_params: Mapping[str, Any] | None = None,
    ) -> bool:
        """Atomically add ``delta`` to ``counter`` without exceeding ``limit_column``.

        Returns ``False`` when the guard rejects the change — that is the "just
        sold out" signal (R10.6). Doing this as one conditional UPDATE, rather
        than read-check-write, is what removes the race entirely; the CHECK
        constraint on the table is the belt to this braces.
        """
        params: dict[str, Any] = {"_id": row_id, "_delta": int(delta)}
        sql = f"UPDATE {table} SET {counter} = {counter} + :_delta WHERE id = :_id"
        if tenant_id is not None:
            sql += " AND tenant_id = :_tenant"
            params["_tenant"] = tenant_id
        if delta > 0 and limit_column:
            sql += f" AND ({limit_column} IS NULL OR {counter} + :_delta <= {limit_column})"
        if delta < 0:
            sql += f" AND {counter} + :_delta >= 0"
        if extra_predicate:
            sql += f" AND ({extra_predicate})"
            params.update(dict(extra_params or {}))
        return self.execute(sql, params).rowcount == 1


# --------------------------------------------------------------------------- #
# JSON helpers
# --------------------------------------------------------------------------- #


def encode(value: Any) -> Any:
    """Encode dict/list/bool values for SQLite storage."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def decode(value: Any, default: Any = None) -> Any:
    """Decode a JSON column, tolerating NULL and malformed legacy content."""
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def row_to_dict(row: Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Sequence[Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


__all__ = [
    "Database",
    "IntegrityViolation",
    "Row",
    "decode",
    "encode",
    "row_to_dict",
    "rows_to_dicts",
]
