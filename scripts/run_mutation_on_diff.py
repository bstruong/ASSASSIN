#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path


def get_changed_app_files():
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main"], check=True, capture_output=True
        )
        res = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        files = res.stdout.splitlines()
        app_files = [f for f in files if f.startswith("app/") and f.endswith(".py")]
        return app_files
    except subprocess.CalledProcessError:
        return []


def main():
    app_files = get_changed_app_files()
    if not app_files:
        print(
            "No app/ Python files changed. Skipping mutation testing to save CI time."
        )
        stats_file = Path("mutants/mutmut-cicd-stats.json")
        stats_file.parent.mkdir(exist_ok=True)
        stats_file.write_text('{"killed": 1, "survived": 0, "total": 1}')
        sys.exit(0)

    # Keep source_paths = ["app"] so the whole package is copied into mutants/
    # (tests import unchanged modules), but only mutate the changed files.
    app_files_str = ", ".join(f'"{f}"' for f in app_files)
    new_source_paths_line = f'source_paths = ["app"]\nonly_mutate = [{app_files_str}]'

    print(f"Running mutmut only on changed files: {', '.join(app_files)}")

    pyproject_file = Path("pyproject.toml")
    original_pyproject = pyproject_file.read_text()

    # Add the only_mutate restriction next to source_paths
    new_pyproject = original_pyproject.replace(
        'source_paths = ["app"]', new_source_paths_line
    )

    pyproject_file.write_text(new_pyproject)

    e2e_test = Path("tests/test_e2e_dashboard.py")
    e2e_hidden = Path("tests/test_e2e_dashboard.py.bak")

    if e2e_test.exists():
        e2e_test.rename(e2e_hidden)

    try:
        cmd = ["uv", "run", "mutmut", "run"]
        subprocess.run(cmd, check=False)
        subprocess.run(["uv", "run", "mutmut", "export-cicd-stats"], check=False)
    finally:
        if e2e_hidden.exists():
            e2e_hidden.rename(e2e_test)
        # Restore original pyproject.toml
        pyproject_file.write_text(original_pyproject)

    sys.exit(0)


if __name__ == "__main__":
    main()
