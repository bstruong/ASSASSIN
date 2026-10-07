# ASSASSIN Agent Configuration & Repository Conventions

This document serves as the authoritative source of conventions and instructions for all AI agents and contributors operating within the ASSASSIN repository.

## Environment & Runtime Commands

Agents must verify their work using the following commands:
- **Build/Dependencies:** `uv sync`
- **Lint:** `uv run ruff check .`
- **Format Check:** `uv run ruff format --check .` (Format fix: `uv run ruff format .`)
- **Test with Coverage Gate (>90%):** `uv run pytest --cov=app --cov-fail-under=90 -v`
- **Mutation Testing Gate:** `uv run mutmut run && uv run mutmut export-cicd-stats && uv run python scripts/check_mutation_score.py`
- **Agent Invariant Review:** `uv run python scripts/agent_review.py`

## Core Invariants

Any changes made to the codebase must strictly adhere to these invariants:

1. **Strict Contract Enforcement:**
   - Services must validate inputs and fail loudly on invalid payloads.
   - Do not implement silent coercion, skipping of malformed rows, or "best effort" parsing. If it's invalid, it fails with an explicit exception from `app.models.exceptions`.

2. **Financial Precision:**
   - All monetary values must be stored strictly as integer cents (`BIGINT` in database, `int` in Python).
   - No floating-point arithmetic (`float`, `Double`, etc.) is permitted for any monetary operations.

3. **Code Clarity:**
   - No dynamic metaprogramming or deep reflection.
   - Code must be highly readable and support static AST analysis. Explicit is always better than implicit.

4. **Observability:**
   - Structured JSON logging is required on all external I/O and processing boundaries.
   - Logs must provide sufficient context to reconstruct the state leading up to an error without manual debugging.
   - Never log raw PII (account numbers, SSNs, personal identities) or unmasked financial amounts (`amount_cents`, `amount`).

5. **Frontend Asset Integrity:**
   - Agents are strictly forbidden from hallucinating cryptographic hashes (SRI, SHA, MD5) or UUIDs for frontend assets.
   - You must either serve assets locally from `app/static/`, compute hashes programmatically, or omit them.

---


6. **Strict Separation of Orchestration and Implementation (No Cheating):**
   - Cloud Agents / Orchestrators are STRICTLY FORBIDDEN from writing application code, implementing features, or authoring tests themselves.
   - Cloud Agents may ONLY generate architectural plans (`ARCH_PLAN.md`), review Pull Requests, configure CI/CD infrastructure, and trigger the Tier 1 Local Generation Agent (e.g., via `berserker/harness/pi/bin/pi-up.sh`).
   - Under no circumstances may an Orchestrator bypass the local execution harness to save time. Writing code directly is considered a critical invariant violation and cheating.

## Test-Driven Development (TDD) Mandate

All feature development and adapter implementations must strictly adhere to Test-Driven Development (TDD):

1. **Phase 1: Red (Test First):**
   - Before writing any implementation code or schema parsing logic, write exhaustive unit and integration tests based on the feature specification, canonical contracts, and known negative invariant boundaries.
   - Tests must include both happy path assertions and explicit negative tests asserting that malformed tokens, schema drift, or equation violations fail loudly.
   - Verify tests fail before writing implementation code.

2. **Phase 2: Green (Make It Pass):**
   - Write the minimum contract-compliant code necessary to pass the test suite.
   - Do not introduce extraneous features, speculative abstractions, or bypassed validation checks.

3. **Phase 3: Refactor (Clean & Type-Safe):**
   - Refactor code for readability, type hints, and performance while ensuring AST clarity.
   - Run `uv run ruff check .` and `uv run ruff format --check .` to ensure compliance.

4. **Strict Test Coverage Standard (>90%):**
   - All modules in `app/` must maintain at least **90% line and branch test coverage**.
   - Any pull request dropping coverage below 90% is strictly blocked by CI (`uv run pytest --cov=app --cov-fail-under=90`).
   - Trivial tests that simply mock away validation without asserting invariant enforcement are unacceptable.

5. **Mutation Testing Mandate (Killing Injected Defects):**
   - High code coverage alone is meaningless if tests merely execute lines of code without validating outcomes. Tests must be robust enough to **kill mutations**.
   - Agents must run mutation testing via `uv run mutmut run && uv run mutmut export-cicd-stats`.
   - Injected mutations (such as operator inversions `<` vs `<=`, altered arithmetic signs, or deleted statements) must cause test failures.
   - Core financial validators (such as `app/pipeline/validator.py`) enforce a **100% mutation kill rate (zero surviving mutants)**.
   - All PRs are gated by `uv run python scripts/check_mutation_score.py --min-score 55.0 --max-no-tests 0` in CI.
   - Score is classic `killed/(killed+survived)`. `no_tests` fails separately. Only financial allowlisted modules are mutated; API/dashboard/MCP are out of scope.

---

## Product Manager & QA Acceptance Mandate

The human operator acts in the role of Quality Assurance (QA) and Product Manager (PM). They must be able to verify product functionality, reliability, and correctness **without reading code or diffs**.

Every pull request and feature deliverable **MUST** satisfy the following:

