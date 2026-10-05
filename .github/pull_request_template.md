## Summary of Changes
[Provide a clear, human-readable summary of the changes introduced in this pull request. Explain the "why" and "what" without going into exhaustive line-by-line detail.]

## QA & Product Manager Verification
[Provide clear, non-technical instructions for the PM to execute and verify this deliverable without reading code.]
- **Executable QA Script:** `uv run python scripts/qa_<feature>.py`
- **Expected Outcome:** [Summary of expected terminal output, happy path demo, and fail-loud demonstrations]

## Verification & Test Coverage
- [ ] **Linter:** `uv run ruff check .` passes cleanly.
- [ ] **Formatter:** `uv run ruff format --check .` passes cleanly.
- [ ] **Coverage Gate:** `uv run pytest --cov=app --cov-fail-under=90 -v` passes.
- **Reported Coverage:** `[Insert % here, e.g. 98%]`
- [ ] **Mutation Testing Gate:** `mutmut` runs; tests actively kill synthetic mutations.
- **Reported Mutation Score:** `[Insert % here, e.g. 58.5%]`

## Architecture & Agent Review Invariants
Please ensure all applicable boxes are checked before requesting review:
- [ ] **Strict contract enforcement:** Validates inputs and fails loudly on invalid payloads.
- [ ] **Financial precision:** Uses integer cents (`BIGINT`); no floating-point arithmetic.
- [ ] **Code clarity:** Avoids dynamic metaprogramming; supports static AST analysis.
- [ ] **Observability:** Structured JSON logging at boundaries; zero PII or financial amount leakage.
- [ ] **TDD Compliance:** Tests were authored first for negative boundaries and contracts.
- [ ] **Mutation Invariant Gate:** Zero surviving mutants in core validators (`app/pipeline/validator.py`).
- [ ] **Agent Review Check:** `uv run python scripts/agent_review.py` returns `APPROVE`.

<!-- Explicit instruction: Do not include automated agent attribution footers or tool co-author trailers in PR descriptions or commit messages. -->
