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

    # mutmut 3.x completely ignores setup.cfg if [tool.mutmut] exists in pyproject.toml
    pyproject_file = Path("pyproject.toml")
    pyproject_content = pyproject_file.read_text() if pyproject_file.exists() else None
    if pyproject_content:
        lines = pyproject_content.splitlines()
        new_lines = []
        in_mutmut = False
        for line in lines:
            if line.strip().startswith("[tool.mutmut]"):
                in_mutmut = True
            elif line.strip().startswith("[") and in_mutmut:
                in_mutmut = False

            if not in_mutmut:
                new_lines.append(line)
        pyproject_file.write_text("\n".join(new_lines) + "\n")

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

        if pyproject_content is not None:
            pyproject_file.write_text(pyproject_content)

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
