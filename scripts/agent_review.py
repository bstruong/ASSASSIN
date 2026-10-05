#!/usr/bin/env python3
"""Automated Agent-to-Agent Review static analysis and invariant validator.

This script enforces the ASSASSIN core architectural invariants, static AST
safety rules, test coverage thresholds, and QA deliverable requirements across
the codebase before pull requests are approved.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


class ReviewViolation:
    """Represents a failed check or architectural invariant violation."""

    def __init__(self, rule: str, file_path: str, message: str) -> None:
        self.rule = rule
        self.file_path = file_path
        self.message = message

    def __str__(self) -> str:
        return f"[{self.rule}] {self.file_path}: {self.message}"


class InvariantAstScanner(ast.NodeVisitor):
    """AST visitor enforcing strict financial and code clarity invariants."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.violations: list[ReviewViolation] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Rule 1: Ban silent exception swallowing (except ...: pass)."""
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            name = ast.unparse(node.type) if node.type else "Bare Exception"
            self.violations.append(
                ReviewViolation(
                    rule="Strict Contract Enforcement",
                    file_path=f"{self.file_path}:{node.lineno}",
                    message=f"Silent exception swallowing detected: 'except {name}: pass'. Fail-loud required.",
                )
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        """Rule 2: Ban 'float' in canonical financial models."""
        if (
            "canonical.py" in self.file_path.name or "models" in str(self.file_path)
        ) and node.id == "float":
            self.violations.append(
                ReviewViolation(
                    rule="Financial Precision",
                    file_path=f"{self.file_path}:{node.lineno}",
                    message="Forbidden type 'float' detected in canonical financial models. Monetary values must be integer cents.",
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Rule 3: Ban dynamic evaluation (eval, exec) for AST clarity."""
        if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
            self.violations.append(
                ReviewViolation(
                    rule="Code Clarity",
                    file_path=f"{self.file_path}:{node.lineno}",
                    message=f"Dynamic evaluation function '{node.func.id}' is strictly forbidden.",
                )
            )
        self.generic_visit(node)


def scan_codebase_invariants(root_dir: Path) -> list[ReviewViolation]:
    """Scan all Python files under app/ for AST invariant violations."""
    violations: list[ReviewViolation] = []
    app_dir = root_dir / "app"
    if not app_dir.exists():
        return violations

    for py_file in app_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            scanner = InvariantAstScanner(py_file.relative_to(root_dir))
            scanner.visit(tree)
            violations.extend(scanner.violations)
        except SyntaxError as err:
            violations.append(
                ReviewViolation(
                    rule="Syntax Integrity",
                    file_path=str(py_file.relative_to(root_dir)),
                    message=f"Syntax error during AST parse: {err}",
                )
            )
    return violations


def run_command(cmd: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run a subprocess command and return exit code, stdout, and stderr."""
    res = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return res.returncode, res.stdout, res.stderr


def check_qa_deliverables(root_dir: Path) -> list[ReviewViolation]:
    """Verify that executable QA scripts exist in scripts/."""
    violations: list[ReviewViolation] = []
    scripts_dir = root_dir / "scripts"
    if not scripts_dir.exists():
        violations.append(
            ReviewViolation(
                rule="QA Deliverables",
                file_path="scripts/",
                message="Missing 'scripts/' directory for Product Manager executable QA verification.",
            )
        )
        return violations

    qa_scripts = list(scripts_dir.glob("qa_*.py"))
    if not qa_scripts:
        violations.append(
            ReviewViolation(
                rule="QA Deliverables",
                file_path="scripts/",
                message="No executable QA scripts found matching 'scripts/qa_*.py'. Every PR must provide an executable verification script.",
            )
        )
    return violations


def main() -> int:
    """Execute the full agent-to-agent review suite."""
    root_dir = Path(__file__).resolve().parent.parent
    print("=" * 70)
    print("🤖 ASSASSIN Autonomous Agent-to-Agent Review Validator")
    print("=" * 70)

    total_failures = 0

    # 1. AST Invariant Inspection
    print("\n[1/5] Scanning AST Invariants (Contracts, Precision, Clarity)...")
    ast_violations = scan_codebase_invariants(root_dir)
    if ast_violations:
        print("  ❌ Invariant violations detected:")
        for v in ast_violations:
            print(f"     - {v}")
        total_failures += len(ast_violations)
    else:
        print(
            "  ✅ All AST invariant checks passed (no floats, no silent swallowing, no dynamic eval)."
        )

    # 2. Ruff Linter
    print("\n[2/5] Running Ruff Linter (`uv run ruff check .`)...")
    code, out, err = run_command(["uv", "run", "ruff", "check", "."], cwd=root_dir)
    if code != 0:
        print("  ❌ Ruff linting failed:")
        print(out or err)
        total_failures += 1
    else:
        print("  ✅ Ruff linting passed cleanly.")

    # 3. Ruff Formatter
    print("\n[3/5] Checking Ruff Formatting (`uv run ruff format --check .`)...")
    code, out, err = run_command(
        ["uv", "run", "ruff", "format", "--check", "."], cwd=root_dir
    )
    if code != 0:
        print("  ❌ Ruff formatting check failed:")
        print(out or err)
        total_failures += 1
    else:
        print("  ✅ Codebase is properly formatted.")

    # 4. Pytest & Strict Coverage Gate (>90%)
    print("\n[4/5] Executing Test Suite & Coverage Gate (`--cov-fail-under=90`)...")
    code, out, err = run_command(
        ["uv", "run", "pytest", "--cov=app", "--cov-fail-under=90", "-q"],
        cwd=root_dir,
    )
    if code != 0:
        print("  ❌ Test suite or coverage threshold failed:")
        print(out or err)
        total_failures += 1
    else:
        print("  ✅ Test suite passed with coverage >= 90%.")

    # 5. QA Verification Deliverables
    print("\n[5/5] Verifying Product Manager QA Deliverables...")
    qa_violations = check_qa_deliverables(root_dir)
    if qa_violations:
        print("  ❌ QA deliverable violations:")
        for v in qa_violations:
            print(f"     - {v}")
        total_failures += len(qa_violations)
    else:
        qa_files = [f.name for f in (root_dir / "scripts").glob("qa_*.py")]
        print(f"  ✅ Executable QA scripts verified: {', '.join(qa_files)}")

    print("\n" + "=" * 70)
    if total_failures == 0:
        print("🎉 AGENT REVIEW: APPROVE (All automated gates & invariants satisfied)")
        print("=" * 70)
        return 0
    else:
        print(
            f"🚫 AGENT REVIEW: REQUEST_CHANGES ({total_failures} issue(s) require remediation)"
        )
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
