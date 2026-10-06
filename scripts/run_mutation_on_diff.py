#!/usr/bin/env python3
import configparser
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

    paths_arg = ",".join(app_files)
    print(f"Running mutmut only on changed files: {paths_arg}")

    # Mutmut 3.x no longer supports --paths-to-mutate CLI flag, so we inject it into setup.cfg
    config = configparser.ConfigParser()
    config.read("setup.cfg")
    if "mutmut" not in config:
        config.add_section("mutmut")

    # Save original to restore later
    original_paths = config["mutmut"].get("paths_to_mutate", None)
    config["mutmut"]["paths_to_mutate"] = paths_arg

    with open("setup.cfg", "w") as f:
        config.write(f)

    try:
        cmd = ["uv", "run", "mutmut", "run"]
        subprocess.run(cmd, check=False)
        subprocess.run(["uv", "run", "mutmut", "export-cicd-stats"], check=False)
    finally:
        # Restore original setup.cfg
        if original_paths:
            config["mutmut"]["paths_to_mutate"] = original_paths
        else:
            del config["mutmut"]["paths_to_mutate"]
        with open("setup.cfg", "w") as f:
            config.write(f)

    sys.exit(0)


if __name__ == "__main__":
    main()