1. **Executable QA Script:**
   - Every PR must include an executable script (e.g., `scripts/qa_<feature_name>.py`) or explicit, copy-pasteable terminal instructions.
   - The script must be runnable via `uv run python scripts/qa_<feature_name>.py`.
   - The QA script must clearly print:
     - What feature or capability is being verified.
     - Successful processing of valid inputs (happy path).
     - Intentional, loud rejection of malformed or fraudulent inputs (negative verification).
     - Clear terminal indicators (`[PASS]` / `[FAIL]`) summarizing verification results.

2. **Structured Architecture & QA Artifact:**
   - Every PR must provide a Markdown artifact (or detailed PR description section) summarizing:
     - Architectural decisions made.
     - Core invariants verified (financial precision, contract enforcement, observability).
     - Test coverage report.
     - Exact instructions for the PM to execute the verification script.

---

## Agent-to-Agent Review Protocol

To ensure autonomous code quality and invariant compliance, every pull request must undergo automated Agent-to-Agent review before human acceptance.

### 1. Dual Agent Architecture
- **Implementation Agent:** Responsible for authoring contracts, writing failing tests (TDD), implementing features, authoring QA scripts/artifacts, and opening the PR.
- **Reviewer Agent:** An autonomous peer agent responsible for rigorous static analysis, invariant verification, and validation of QA deliverables.

### 2. Reviewer Checklist
Before a pull request can be merged, the Reviewer Agent verifies:
- [ ] **Financial Precision:** AST analysis passes; zero floating-point types used for monetary values.
- [ ] **Strict Contract Enforcement:** No silent error suppression (`except Exception: pass`), no best-effort coercion, explicit taxonomy exceptions raised.
- [ ] **Observability & Data Privacy:** Structured logging is configured at boundaries; no PII or financial amounts leaked.
- [ ] **TDD & Coverage Gate:** `uv run pytest --cov=app --cov-fail-under=90` passes with >90% coverage.
- [ ] **Mutation Testing Gate:** allowlisted modules mutated (or explicit `skipped: true`); classic score >= 55%; `no_tests == 0`; zero survivors in `app/pipeline/validator.py`.
- [ ] **Code Hygiene:** `uv run ruff check .` and `uv run ruff format --check .` pass with zero warnings.
- [ ] **QA Deliverables:** Executable QA script (`scripts/qa_*.py`) exists, runs successfully, and demonstrates both positive and negative cases.

### 3. Automated Review Gates
- **GitHub Actions CI (`.github/workflows/ci.yml`):** Automatically executes linting, formatting check, and the 90% coverage test gate on all PRs and pushes to `main`.
- **Agent Review Workflow (`.github/workflows/agent-review.yml`):** Automatically executes static invariant scans via `scripts/agent_review.py` and provides automated critique.

---

## Dual-Tier Workflow: Local Generation & Cloud Verification

To combine developer velocity with strict cloud validation, ASSASSIN utilizes a Dual-Tier Workflow across local and cloud environments:

### 1. Tier 1: Local Generation Agent (e.g., `qwen36-coder`)
- **Role:** Primary author and implementation agent.
- **Responsibilities:**
  - Operates locally to author canonical contracts and comprehensive TDD test suites (Red phase).
  - Implements contract-compliant business logic (Green phase) and refactors for static AST clarity (Refactor phase).
  - Authors executable QA verification scripts (`scripts/qa_<feature_name>.py`).
  - Executes local pre-flight checks (`uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest --cov=app --cov-fail-under=90 -v`, `uv run python scripts/agent_review.py`).
  - Commits changes and opens the Pull Request on GitHub.

### Process Execution & Hang Prevention (CRITICAL)
1. **No Dangling Background Processes:** Never run commands in the background using `&` unless absolutely necessary. If you must start a server or long-running process, you must kill it explicitly before ending your turn.
2. **Explicit Thread Cleanup:** All test scripts and execution wrappers must have explicit cleanup logic (e.g., `sys.exit(0)` in Python or `os.Exit(0)` in Go) to forcefully terminate all lingering background threads.
3. **Pipe Detachment:** If you absolutely must spawn a persistent background process, you must redirect its output to detach it from the execution pipe: `command > /dev/null 2>&1 &`.

## Troubleshooting & CI/CD Runbooks (For Future Agents)

### Mutation Testing (`mutmut`) Anomalies
* **`no_tests` (🫥) is a hard fail:** Untested / dead code on allowlisted modules fails `--max-no-tests 0`. Delete dead helpers or add assertion-heavy tests. Do **not** "fix" this by mutating presentation code or faking stats.
* **Wrong score denominator:** Never use `killed/total`. `total` includes `no_tests` and skips. Use classic `killed/(killed+survived)` via `scripts/check_mutation_score.py`.
* **Module Import Crashes (Exit Code 4):** `mutmut` isolates files in `mutants/`. Always keep `source_paths = ["app"]` and inject `only_mutate` via `scripts/run_mutation_on_diff.py`. Never point `source_paths` at a single file.
* **Out-of-scope diffs:** Changes only under `app/api/`, `app/mcp/`, `app/routers/`, `app/middleware/`, `app/core/` should skip mutation (`skipped: true`). Do not expand the allowlist to HTMX/dashboard just to satisfy the gate—cover those with unit/E2E tests.
* **Slow Mutations / E2E:** E2E tests are ignored in `[tool.mutmut]` and renamed `.bak` during diff runs. Playwright belongs on the **coverage** job only, not the mutation job.
* **Single config:** Do not reintroduce `setup.cfg` for mutmut; `[tool.mutmut]` in `pyproject.toml` is authoritative.

