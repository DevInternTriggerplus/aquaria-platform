"""Restore or reset a staff account's access — from the terminal, any time.

This is the escape hatch for "I'm locked out of the back office." It opens the same
on-disk database the server uses and reconciles an account so it can sign in with a
known password and a full-access role. It does **not** need the server running (in
fact it should not run while the server holds the database), and it never depends on
the demo seed having been run in a particular way.

Usage (from the ``backend`` / ``legacy-stdlib`` folder):

    python reset_access.py
        Restore the owner account (nisachol.la@triggersplus.com) with the default
        password and the OWNER role, against data/aquaria.db.

    python reset_access.py --email someone@example.com --password "MyNewPass123" --role VENUE_MANAGER
        Restore/reset any account with any role.

    python reset_access.py --db data/aquaria.db
        Point at a specific database file.

After it runs, sign in at the login page with the email and password it prints.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import seed
from utp.app import Platform

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "aquaria.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore or reset a staff account's access.")
    parser.add_argument("--email", default=seed.OWNER_EMAIL, help=f"account email (default {seed.OWNER_EMAIL})")
    parser.add_argument("--password", default=seed.DEMO_CREDENTIAL,
                        help="new password (default the demo credential)")
    parser.add_argument("--role", default="OWNER",
                        help="role code to grant (default OWNER = full access)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="database path (default data/aquaria.db)")
    args = parser.parse_args(argv)

    db_path = args.db
    if db_path != ":memory:":
        target = Path(db_path)
        if not target.is_absolute():
            target = ROOT / target
        if not target.exists():
            print(
                f"Database not found: {target}\n"
                "Start the server once first (python serve.py) so the database is created,\n"
                "then stop it and run this command again.",
                file=sys.stderr,
            )
            return 1
        db_path = str(target)

    platform = Platform(db_path=db_path)
    try:
        # provision() creates the tenant if the DB is empty, or reuses it; either way
        # it returns the tenant/venue ids we need. It also self-heals the owner, which
        # is harmless here.
        info = seed.provision(platform)
        result = seed.ensure_owner_access(
            platform,
            tenant_id=info["tenant_id"],
            venue_id=info["venue_id"],
            email=args.email,
            credential=args.password,
            role_code=args.role,
        )
    finally:
        platform.close()

    print(
        "\n  Access restored.\n"
        f"    Email:    {result['email']}\n"
        f"    Password: {args.password}\n"
        f"    Role:     {result['role']} (full access)\n"
        f"    Database: {db_path}\n\n"
        "  Start the server (python serve.py) and sign in with the email and password above.\n"
    )
    return 0


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
