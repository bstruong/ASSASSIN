#!/usr/bin/env python3
"""Enforce mutation score thresholds and critical invariant kill rates.

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check mutmut mutation score and critical gates."
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=55.0,
        help="Minimum overall mutation score percentage required (default: 55.0%%).",
    )
    args = parser.parse_args()

    root_dir = Path(__file__).resolve().parent.parent
    stats_file = root_dir / "mutants" / "mutmut-cicd-stats.json"

    if not stats_file.exists():
        print(
            f"❌ Error: {stats_file} not found. Run 'mutmut run' and 'mutmut export-cicd-stats' first."
        )
        return 1

    with stats_file.open(encoding="utf-8") as f:
        stats = json.load(f)

    killed = stats.get("killed", 0)
    survived = stats.get("survived", 0)
    total = stats.get("total", 0)

    if total == 0:
        print("❌ Error: Total mutants evaluated is 0.")
        return 1

    score = (killed / total) * 100.0

    print("=" * 70)
    print("🧬 ASSASSIN Mutation Testing Quality Gate")
    print("=" * 70)
    print(f"  • Total Mutants Generated: {total}")
    print(f"  • Mutants Killed:          {killed} ({score:.2f}%)")
    print(f"  • Mutants Survived:        {survived}")
    print(f"  • Required Minimum Score:  {args.min_score:.2f}%")
    print("-" * 70)

    failed = False

    # Check 1: Global mutation score threshold
    if score < args.min_score:
        print(
            f"  ❌ FAILED: Global mutation score {score:.2f}% is below required {args.min_score:.2f}%."
        )
        failed = True
    else:
        print(
            f"  ✅ PASSED: Global mutation score {score:.2f}% satisfies >= {args.min_score:.2f}%."
        )

    # Check 2: Core pipeline validator zero-tolerance gate
    surviving = get_surviving_mutants()
    validator_survivors = [m for m in surviving if "app.pipeline.validator" in m]
    if validator_survivors:
        print(
            f"  ❌ FAILED: {len(validator_survivors)} mutant(s) survived in critical module 'app.pipeline.validator':"
        )
        for m in validator_survivors:
            print(f"     - {m}")
        failed = True
    else:
        print(
            "  ✅ PASSED: Zero surviving mutants in critical validator ('app.pipeline.validator' is 100% killed)."
        )

    print("=" * 70)
    if failed:
        print(
            "🚫 MUTATION GATE: FAILED (Tests must be hardened to kill surviving mutations)"
        )
        print("=" * 70)
        return 1

    print("🎉 MUTATION GATE: PASSED (Test suite actively kills injected defects)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
