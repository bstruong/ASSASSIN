"""Declared table schemas and section anchors for fail-loud extraction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TableSchema(BaseModel):
    """Specification of expected table column headers and mandatory section markers.

    Adapters declare exact sequences of headers and section markers.
    If the extracted table headers differ (extra, missing, or misordered columns),
    a SchemaDriftError is raised.
    If mandatory section markers are missing from the page text,
    a MissingSectionError is raised.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    name: str = Field(min_length=1)
    headers: tuple[str, ...] = Field(min_length=1)
    section_markers: tuple[str, ...] = Field(default_factory=tuple)
    allow_out_of_cycle: bool = False
