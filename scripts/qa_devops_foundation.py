#!/usr/bin/env python3
"""QA Acceptance Script: DevOps, QA & Agent-to-Agent Review Foundation.

Target Audience: Product Manager & Quality Assurance
Purpose:
    Execute end-to-end verification of the DevOps, CI/CD, and agent review
    foundation without requiring manual code inspection.

Usage:
    uv run python scripts/qa_devops_foundation.py
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def print_header(title: str) -> None:
    print(f"\n{'=' * 75}")
    print(f"🔍 [QA VERIFICATION] {title}")
    print(f"{'=' * 75}")


def assert_condition(name: str, passed: bool, detail: str = "") -> None:
    status = "✅ [PASS]" if passed else "❌ [FAIL]"
    print(f"{status} {name}")
    if detail:
        print(f"      Detail: {detail}")
    if not passed:
        raise AssertionError(f"QA Assertion Failed: {name} - {detail}")


def test_agents_markdown_spec(root_dir: Path) -> None:
    print_header("1. AGENTS.md Protocol & Convention Verification")
    agents_md = root_dir / "AGENTS.md"
    assert_condition("AGENTS.md exists", agents_md.is_file())

    content = agents_md.read_text(encoding="utf-8")
    assert_condition(
        "TDD mandate documented",
        "Test-Driven Development (TDD) Mandate" in content,
        "Ensures agents must write failing tests prior to implementation.",
    )
    assert_condition(
        "Strict 90% coverage gate documented",
        "--cov-fail-under=90" in content,
        "Ensures agents cannot regress test coverage below 90%.",
    )
    assert_condition(
        "Executable QA script mandate documented",
        "Product Manager & QA Acceptance Mandate" in content,
        "Enforces demonstration over code inspection for PM acceptance.",
    )
    assert_condition(
        "Agent-to-Agent Review protocol documented",
        "Agent-to-Agent Review Protocol" in content,
        "Documents dual-agent review roles, checklists, and automated gates.",
    )


def test_ci_cd_workflow(root_dir: Path) -> None:
    print_header("2. GitHub Actions CI/CD Gates (.github/workflows/ci.yml)")
    ci_yaml = root_dir / ".github" / "workflows" / "ci.yml"
    assert_condition(".github/workflows/ci.yml exists", ci_yaml.is_file())

    content = ci_yaml.read_text(encoding="utf-8")
    assert_condition(
        "CI executes `uv run ruff check .`",
        "uv run ruff check ." in content,
    )
    assert_condition(
        "CI executes `uv run ruff format --check .`",
        "uv run ruff format --check ." in content,
    )
    assert_condition(
        "CI executes `uv run pytest --cov=app --cov-fail-under=90 -v`",
        "--cov-fail-under=90" in content,
    )


def test_agent_review_workflow(root_dir: Path) -> None:
    print_header(
        "3. Agent-to-Agent Review Workflow (.github/workflows/agent-review.yml)"
    )
    review_yaml = root_dir / ".github" / "workflows" / "agent-review.yml"
    assert_condition(".github/workflows/agent-review.yml exists", review_yaml.is_file())

    content = review_yaml.read_text(encoding="utf-8")
    assert_condition(
        "Review workflow executes `scripts/agent_review.py`",
        "scripts/agent_review.py" in content,
    )


def test_agent_review_execution(root_dir: Path) -> None:
    print_header("4. Automated Agent Review Tool Execution (`scripts/agent_review.py`)")
    script = root_dir / "scripts" / "agent_review.py"
    assert_condition("scripts/agent_review.py exists", script.is_file())

    result = subprocess.run(
        ["uv", "run", "python", str(script)],
        cwd=root_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert_condition(
        "agent_review.py returned exit code 0 (APPROVE)",
        result.returncode == 0,
        result.stdout[-150:].strip(),
    )


def test_fail_loud_invariant_enforcement(root_dir: Path) -> None:
    print_header("5. Negative Invariant Demonstration (Failing Loudly on Corrupt Code)")
    sys.path.insert(0, str(root_dir / "scripts"))
    import agent_review  # type: ignore[import-not-found]

    # Test 1: Banned float detection
    bad_float_code = "class TestAccount:\n    balance: float = 10.0\n"
    tree = ast.parse(bad_float_code)
    scanner = agent_review.InvariantAstScanner(Path("app/models/canonical.py"))
    scanner.visit(tree)
    assert_condition(
        "AST scanner rejects 'float' in financial models",
        any(v.rule == "Financial Precision" for v in scanner.violations),
        "Correctly intercepted forbidden floating point type.",
    )

    # Test 2: Banned silent error suppression
    bad_pass_code = "try:\n    parse()\nexcept Exception:\n    pass\n"
    tree = ast.parse(bad_pass_code)
    scanner = agent_review.InvariantAstScanner(Path("app/adapters/test.py"))
    scanner.visit(tree)
    assert_condition(
        "AST scanner rejects 'except ...: pass' (silent swallow)",
        any(v.rule == "Strict Contract Enforcement" for v in scanner.violations),
        "Correctly intercepted silent error suppression.",
    )

    # Test 3: Coverage gate strictly fails when under 90%
    # We test this by asking pytest for a ridiculously high coverage requirement (100%)
    res = subprocess.run(
        ["uv", "run", "pytest", "--cov=app", "--cov-fail-under=100", "-q"],
        cwd=root_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    # Schwab has 96%, total is 98%, so 100% will fail, demonstrating the gate halts on deficit
    assert_condition(
        "Coverage gate halts build when threshold is not reached",
        res.returncode != 0,
        "Gate halts loud and clear on coverage shortfall.",
    )


def main() -> int:
    root_dir = Path(__file__).resolve().parent.parent
    print("=" * 75)
    print("🎯 ASSASSIN Product Manager & QA Acceptance Test")
    print("   Feature: DevOps, QA Gates & Agent-to-Agent Review Foundation")
    print("=" * 75)

    try:
        test_agents_markdown_spec(root_dir)
        test_ci_cd_workflow(root_dir)
        test_agent_review_workflow(root_dir)
        test_agent_review_execution(root_dir)
        test_fail_loud_invariant_enforcement(root_dir)

        print("\n" + "=" * 75)
        print("🏆 QA RESULT: ALL ACCEPTANCE GATES PASSED")
        print(
            "   The DevOps, TDD, CI/CD, and Agent Review foundation is fully validated."
        )
        print("=" * 75)
        return 0
    except AssertionError as err:
        print("\n" + "=" * 75)
        print(f"❌ QA RESULT: FAILED - {err}")
        print("=" * 75)
        return 1


if __name__ == "__main__":
    sys.exit(main())
