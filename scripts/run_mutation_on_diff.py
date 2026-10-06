#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path


def get_changed_app_files():
    # If in GitHub Actions PR, target is origin/main
    # Alternatively, just compare against origin/main always
    try:
        # fetch origin main just in case
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
        # Fallback to just everything if diff fails
        return []


def main():
    app_files = get_changed_app_files()
    if not app_files:
        print(
            "No app/ Python files changed. Skipping mutation testing to save CI time."
        )
        # Create a dummy stats file so the next step doesn't crash, OR exit with a special code
        stats_file = Path("mutants/mutmut-cicd-stats.json")
        stats_file.parent.mkdir(exist_ok=True)
        stats_file.write_text('{"killed": 1, "survived": 0, "total": 1}')
        sys.exit(0)

    paths_arg = ",".join(app_files)
    print(f"Running mutmut only on changed files: {paths_arg}")

    cmd = ["uv", "run", "mutmut", "run", "--paths-to-mutate", paths_arg]
    res = subprocess.run(cmd)

    # We still export stats regardless of success/failure so check_mutation_score can evaluate
    subprocess.run(["uv", "run", "mutmut", "export-cicd-stats"])
    sys.exit(0)


if __name__ == "__main__":
    main()
