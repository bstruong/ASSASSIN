"""Unit tests for mutation CI gate scripts (no live mutmut run required)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _repo_root() -> Path:
    """Resolve repo root even when this file is mirrored under ``mutants/``.

    mutmut copies ``app/`` (and tests) into ``mutants/`` but not ``scripts/``.
    Walk parents until ``scripts/run_mutation_on_diff.py`` is found.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        script = candidate / "scripts" / "run_mutation_on_diff.py"
        if script.is_file():
            return candidate
    raise FileNotFoundError(
        "Could not locate scripts/run_mutation_on_diff.py from "
        f"{here} (mutmut mutants/ tree lacks scripts/; resolve from repo root)."
    )


ROOT = _repo_root()
SCRIPTS = ROOT / "scripts"


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner():
    return _load("run_mutation_on_diff", SCRIPTS / "run_mutation_on_diff.py")


@pytest.fixture(scope="module")
def checker():
    return _load("check_mutation_score", SCRIPTS / "check_mutation_score.py")


def test_repo_root_resolves_scripts() -> None:
    assert (ROOT / "scripts" / "run_mutation_on_diff.py").is_file()
    assert (ROOT / "pyproject.toml").is_file()


class TestAllowlistSelection:
    def test_dashboard_and_api_out_of_scope(self, runner) -> None:
        assert not runner.is_allowlisted("app/api/dashboard.py")
        assert not runner.is_allowlisted("app/api/app.py")
        assert not runner.is_allowlisted("app/mcp/cloud_tools.py")
        assert not runner.is_allowlisted("app/routers/orchestrator.py")

    def test_financial_modules_allowlisted(self, runner) -> None:
        assert runner.is_allowlisted("app/pipeline/validator.py")
        assert runner.is_allowlisted("app/adapters/schwab.py")
        assert runner.is_allowlisted("app/extraction/core.py")
        assert runner.is_allowlisted("app/models/enums.py")
        assert runner.is_allowlisted("app/db/repository.py")

    def test_only_api_diff_yields_empty_targets(self, runner) -> None:
        targets = runner.select_mutation_targets(
            ["app/api/dashboard.py", "app/api/app.py", "README.md"]
        )
        assert targets == []

    def test_adapter_diff_includes_critical_validator(self, runner) -> None:
        targets = runner.select_mutation_targets(["app/adapters/depository.py"])
        assert "app/adapters/depository.py" in targets
        assert "app/pipeline/validator.py" in targets

    def test_full_allowlist_includes_financial_surface(self, runner) -> None:
        targets = runner.list_allowlisted_modules()
        assert "app/pipeline/validator.py" in targets
        assert any(t.startswith("app/adapters/") for t in targets)
        assert all(runner.is_allowlisted(t) for t in targets)
        assert not any(t.startswith("app/api/") for t in targets)
        assert not any(t.startswith("app/mcp/") for t in targets)


class TestClassicScore:
    def test_score_excludes_no_tests_from_denominator(self, checker) -> None:
        # 43 killed, 30 survived, 22 no_tests → classic score uses 43/73 only
        score = checker.classic_mutation_score(killed=43, survived=30)
        assert score is not None
        assert abs(score - (43 / 73) * 100.0) < 1e-9

    def test_score_none_when_no_evaluated(self, checker) -> None:
        assert checker.classic_mutation_score(0, 0) is None


class TestEvaluateStats:
    def test_skip_artifact_passes(self, checker) -> None:
        ok, lines = checker.evaluate_stats(
            {
                "skipped": True,
                "skip_reason": "no allowlisted modules changed",
                "killed": 0,
                "survived": 0,
                "total": 0,
                "no_tests": 0,
            },
            min_score=55.0,
            max_no_tests=0,
        )
        assert ok is True
        assert any("SKIPPED" in line for line in lines)

    def test_htmx_style_stats_pass_classic_but_fail_no_tests(self, checker) -> None:
        # Historical failure mode: killed/total=45% but classic=43/73≈58.9%
        ok, lines = checker.evaluate_stats(
            {
                "killed": 43,
                "survived": 30,
                "total": 95,
                "no_tests": 22,
                "suspicious": 0,
                "timeout": 0,
                "segfault": 0,
            },
            min_score=55.0,
            max_no_tests=0,
        )
        assert ok is False
        joined = "\n".join(lines)
        assert "Classic mutation score" in joined
        assert "no_tests" in joined
        assert "FAILED" in joined

    def test_healthy_run_passes(self, checker) -> None:
        ok, lines = checker.evaluate_stats(
            {
                "killed": 80,
                "survived": 20,
                "total": 100,
                "no_tests": 0,
                "suspicious": 0,
                "timeout": 0,
                "segfault": 0,
            },
            min_score=55.0,
            max_no_tests=0,
        )
        assert ok is True
        assert any("PASSED: Classic mutation score" in line for line in lines)

    def test_low_classic_score_fails(self, checker) -> None:
        ok, _ = checker.evaluate_stats(
            {
                "killed": 10,
                "survived": 90,
                "total": 100,
                "no_tests": 0,
                "suspicious": 0,
                "timeout": 0,
                "segfault": 0,
            },
            min_score=55.0,
            max_no_tests=0,
        )
        assert ok is False

    def test_fabricated_100_percent_without_skip_flag_fails_when_empty(
        self, checker
    ) -> None:
        # Old skip path wrote killed=1,total=1 — still valid classic score.
        # Empty evaluated without skip must fail.
        ok, lines = checker.evaluate_stats(
            {
                "killed": 0,
                "survived": 0,
                "total": 0,
                "no_tests": 0,
                "suspicious": 0,
                "timeout": 0,
                "segfault": 0,
            },
            min_score=55.0,
            max_no_tests=0,
        )
        assert ok is False
        assert any("No mutants were evaluated" in line for line in lines)
