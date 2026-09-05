"""SQL dialect abstraction.

PostgreSQL is the production database; SQLite remains the local/test backend so the
suite can run without a server. That is only safe if the *differences* live in one
place and the service layer never sees them — which is what this module is for.

The differences that actually matter to this platform's guarantees:

======================  ==================================  ==============================
Concern                 SQLite                              PostgreSQL
======================  ==================================  ==============================
Placeholders            ``?`` / ``:name``                   ``%s`` / ``%(name)s``
JSON storage            ``TEXT`` + json.dumps               ``JSONB``
Serialize writers       ``BEGIN IMMEDIATE``                 ``BEGIN`` + row/advisory locks
Immutability            ``RAISE(ABORT)`` in a trigger       trigger function ``RAISE EXCEPTION``
Partial unique index    supported                           supported (identical syntax)
Insertion-order tiebreak``rowid``                           ordered ``id`` (see note)
Upsert                  ``INSERT OR REPLACE``               ``ON CONFLICT DO UPDATE``
======================  ==================================  ==============================

Note on ordering: the platform never relies on ``rowid``. ``new_id()`` embeds a
millisecond timestamp as its leading field, so ``ORDER BY <ts_column> DESC, id DESC``
is insertion order to the millisecond and is deterministic within it, on both
engines.

The capacity guarantee is preserved on both engines but by different mechanisms.
SQLite serializes *all* writers with ``BEGIN IMMEDIATE``. PostgreSQL takes a row
lock on the specific session (``SELECT ... FOR UPDATE``), which is strictly better:
two different sessions no longer block each other, while contention for the *same*
session still serializes. Both are backed by the same CHECK constraint and the same
conditional-UPDATE compare-and-swap, so correctness does not depend on which lock
strategy is in play.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

DialectName = Literal["sqlite", "postgresql"]

#: Column-type tokens used in :mod:`utp.core.schema`. The schema is written once in
#: these tokens and rendered per dialect, so a new column cannot drift between engines.
TYPE_TOKENS: tuple[str, ...] = ("TEXT", "INTEGER", "BIGINT", "BOOLEAN", "JSON", "TIMESTAMPTZ", "NUMERIC")

_JSON_COLUMN_SUFFIX = "_json"


@dataclass(frozen=True, slots=True)
class Dialect:
    """Everything the storage layer needs to know about one engine."""

    name: DialectName
    json_type: str
    boolean_type: str
    timestamp_type: str
    paramstyle: Literal["qmark", "pyformat"]
    supports_immediate_transaction: bool
    #: True when JSON values must be serialized to text by the driver layer.
    json_as_text: bool

    @property
    def is_postgres(self) -> bool:
        return self.name == "postgresql"

    # ------------------------------------------------------------------ #
    # Type rendering
    # ------------------------------------------------------------------ #

    def render_type(self, token: str) -> str:
        return {
            "JSON": self.json_type,
            "BOOLEAN": self.boolean_type,
            "TIMESTAMPTZ": self.timestamp_type,
        }.get(token, token)

    def render_ddl(self, statement: str) -> str:
        """Rewrite a dialect-neutral DDL statement for this engine."""
        rendered = statement
        for token in ("JSON", "BOOLEAN", "TIMESTAMPTZ"):
            rendered = re.sub(rf"\b{token}\b(?!\w)", self.render_type(token), rendered)
        if self.is_postgres:
            # SQLite tolerates ``IF NOT EXISTS`` on triggers; PostgreSQL does not,
            # so trigger creation is emitted defensively instead.
            rendered = rendered.replace("CREATE TRIGGER IF NOT EXISTS", "CREATE TRIGGER")
        return rendered

    # ------------------------------------------------------------------ #
    # Parameter rendering
    # ------------------------------------------------------------------ #

    def convert(self, sql: str) -> str:
        """Translate ``?`` / ``:name`` placeholders into this driver's style.

        Applied centrally in :class:`utp.core.db.Database` so every query in the
        service layer stays written in one style.
        """
        if self.paramstyle == "qmark":
            return sql
        # Named placeholders first, so ``:name`` is not mistaken for a cast.
        sql = re.sub(r"(?<![:\w]):([a-zA-Z_]\w*)", r"%(\1)s", sql)
        return sql.replace("?", "%s")


SQLITE = Dialect(
    name="sqlite",
    json_type="TEXT",
    boolean_type="INTEGER",
    timestamp_type="TEXT",
    paramstyle="qmark",
    supports_immediate_transaction=True,
    json_as_text=True,
)

POSTGRESQL = Dialect(
    name="postgresql",
    json_type="JSONB",
    boolean_type="BOOLEAN",
    # Timestamps are stored as ISO-8601 text on both engines. Keeping one
    # representation means the audit trail, offline gate caches and exported
    # evidence are byte-identical regardless of backend, which matters more here
    # than native date arithmetic the platform never uses.
    timestamp_type="TEXT",
    paramstyle="pyformat",
    supports_immediate_transaction=False,
    json_as_text=False,
)

DIALECTS: dict[str, Dialect] = {"sqlite": SQLITE, "postgresql": POSTGRESQL}


def for_url(url: str) -> Dialect:
    """Resolve a dialect from a connection URL or path.

    ``postgresql://…`` / ``postgres://…`` select PostgreSQL; anything else (a path,
    ``:memory:``, ``sqlite://…``) selects SQLite.
    """
    lowered = (url or "").strip().lower()
    if lowered.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")):
        return POSTGRESQL
    return SQLITE


def is_json_column(column: str) -> bool:
    """JSON columns are named by convention so encoding needs no per-table registry."""
    return column.endswith(_JSON_COLUMN_SUFFIX)


# --------------------------------------------------------------------------- #
# Immutability enforcement
# --------------------------------------------------------------------------- #

#: PostgreSQL needs a function per guard kind; SQLite inlines RAISE(ABORT).
POSTGRES_GUARD_FUNCTIONS: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION utp_reject_delete() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'protected_record: % rows are retained for audit and cannot be deleted',
            TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION utp_reject_update() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'append_only_record: % rows cannot be modified',
            TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION utp_reject_scan_mutation() RETURNS trigger AS $$
    BEGIN
        IF NEW.decision <> OLD.decision
           OR COALESCE(NEW.reason, '') <> COALESCE(OLD.reason, '')
           OR NEW.at_utc <> OLD.at_utc
           OR COALESCE(NEW.ticket_id, '') <> COALESCE(OLD.ticket_id, '') THEN
            RAISE EXCEPTION 'append_only_record: a recorded scan decision cannot be altered'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION utp_reject_published_layout_change() RETURNS trigger AS $$
    BEGIN
        IF OLD.state = 'PUBLISHED' AND NEW.canvas_json::text <> OLD.canvas_json::text THEN
            RAISE EXCEPTION 'immutable_layout_version: create a new version to change a published layout'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION utp_reject_seat_recode() RETURNS trigger AS $$
    BEGIN
        IF NEW.code <> OLD.code AND EXISTS (
            SELECT 1 FROM seat_layout_versions v
            WHERE v.id = OLD.layout_version_id AND v.state = 'PUBLISHED'
        ) THEN
            RAISE EXCEPTION 'immutable_seat_identity: seat codes are stable in a published layout version'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
)


def postgres_delete_guard(table: str) -> str:
    return f"""
    DROP TRIGGER IF EXISTS trg_{table}_no_delete ON {table};
    CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table}
    FOR EACH ROW EXECUTE FUNCTION utp_reject_delete()
    """


def postgres_update_guard(table: str) -> str:
    return f"""
    DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table};
    CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table}
    FOR EACH ROW EXECUTE FUNCTION utp_reject_update()
    """


POSTGRES_SPECIFIC_GUARDS: tuple[str, ...] = (
    """
    DROP TRIGGER IF EXISTS trg_scan_events_decision_immutable ON scan_events;
    CREATE TRIGGER trg_scan_events_decision_immutable BEFORE UPDATE ON scan_events
    FOR EACH ROW EXECUTE FUNCTION utp_reject_scan_mutation()
    """,
    """
    DROP TRIGGER IF EXISTS trg_layout_version_published_immutable ON seat_layout_versions;
    CREATE TRIGGER trg_layout_version_published_immutable BEFORE UPDATE ON seat_layout_versions
    FOR EACH ROW EXECUTE FUNCTION utp_reject_published_layout_change()
    """,
    """
    DROP TRIGGER IF EXISTS trg_seat_code_stable ON seats;
    CREATE TRIGGER trg_seat_code_stable BEFORE UPDATE ON seats
    FOR EACH ROW EXECUTE FUNCTION utp_reject_seat_recode()
    """,
)


__all__ = [
    "DIALECTS",
    "POSTGRESQL",
    "POSTGRES_GUARD_FUNCTIONS",
    "POSTGRES_SPECIFIC_GUARDS",
    "SQLITE",
    "TYPE_TOKENS",
    "Dialect",
    "DialectName",
    "for_url",
    "is_json_column",
    "postgres_delete_guard",
    "postgres_update_guard",
]
