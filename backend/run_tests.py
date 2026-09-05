"""Run the suite and print an unambiguous PASS/FAIL plus the database vendor.

Exists because `manage.py test` writes to stderr, so PowerShell reports a non-zero
exit code even on success, and because the engine actually exercised is the single
most important fact about a run of this suite.
"""

import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.conf import settings  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.utils import get_runner  # noqa: E402

connection.ensure_connection()
vendor = connection.vendor
locking = connection.features.has_select_for_update
print("=" * 62)
print(f"database vendor      : {vendor}")
print(f"SELECT FOR UPDATE    : {locking}")
print(f"database name        : {connection.settings_dict['NAME']}")
print(f"host:port            : {connection.settings_dict['HOST']}:{connection.settings_dict['PORT']}")
print("=" * 62)

if vendor != "postgresql":
    print("WARNING: not running on PostgreSQL. Capacity guarantees are NOT")
    print("         being proven — SQLite has no row-level locking.")
    print("=" * 62)

runner = get_runner(settings)(verbosity=1, interactive=False)
failures = runner.run_tests(["apps"])

print("=" * 62)
print(f"RESULT: {'PASS' if failures == 0 else 'FAIL'}  ({failures} failure(s))")
print(f"engine: {vendor}")
print("=" * 62)
sys.exit(0 if failures == 0 else 1)
