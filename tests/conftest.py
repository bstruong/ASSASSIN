"""Pytest fixtures shared across the ASSASSIN test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.canonical import RawStatement

FIXTURES_DIR: Path = Path(__file__).parent / "fixtures"


@pytest.fixture()
def schwab_sample_path() -> Path:
    """Return the absolute path to the Schwab sample JSON fixture."""
    path: Path = FIXTURES_DIR / "schwab_sample.json"
    assert path.is_file(), f"Fixture missing: {path}"
    return path


@pytest.fixture()
def schwab_raw_statement(schwab_sample_path: Path) -> RawStatement:
    """Load and parse the Schwab fixture into a ``RawStatement``.

    Uses ``model_validate_json`` so that Pydantic's native JSON parser
    handles ``str → date`` and ``str → enum`` coercion correctly under
    strict mode.
    """
    raw_json: str = schwab_sample_path.read_text(encoding="utf-8")
    return RawStatement.model_validate_json(raw_json)
