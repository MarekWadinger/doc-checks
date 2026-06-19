"""Doc Check Runner — runs all self-healing documentation checks and reports results.

Usage:
    python -m doc_checks.runner            # Run all checks
    python -m doc_checks.runner make_refs  # Run specific check
    python -m doc_checks.runner --ci       # CI mode (exit 1 on any failure)
"""

import argparse
import importlib
import sys
import time
from collections.abc import Callable
from pathlib import Path

from doc_checks import CheckResult, get_config

_cfg = get_config()["runner"]
LABELS: dict[str, str] = _cfg["labels"]
_ORDER: list[str] = _cfg["order"]


def _discover_checks() -> dict[str, Callable[..., CheckResult]]:
    """Discover all check_*.py modules and return an ordered {name: run} dict."""
    here = Path(__file__).parent
    found: dict[str, Callable[..., CheckResult]] = {}
    for path in sorted(here.glob("check_*.py")):
        name = path.stem[len("check_") :]
        module = importlib.import_module(f"doc_checks.{path.stem}")
        if hasattr(module, "run"):
            found[name] = module.run

    # Return in configured order, then any remaining alphabetically
    ordered: dict[str, Callable[..., CheckResult]] = {}
    for name in _ORDER:
        if name in found:
            ordered[name] = found[name]
    for name in sorted(found):
        if name not in ordered:
            ordered[name] = found[name]
    return ordered


CHECKS: dict[str, Callable[..., CheckResult]] = _discover_checks()


def run_checks(selected: list[str] | None = None, verbose: bool = True, fail_fast: bool = False) -> bool:
    """Run selected (or all) doc checks. Returns True if all pass.

    Args:
        selected: List of check names to run, or None for all.
        verbose: Show detailed output per check.
        fail_fast: Stop on first failure (useful for CI).

    """
    checks_to_run = selected or list(CHECKS.keys())
    results: dict[str, CheckResult] = {}
    all_passed = True

    print(f"\n{'─' * 60}")
    print("  🔍 Self-Healing Documentation Checks")
    print(f"{'─' * 60}\n")

    for name in checks_to_run:
        if name not in CHECKS:
            print(f"  ⚠ Unknown check: '{name}'. Available: {', '.join(CHECKS.keys())}")
            continue

        label = LABELS.get(name, name)
        start = time.time()

        try:
            result = CHECKS[name]()
        except Exception as e:
            result = CheckResult(passed=False, message=f"Check crashed: {e}", details=[])

        elapsed = time.time() - start
        results[name] = result

        status = "✅ PASS" if result.passed else "❌ FAIL"
        if not result.passed:
            all_passed = False

        print(f"  {status}  {label} ({elapsed:.1f}s)")
        print(f"         {result.message}")

        if verbose and result.details:
            for line in result.details:
                print(f"       {line}")
            print()

        if fail_fast and not result.passed:
            print(f"\n  ⛔ Stopping early (--fail-fast). Fix '{name}' and re-run.")
            break

    print(f"\n{'─' * 60}")
    total = len(results)
    passed = sum(1 for r in results.values() if r.passed)
    failed = total - passed
    summary = f"  Results: {passed}/{total} passed"
    if failed:
        summary += f", {failed} failed"
    print(summary)
    print(f"{'─' * 60}\n")

    return all_passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run doc consistency checks",
        epilog=f"Available checks: {', '.join(CHECKS.keys())}",
    )
    parser.add_argument("checks", nargs="*", help="Specific checks to run (default: all)")
    parser.add_argument("--ci", action="store_true", help="CI mode: quiet output, exit 1 on failure")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress detailed output")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on first failure")
    parser.add_argument("--list", action="store_true", dest="list_checks", help="List available checks and exit")
    args = parser.parse_args()

    if args.list_checks:
        for name, label in LABELS.items():
            print(f"  {name:15s} {label}")
        sys.exit(0)

    all_passed = run_checks(
        selected=args.checks or None,
        verbose=not (args.quiet or args.ci),
        fail_fast=args.fail_fast,
    )

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
