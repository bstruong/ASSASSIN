# ASSASSIN Agent Configuration & Repository Conventions

This document serves as the authoritative source of conventions and instructions for all AI agents and contributors operating within the ASSASSIN repository.

## Environment & Runtime Commands

Agents must verify their work using the following commands:
- **Build/Dependencies:** `uv sync`
- **Test:** `uv run pytest tests/ -v`
- **Lint:** `uv run ruff check .`
- **Format:** `uv run ruff format .`

## Core Invariants

Any changes made to the codebase must strictly adhere to these invariants:

1. **Strict Contract Enforcement:**
   - Services must validate inputs and fail loudly on invalid payloads.
   - Do not implement silent coercion, skipping of malformed rows, or "best effort" parsing. If it's invalid, it fails.

2. **Financial Precision:**
   - All monetary values must be stored strictly as integer cents (`BIGINT` in database, `int` in Python).
   - No floating-point arithmetic (`float`, `Double`, etc.) is permitted for any monetary operations.

3. **Code Clarity:**
   - No dynamic metaprogramming or deep reflection.
   - Code must be highly readable and support static AST analysis. Explicit is always better than implicit.

4. **Observability:**
   - Structured logging is required on all external I/O and processing boundaries.
   - Logs must provide sufficient context to reconstruct the state leading up to an error without manual debugging.
