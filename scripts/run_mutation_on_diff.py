#!/usr/bin/env python3
"""Diff-scoped mutation runner for financial-critical modules only.

Scope policy
------------
Only mutate allowlisted financial / contract modules. Presentation, MCP,
orchestrator, and telemetry glue are intentionally out of mutation scope;
those areas are gated by coverage and targeted unit/E2E tests instead.

Critical modules (e.g. ``app/pipeline/validator.py``) are always included
whenever a mutation run executes, even if they are not in the git diff.

Skip policy
-----------
When no allowlisted ``app/`` files changed, write an explicit skipped
stats artifact (``skipped: true``) and exit 0. Never fabricate a fake
100% kill rate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Financial / contract surface under mutation. Paths are repo-relative.
MUTATION_ALLOWLIST_PREFIXES: tuple[str, ...] = (
    "app/pipeline/",
    "app/adapters/",
    "app/extraction/",
    "app/models/",
    "app/db/repository.py",
    "app/db/connection.py",
    "app/db/__init__.py",
)

# Always mutate when a mutation run is performed (zero survivors required).
CRITICAL_MODULES: tuple[str, ...] = ("app/pipeline/validator.py",)

STATS_PATH = Path("mutants/mutmut-cicd-stats.json")
PYPROJECT = Path("pyproject.toml")
E2E_TEST = Path("tests/test_e2e_dashboard.py")
E2E_HIDDEN = Path("tests/test_e2e_dashboard.py.bak")


def get_changed_app_files() -> list[str]:
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            check=True,
            capture_output=True,
        )
        res = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = res.stdout.splitlines()
        return [f for f in files if f.startswith("app/") and f.endswith(".py")]
    except subprocess.CalledProcessError:
        return []


def is_allowlisted(path: str) -> bool:
    for prefix in MUTATION_ALLOWLIST_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix) or path == prefix.rstrip("/"):
                return True
        elif path == prefix:
            return True
    return False


def select_mutation_targets(changed_files: list[str]) -> list[str]:
    """Return sorted unique allowlisted targets, plus critical modules."""
    targets = {f for f in changed_files if is_allowlisted(f)}
    if not targets:
        return []
    for critical in CRITICAL_MODULES:
        if Path(critical).is_file():
            targets.add(critical)
    return sorted(targets)


def write_skipped_stats(*, reason: str) -> None:
    STATS_PATH.parent.mkdir(exist_ok=True)
    payload = {
        "skipped": True,
        "skip_reason": reason,
        "killed": 0,
        "survived": 0,
        "total": 0,
        "no_tests": 0,
        "suspicious": 0,
        "timeout": 0,
        "segfault": 0,
        "skipped_mutants": 0,
    }
    STATS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Mutation skipped: {reason}")
    print(f"Wrote skip artifact to {STATS_PATH}")


def inject_only_mutate(app_files: list[str]) -> str:
    """Patch pyproject.toml; return original contents for restore."""
    original = PYPROJECT.read_text(encoding="utf-8")
    app_files_str = ", ".join(f'"{f}"' for f in app_files)
    replacement = f'source_paths = ["app"]\nonly_mutate = [{app_files_str}]'
    if 'source_paths = ["app"]' not in original:
        raise RuntimeError(
            'pyproject.toml missing expected `source_paths = ["app"]` under [tool.mutmut]'
        )
    PYPROJECT.write_text(
        original.replace('source_paths = ["app"]', replacement, 1),
        encoding="utf-8",
    )
    return original


def main() -> int:
    changed = get_changed_app_files()
    targets = select_mutation_targets(changed)

    out_of_scope = sorted(f for f in changed if not is_allowlisted(f))
    if out_of_scope:
        print("Out-of-scope app/ changes (not mutated): " + ", ".join(out_of_scope))

    if not targets:
        write_skipped_stats(
            reason=(
                "No allowlisted financial modules changed versus origin/main "
                "(presentation/API/MCP/orchestrator diffs are out of mutation scope)."
            )
        )
        return 0

    print(f"Running mutmut on: {', '.join(targets)}", flush=True)

    original_pyproject = inject_only_mutate(targets)
    e2e_was_hidden = False
    if E2E_TEST.exists():
        E2E_TEST.rename(E2E_HIDDEN)
        e2e_was_hidden = True

    run_rc = 1
    export_rc = 1
    try:
        run_rc = subprocess.run(
            ["uv", "run", "mutmut", "run"],
            check=False,
        ).returncode
        if run_rc != 0:
            print(
                f"ERROR: `mutmut run` exited with code {run_rc}. "
                "Failing before score evaluation.",
                file=sys.stderr,
            )
            return run_rc if run_rc != 0 else 1

        export_rc = subprocess.run(
            ["uv", "run", "mutmut", "export-cicd-stats"],
            check=False,
        ).returncode
        if export_rc != 0:
            print(
                f"ERROR: `mutmut export-cicd-stats` exited with code {export_rc}.",
                file=sys.stderr,
            )
            return export_rc

        if not STATS_PATH.is_file():
            print(
                f"ERROR: expected stats file missing after export: {STATS_PATH}",
                file=sys.stderr,
            )
            return 1
    finally:
        if e2e_was_hidden and E2E_HIDDEN.exists():
            E2E_HIDDEN.rename(E2E_TEST)
        PYPROJECT.write_text(original_pyproject, encoding="utf-8")

    return 0


if __name__ == "__main__":
    sys.exit(main())
