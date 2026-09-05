#!/usr/bin/env python
"""Test runner.

``python run_tests.py`` runs everything. ``python run_tests.py security`` runs one
module. ``--security-report`` prints the OWASP control register instead of testing.
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def build_suite(selectors: list[str]) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    if not selectors:
        return loader.discover(str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    suite = unittest.TestSuite()
    for selector in selectors:
        name = selector if selector.startswith("tests.") else f"tests.test_{selector}"
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except (ImportError, AttributeError):
            # Allow a bare fragment, e.g. "security" -> tests.test_security_owasp
            matches = sorted(
                p.stem for p in (ROOT / "tests").glob("test_*.py") if selector in p.stem
            )
            if not matches:
                raise SystemExit(f"No test module matches {selector!r}")
            for match in matches:
                suite.addTests(loader.loadTestsFromName(f"tests.{match}"))
    return suite


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the platform test suite.")
    parser.add_argument("selectors", nargs="*", help="Module fragments, e.g. security capacity")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-f", "--failfast", action="store_true")
    parser.add_argument(
        "--security-report",
        action="store_true",
        help="Print the OWASP control register and verification result, then exit.",
    )
    args = parser.parse_args()

    if args.security_report:
        from utp.security import owasp

        print(owasp.report())
        result = owasp.verify_register()
        print(f"Register verification: {'PASS' if result['valid'] else 'FAIL'}")
        print(f"  controls with code references : {result['controls_with_code']}")
        print(f"  broken references             : {len(result['broken_references'])}")
        for broken in result["broken_references"]:
            print(f"    {broken['control']}: {broken['reference']} -> {broken['error']}")
        return 0 if result["valid"] else 1

    runner = unittest.TextTestRunner(
        verbosity=2 if args.verbose else 1, failfast=args.failfast, stream=sys.stdout
    )
    outcome = runner.run(build_suite(args.selectors))
    print()
    print(f"tests run : {outcome.testsRun}")
    print(f"failures  : {len(outcome.failures)}")
    print(f"errors    : {len(outcome.errors)}")
    print(f"skipped   : {len(outcome.skipped)}")
    print("RESULT    : " + ("PASS" if outcome.wasSuccessful() else "FAIL"))
    return 0 if outcome.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
