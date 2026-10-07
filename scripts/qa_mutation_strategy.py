#!/usr/bin/env python3
"""QA: Mutation strategy gates (scope, classic score, skip artifact, CI harness).

Usage:
    uv run python scripts/qa_mutation_strategy.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def check(name: str, passed: bool, detail: str = "") -> None:
    status = "[PASS]" if passed else "[FAIL]"
    print(f"{status} {name}")
    if detail:
        print(f"      {detail}")
    if not passed:
        raise AssertionError(name)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    print("QA: Mutation strategy recommendations 1-8")
    print("=" * 60)

    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    nightly = (root / ".github" / "workflows" / "mutation-nightly.yml").read_text(
        encoding="utf-8"
    )
    runner = (root / "scripts" / "run_mutation_on_diff.py").read_text(encoding="utf-8")
    checker = (root / "scripts" / "check_mutation_score.py").read_text(encoding="utf-8")

    check("1. Allowlist present in runner", "MUTATION_ALLOWLIST_PREFIXES" in runner)
    check("1. Critical validator always included", "CRITICAL_MODULES" in runner)
    check("1. API do_not_mutate in pyproject", '"app/api/*"' in pyproject)
    check("2. Classic score formula documented/used", "killed + survived" in checker)
    check("2. max-no-tests supported", "--max-no-tests" in checker)
    check(
        "3. Skip artifact uses skipped: true",
        '"skipped": True' in runner or 'skipped": True' in runner,
    )
    check(
        "3. No fabricated killed:1 total:1",
        '{"killed": 1, "survived": 0, "total": 1}' not in runner,
    )
    check(
        "4. mutmut run exit code checked",
        "mutmut run" in runner and "returncode" in runner,
    )
    check("5. setup.cfg removed", not (root / "setup.cfg").is_file())
    check(
        "5. E2E ignore in pyproject mutmut args",
        "--ignore=tests/test_e2e_dashboard.py" in pyproject,
    )
    check(
        "6. PR CI does not run mutation job",
        "mutation:" not in ci and "check_mutation_score.py" not in ci,
    )
    check(
        "6. Nightly workflow runs full allowlist mutation",
        "--full-allowlist" in nightly and "check_mutation_score.py" in nightly,
    )
    check(
        "6. Nightly mutation job skips Playwright install",
        "playwright install" not in nightly,
    )
    check(
        "7. Nightly cache fingerprint uses mutmut_ver + config_hash",
        "mutmut_ver" in nightly and "config_hash" in nightly,
    )
    check(
        "8. Dashboard documents out-of-mutation-scope",
        "out of mutmut scope"
        in (root / "app" / "api" / "dashboard.py").read_text(encoding="utf-8"),
    )

    # Live unit gate: historic HTMX-style stats must fail on no_tests, not classic score alone
    print("-" * 60)
    print("Evaluating historic HTMX-style stats via check_mutation_score helpers...")
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_mutation_gates.py",
            "-q",
            "--tb=short",
        ],
        cwd=root,
        check=False,
    )
    check("Unit tests for mutation gates pass", res.returncode == 0)

    # Skip artifact round-trip
    with tempfile.TemporaryDirectory() as tmp:
        stats = Path(tmp) / "mutmut-cicd-stats.json"
        stats.write_text(
            json.dumps(
                {
                    "skipped": True,
                    "skip_reason": "qa synthetic",
                    "killed": 0,
                    "survived": 0,
                    "total": 0,
                    "no_tests": 0,
                }
            ),
            encoding="utf-8",
        )
        # Import evaluate_stats
        sys.path.insert(0, str(root / "scripts"))
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_mutation_score", root / "scripts" / "check_mutation_score.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok, _ = mod.evaluate_stats(
            json.loads(stats.read_text(encoding="utf-8")),
            min_score=55.0,
            max_no_tests=0,
        )
        check("Skip artifact evaluates to PASS", ok is True)

    print("=" * 60)
    print("[PASS] Mutation strategy QA complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"[FAIL] {exc}")
        raise SystemExit(1) from exc
