"""Run the platform: HTTP API plus the customer web application.

This is a demo/development entry point. It provisions Aquaria Phuket if the database is
empty, runs the scheduled maintenance pass on a timer (hold reclaim, session
completion, reminder dispatch) and serves both ``/api/*`` and the static web app from
one process.

    python serve.py                     # http://127.0.0.1:8080
    python serve.py --port 9000
    python serve.py --fresh             # discard and re-provision the demo database
    python serve.py --db :memory:       # nothing persisted

What this is *not*: a production deployment. Production runs behind CloudFront and an
ALB with TLS terminated at the edge, real secrets from Secrets Manager, PostgreSQL
instead of SQLite, and the maintenance pass on EventBridge rather than a thread. Those
differences are called out where they matter below rather than left implicit.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import seed
from utp.api import create_server
from utp.app import Platform
from utp.core.clock import FixedClock
from utp.core.errors import ConfigurationError
from utp.security.secrets import SECRET_NAMES, EnvironmentSecretProvider

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "aquaria.db"

#: How often the maintenance pass runs locally. R10.4 caps hold reclaim at 60 seconds
#: after expiry, so anything at or below that satisfies the requirement.
MAINTENANCE_INTERVAL_SECONDS = 20


@dataclass(slots=True)
class DevelopmentSecretProvider:
    """Environment secrets with a loud, clearly-labelled local fallback.

    The platform is designed to refuse to start when a secret is missing (R73.9), which
    is right for production and useless for a demo on a laptop. This provider keeps the
    real behaviour — the environment always wins — and derives an obviously fake value
    otherwise, recording which names it had to invent so the banner can say so.
    """

    delegate: EnvironmentSecretProvider = field(default_factory=EnvironmentSecretProvider)
    invented: set[str] = field(default_factory=set)

    def get(self, name: str) -> str:
        try:
            return self.delegate.get(name)
        except ConfigurationError:
            self.invented.add(name)
            return f"development-only-{name.replace('.', '-')}-not-for-production"

    def get_versioned(self, name: str) -> tuple[str, str]:
        try:
            return self.delegate.get_versioned(name)
        except ConfigurationError:
            return "dev", self.get(name)


def _maintenance_loop(platform: Platform, tenant_id: str, venue_id: str, stop: threading.Event) -> None:
    """Run the scheduled jobs until asked to stop.

    Deliberately swallows and reports errors: a failed maintenance pass must not kill
    the server, because the gate and the checkout matter more than a reminder email.
    """
    while not stop.wait(MAINTENANCE_INTERVAL_SECONDS):
        try:
            result = platform.run_maintenance(tenant_id, venue_id=venue_id)
            interesting = {k: v for k, v in result.items() if v}
            if interesting:
                print(f"[maintenance] {interesting}", flush=True)
        except Exception as exc:  # noqa: BLE001 - report and keep serving
            print(f"[maintenance] failed: {type(exc).__name__}: {exc}", flush=True)


def _banner(*, host: str, port: int, db_label: str, provisioning: dict, secrets_invented: set[str]) -> str:
    url = f"http://{host}:{port}"
    lines = [
        "",
        "  Aquaria Phuket — Universal Ticketing, Booking & Access Platform",
        "  " + "-" * 68,
        f"  Customer booking app   {url}/",
        f"  Health                 {url}/api/health",
        f"  Security posture       {url}/api/security/posture",
        f"  Database               {db_label}",
        f"  Tenant / venue         {provisioning['tenant_id']} / {provisioning['venue_id']}",
        "",
        "  Staff sign-in (demo)   credential: Aquaria-Demo-2026",
        "      admin@aquaria.test      Platform Super Admin",
        "      manager@aquaria.test    Venue Manager",
        "      cashier@aquaria.test    Counter / Cashier",
        "      gate@aquaria.test       Gate Staff",
        "",
        "  Payment is simulated. No card data is accepted or stored (R14.2).",
        f"  Maintenance pass every {MAINTENANCE_INTERVAL_SECONDS}s: hold reclaim, reminders, session completion.",
    ]
    if secrets_invented:
        lines += [
            "",
            f"  WARNING: {len(secrets_invented)} of {len(SECRET_NAMES)} secrets are development",
            "  placeholders. Set UTP_SECRET_* (and UTP_SIGNING_KEY) before any real use.",
        ]
    lines += ["", "  Ctrl+C to stop.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ticketing platform locally.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="database path, or :memory:")
    parser.add_argument("--fresh", action="store_true", help="delete the database and re-provision")
    parser.add_argument("--no-maintenance", action="store_true", help="do not run scheduled jobs")
    parser.add_argument(
        "--demo-history",
        nargs="?",
        const=28,
        type=int,
        default=None,
        metavar="DAYS",
        help="generate N days of trading history so the dashboards have data (default 28)",
    )
    args = parser.parse_args(argv)

    db_path = args.db
    if db_path != ":memory:":
        target = Path(db_path)
        if not target.is_absolute():
            target = ROOT / target
        if args.fresh and target.exists():
            target.unlink()
            print(f"Removed {target}", flush=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        db_path = str(target)
        db_label = db_path
    else:
        db_label = ":memory: (nothing is persisted)"

    secret_provider = DevelopmentSecretProvider()
    # Demo history has to date orders in the past, which a real clock cannot do, so
    # that mode runs on a FixedClock wound back per day and then restored.
    clock = FixedClock(_dt.datetime.now(_dt.timezone.utc)) if args.demo_history else None
    platform = Platform(db_path=db_path, secret_provider=secret_provider, clock=clock)

    try:
        provisioning = seed.provision(platform)
    except Exception:
        platform.close()
        raise
    if provisioning.get("created"):
        print("Provisioned Aquaria Phuket.", flush=True)
    else:
        print("Existing tenant found; reusing it.", flush=True)

    tenant_id = provisioning["tenant_id"]
    venue_id = provisioning["venue_id"]

    if args.demo_history:
        import demo_history

        print(f"Generating {args.demo_history} days of demo trading history…", flush=True)
        stats = demo_history.generate(platform, provisioning, days=int(args.demo_history))
        print(
            "  {bookings} bookings, {tickets} tickets, {scans} admissions, "
            "{cancelled} cancelled, {no_show} no-shows".format(**stats),
            flush=True,
        )
        if stats.get("rejected"):
            # Surfaced rather than hidden: a silently dropped channel would leave a
            # missing column on the dashboard with no explanation.
            print(f"  {stats['rejected']} attempts refused by the platform:", flush=True)
            for reason, count in sorted(stats["reject_reasons"].items(), key=lambda kv: -kv[1])[:5]:
                print(f"    {count:4}x {reason}", flush=True)

    # Touch every secret once so the banner can report honestly what is missing,
    # rather than discovering it at the first QR scan.
    for name in SECRET_NAMES:
        secret_provider.get(name)

    stop = threading.Event()
    worker: threading.Thread | None = None
    if not args.no_maintenance:
        worker = threading.Thread(
            target=_maintenance_loop,
            args=(platform, tenant_id, venue_id, stop),
            name="utp-maintenance",
            daemon=True,
        )
        worker.start()

    try:
        server = create_server(
            platform, tenant_id=tenant_id, venue_id=venue_id, host=args.host, port=args.port
        )
    except OSError as exc:
        stop.set()
        platform.close()
        print(f"Cannot bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1

    print(
        _banner(
            host=args.host,
            port=args.port,
            db_label=db_label,
            provisioning=provisioning,
            secrets_invented=secret_provider.invented,
        ),
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.", flush=True)
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        if worker is not None:
            worker.join(timeout=2)
        platform.close()
    return 0


if __name__ == "__main__":
    # Ensure the package is importable when run from another working directory.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    raise SystemExit(main())
