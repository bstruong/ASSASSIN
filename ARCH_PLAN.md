# Architecture Plan: Step 11 Frontier-to-Local Router

## Objective
Build 'Step 11: Frontier-to-Local Router' for the `assassin` project. This requires building FastAPI endpoints and logic that orchestrate a Frontier-to-Local handoff:
- **Frontier Model:** Generates analytical SQL plans based ONLY on schemas.
- **Local Engine:** A FastAPI service (running at `127.0.0.1:8080/v1`) that executes the SQL plan against raw data using Tier 1 MCP tools.
- **PII Sanitization Interceptor/Middleware:** Scrub the payload before it is dispatched to the frontier model to ensure no raw data/PII leaks.

## Core Requirements & Invariants

This implementation must strictly adhere to the `AGENTS.md` invariants:

1. **Strict Contract Enforcement:**
   - The FastAPI endpoints must validate incoming payloads and fail loudly on invalid or malformed data.
   - Use explicit exceptions (e.g., from `app.models.exceptions`). No silent error suppression.

2. **Financial Precision:**
   - Any financial values must use integer cents (`int`). No floats.

3. **Code Clarity:**
   - Explicit AST clarity. No dynamic metaprogramming.

4. **Observability & PII Sanitization:**
   - Structured JSON logging required at all I/O boundaries.
   - The **PII Sanitization Interceptor/Middleware** MUST ensure that logs and outbound requests to the frontier model do NOT contain raw PII (account numbers, SSNs) or unmasked financial amounts.

## Test-Driven Development (TDD) Mandate
- **>90% Coverage:** All new modules must maintain at least 90% line and branch test coverage.
- **Mutation Testing:** Tests must be robust enough to kill mutations. Zero surviving mutants permitted in core validators.
- Write tests in `tests/routers/test_orchestrator.py` and `tests/middleware/test_sanitization.py` before implementation.

## Implementation Details
1. Create `app/routers/orchestrator.py` containing the FastAPI router for the Frontier-to-Local handoff.
2. Implement the **PII Sanitization Interceptor/Middleware** in `app/middleware/sanitization.py`.
3. Integrate with the existing Local Engine via Tier 1 MCP tools.

## Executable QA Script
- Create `scripts/qa_step11.py` that demonstrates the orchestrator's functionality.
- It must clearly print what is being verified, demonstrate successful processing, verify PII sanitization (positive verification), intentionally reject malformed inputs (negative verification), and show `[PASS]` / `[FAIL]` indicators.

## Task Checklist for Local Agent
1. Create a new git branch (e.g., `feat/step-11-frontier-router`).
2. Write unit tests for endpoints and the sanitization middleware (Red phase).
3. Implement `app/middleware/sanitization.py` and `app/routers/orchestrator.py` (Green phase).
4. Run `uv run ruff check .` and `uv run ruff format .`.
5. Run `uv run pytest --cov=app --cov-fail-under=90 -v`.
6. Run `uv run mutmut run && uv run mutmut export-cicd-stats && uv run python scripts/check_mutation_score.py`.
7. Run `uv run python scripts/agent_review.py`.
8. Write `scripts/qa_step11.py`.
9. Commit changes.
10. Open a Pull Request on GitHub.
