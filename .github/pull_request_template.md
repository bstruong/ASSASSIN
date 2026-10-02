## Summary of Changes
[Provide a clear, human-readable summary of the changes introduced in this pull request. Explain the "why" and "what" without going into exhaustive line-by-line detail.]

## Verification
[Detail the exact commands run to verify this change and their output/results. e.g. `uv run pytest tests/ -v`]

## Architecture Invariants Checked
Please ensure all applicable boxes are checked before requesting a review.
- [ ] **Strict contract enforcement:** Validates inputs and fails loudly on invalid payloads.
- [ ] **Financial precision:** Uses integer cents (`BIGINT`); no floating-point arithmetic.
- [ ] **Code clarity:** Avoids dynamic metaprogramming; supports static AST analysis.
- [ ] **Observability:** Includes structured logging at all external I/O and processing boundaries.

<!-- Explicit instruction: Do not include automated agent attribution footers or tool co-author trailers in PR descriptions or commit messages. -->
