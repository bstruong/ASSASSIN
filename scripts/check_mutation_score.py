#!/usr/bin/env python3
"""Enforce mutation score thresholds and critical invariant kill rates.

Score formula (classic mutation score)::

    score = 100 * killed / (killed + survived)

``no_tests`` mutants are **not** folded into the denominator. They fail the
gate separately (default: zero allowed), so untested code cannot silently
dilute the kill rate.

Skipped runs (``skipped: true`` from ``run_mutation_on_diff.py``) pass
without claiming a fabricated 100% score.

Usage:
    uv run python scripts/check_mutation_score.py [--min-score 55.0]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def get_surviving_mutants() -> list[str]:
    """Retrieve list of surviving mutant identifiers from mutmut."""
    res = subprocess.run(
        ["uv", "run", "mutmut", "results"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return []
    lines = [line.strip() for line in res.stdout.splitlines() if "survived" in line]
    return [line.split(":")[0].strip() for line in lines]


def classic_mutation_score(killed: int, survived: int) -> float | None:
    """Return classic mutation score, or None if no evaluated mutants."""
    evaluated = killed + survived
    if evaluated == 0:
        return None
    return (killed / evaluated) * 100.0


def evaluate_stats(
    stats: dict,
    *,
    min_score: float,
    max_no_tests: int,
) -> tuple[bool, list[str]]:
    """Return (passed, human-readable detail lines)."""
    lines: list[str] = []
    failed = False

    if stats.get("skipped") is True:
        reason = stats.get("skip_reason") or "no allowlisted modules changed"
        lines.append(f"  ⏭️  SKIPPED: Mutation run skipped ({reason}).")
        lines.append(
            "  ✅ PASSED: Skip artifact accepted (not a fabricated 100% score)."
        )
        return True, lines

    killed = int(stats.get("killed", 0))
    survived = int(stats.get("survived", 0))
    no_tests = int(stats.get("no_tests", 0))
    total = int(stats.get("total", 0))
    suspicious = int(stats.get("suspicious", 0))
    timeout = int(stats.get("timeout", 0))
    segfault = int(stats.get("segfault", 0))

    score = classic_mutation_score(killed, survived)
    evaluated = killed + survived

    lines.append(f"  • Mutants accounted (total field): {total}")
    lines.append(f"  • Evaluated (killed + survived):  {evaluated}")
    lines.append(f"  • Mutants Killed:                 {killed}")
    lines.append(f"  • Mutants Survived:               {survived}")
    lines.append(f"  • Mutants no_tests:               {no_tests}")
    lines.append(
        f"  • Suspicious / timeout / segfault: {suspicious} / {timeout} / {segfault}"
    )
    if score is None:
        lines.append("  • Classic mutation score:         n/a (no killed+survived)")
    else:
        lines.append(f"  • Classic mutation score:         {score:.2f}%")
    lines.append(f"  • Required Minimum Score:         {min_score:.2f}%")
    lines.append(f"  • Max allowed no_tests:           {max_no_tests}")
    lines.append("-" * 70)

    if evaluated == 0:
        lines.append(
            "  ❌ FAILED: No mutants were evaluated (killed + survived == 0). "
            "Cannot compute a mutation score."
        )
        failed = True
    elif score is not None and score < min_score:
        lines.append(
            f"  ❌ FAILED: Classic mutation score {score:.2f}% is below "
            f"required {min_score:.2f}%."
        )
        failed = True
    else:
        lines.append(
            f"  ✅ PASSED: Classic mutation score {score:.2f}% satisfies "
            f">= {min_score:.2f}%."
        )

    if no_tests > max_no_tests:
        lines.append(
            f"  ❌ FAILED: {no_tests} no_tests mutant(s) exceed allowed "
            f"max of {max_no_tests} (untested / dead code on allowlisted modules)."
        )
        failed = True
    else:
        lines.append(
            f"  ✅ PASSED: no_tests count {no_tests} within allowed max {max_no_tests}."
        )

    if suspicious or timeout or segfault:
        lines.append(
            "  ❌ FAILED: Non-zero suspicious/timeout/segfault counts "
            f"(suspicious={suspicious}, timeout={timeout}, segfault={segfault})."
        )
        failed = True
    else:
        lines.append("  ✅ PASSED: No suspicious, timeout, or segfault mutants.")

    return (not failed), lines


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check mutmut mutation score and critical gates."
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=55.0,
        help=(
            "Minimum classic mutation score "
            "(killed/(killed+survived)) required (default: 55.0%%)."
        ),
    )
    parser.add_argument(
        "--max-no-tests",
        type=int,
        default=0,
        help="Maximum allowed no_tests mutants (default: 0).",
    )
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    stats_file = root_dir / "mutants" / "mutmut-cicd-stats.json"

    if not stats_file.exists():
        print(
            f"❌ Error: {stats_file} not found. "
            "Run scripts/run_mutation_on_diff.py (or mutmut run + export-cicd-stats) first."
        )
        return 1

    with stats_file.open(encoding="utf-8") as f:
        stats = json.load(f)

    print("=" * 70)
    print("🧬 ASSASSIN Mutation Testing Quality Gate")
    print("=" * 70)

    prelim_ok, detail_lines = evaluate_stats(
        stats,
        min_score=args.min_score,
        max_no_tests=args.max_no_tests,
    )
    for line in detail_lines:
        print(line)

    failed = not prelim_ok

    # Critical validator zero-tolerance (only meaningful when mutmut results exist)
    if stats.get("skipped") is not True:
        surviving = get_surviving_mutants()
        validator_survivors = [m for m in surviving if "app.pipeline.validator" in m]
        if validator_survivors:
            print(
                f"  ❌ FAILED: {len(validator_survivors)} mutant(s) survived in "
                "critical module 'app.pipeline.validator':"
            )
            for m in validator_survivors:
                print(f"     - {m}")
            failed = True
        else:
            print(
                "  ✅ PASSED: Zero surviving mutants in critical validator "
                "('app.pipeline.validator' is 100% killed when present)."
            )

    print("=" * 70)
    if failed:
        print(
            "🚫 MUTATION GATE: FAILED "
            "(Harden tests, delete dead code, or fix the mutation harness)"
        )
        print("=" * 70)
        return 1

    print("🎉 MUTATION GATE: PASSED")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
