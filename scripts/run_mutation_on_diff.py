#!/usr/bin/env python3
"""Mutation runner for financial-critical modules.

Modes
-----
* **Diff-scoped (default):** mutate allowlisted files changed vs ``origin/main``.
  When none changed, write ``skipped: true`` and exit 0.
* **Full allowlist (``--full-allowlist``):** mutate every allowlisted module.
  Used by the nightly / main-required mutation workflow.

Scope policy
------------
Only mutate allowlisted financial / contract modules. Presentation, MCP,
orchestrator, and telemetry glue are intentionally out of mutation scope;
those areas are gated by coverage and targeted unit/E2E tests instead.

Critical modules (e.g. ``app/pipeline/validator.py``) are always included
whenever a mutation run executes, even if they are not in the git diff.
"""

from __future__ import annotations

import argparse
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
    normalized = path.replace("\\", "/")
    for prefix in MUTATION_ALLOWLIST_PREFIXES:
        if prefix.endswith("/"):
            if normalized.startswith(prefix) or normalized == prefix.rstrip("/"):
                return True
        elif normalized == prefix:
            return True
    return False


def list_allowlisted_modules() -> list[str]:
    """Return every existing allowlisted ``*.py`` path plus critical modules."""
    targets: set[str] = set()
    for prefix in MUTATION_ALLOWLIST_PREFIXES:
        if prefix.endswith("/"):
            root = Path(prefix)
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                rel = path.as_posix()
                if is_allowlisted(rel):
                    targets.add(rel)
        elif Path(prefix).is_file():
            targets.add(prefix)
    for critical in CRITICAL_MODULES:
        if Path(critical).is_file():
            targets.add(critical)
    return sorted(targets)


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


def run_mutation(targets: list[str]) -> int:
    """Inject only_mutate, run mutmut + export, restore pyproject. Return exit code."""
    print(f"Running mutmut on: {', '.join(targets)}", flush=True)

    original_pyproject = inject_only_mutate(targets)
    e2e_was_hidden = False
    if E2E_TEST.exists():
        E2E_TEST.rename(E2E_HIDDEN)
        e2e_was_hidden = True

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-allowlist",
        action="store_true",
        help=(
            "Mutate every allowlisted financial module (nightly / main gate). "
            "Default is diff-scoped versus origin/main."
        ),
    )
    args = parser.parse_args(argv)

    if args.full_allowlist:
        targets = list_allowlisted_modules()
        if not targets:
            write_skipped_stats(reason="Full allowlist requested but no modules found.")
            return 1
        return run_mutation(targets)

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

    return run_mutation(targets)


if __name__ == "__main__":
    sys.exit(main())
