# ASSASSIN

**ASSASSIN** is a mathematically rigorous, fail-loud financial document extraction and reconciliation pipeline.

Built with an agent-driven development workflow, ASSASSIN enforces strict financial invariants (integer-cents precision), closed canonical schemas, and zero-PII egress. It transforms raw, ragged PDF statement data into perfectly balanced, mathematically proven ledger records.

## Current Project State

**Status:** `v1.0.0-rc` (Core Architecture Complete)

The foundational 9-step implementation sequence has been fully merged into `main`. The current capabilities include:
- **Canonical Data Models:** Frozen, immutable data models representing checking, savings, credit card, and custodial brokerage accounts.
- **Fail-Loud Extraction Core:** Strict TableSchema parsing that mathematically proves balance equations and actively crashes on 1-cent discrepancies or missing document sections.
- **Domain Adapters:** Mutually exclusive parsers for standard depositories, revolving credit, complex multi-account statements, and dual-reconciliation investment statements (Cash vs. Portfolio).
- **PostgreSQL Persistence:** Append-only raw data storage and canonical sidecar models ensuring zero data loss and flawless re-ingest idempotency.
- **Local Ingest API:** A FastAPI service (`/api/v1/ingest/upload`) with automated adapter routing, structured JSON logging, and mathematically guaranteed zero-PII egress.

## Agentic Governance & QA Foundation

This repository is built and maintained by autonomous AI agents governed by strict, automated CI/CD gates. Human contributors serve as Product Managers and QA Directors.

- **Mutation Testing:** The test suite utilizes `mutmut` to inject synthetic bugs into the AST. All core financial validators mathematically require a **100% Mutation Kill Rate**.
- **Automated AST Review:** The CI pipeline runs `scripts/agent_review.py` to intercept forbidden logic (e.g., `float` types, `eval`, or silent `except: pass` blocks).
- **Test-Driven Development (TDD):** Every feature requires strict TDD and maintains >90% line and branch coverage.

For more information on how agents operate in this repository, read [AGENTS.md](./AGENTS.md).

## Quick Start & Verification

**1. Install Dependencies**
```bash
uv sync
```

**2. Run the Invariant & QA Scripts**
The repository contains executable QA scripts that demonstrate the architecture without requiring code inspection:
```bash
# Verify the Mutation Testing and DevOps CI gates
uv run python scripts/qa_mutation_testing.py

# Verify the Local Ingest API
uv run python scripts/qa_step_9_ingest_api.py
```

**3. Run the Test Suite**
```bash
uv run pytest --cov=app --cov-fail-under=90 -v
```

**4. Run Linter & Formatter**
```bash
uv run ruff check .
uv run ruff format .
```

---
*This is a living document and is updated as the ASSASSIN project progresses.*
