"""Exception taxonomy for the statement processing pipeline.

Every exception in this hierarchy is a **fail-loud** signal.  The
pipeline never catches these to continue processing — they abort the
current run and leave raw rows intact with ``RunStatus.FAILED``.

Hierarchy::

    PipelineError (base)
    ├── InvariantError         — balance recon, bucket mismatch
    ├── TokenError             — unparseable money, sign contradiction
    ├── SchemaDriftError       — extra / missing headers
    ├── MissingSectionError    — missing mandatory anchors
    ├── AmbiguousAccountsError — multi-account on single adapter
    └── AdapterRegistryError   — 0 or 2+ adapter matches
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base exception for all pipeline errors.

    Subclasses represent specific failure modes.  Application code
    should never catch ``PipelineError`` to suppress; it exists for
    type hierarchy and diagnostic grouping only.
    """


class InvariantError(PipelineError):
    """A balance reconciliation or bucket-sum equation failed.

    Raised when:

    - ``closing != opening + SUM(signed amounts)``
    - Depository / card bucket totals do not equal line sums.
    - Running-balance column is inconsistent with cumulative sum.
    - Any 1-cent mismatch in a recon check.
    """


class TokenError(PipelineError):
    """A raw token could not be interpreted or contradicts its context.

    Raised when:

    - A money string has ≠ 2 decimal digits.
    - Category and sign contradict (e.g. ``purchase`` with ``-N``).
    - Column and glyph disagree on sign.
    - Ambiguous sign: one amount column, no glyph, no debit/credit
      header.
    - A zero-cent amount appears.
    """


class SchemaDriftError(PipelineError):
    """The table schema does not match the adapter's declared headers.

    Raised when:

    - An unexpected extra column appears.
    - A declared column is missing.
    - Column ordering does not match the declared schema.
    """


class MissingSectionError(PipelineError):
    """A mandatory section marker (anchor string) was not found.

    Raised when:

    - A required header or footer anchor is absent from the page text.
    - A required summary block (e.g. ``payment_due_date`` on default
      card schema) is missing.
    """


class AmbiguousAccountsError(PipelineError):
    """Multiple accounts detected by a single-account adapter.

    Raised when:

    - Two or more distinct account masks appear in the document.
    - Two or more primary balance sections are found.
    - The adapter is not a declared combined-statement adapter.
    """


class AdapterRegistryError(PipelineError):
    """Adapter matching failed: zero or multiple adapters claimed the file.

    Raised when:

    - No registered adapter's ``matches()`` returns ``True``.
    - Two or more adapters' ``matches()`` both return ``True`` and
      neither is a declared combined-statement adapter.
    """


class PersistenceError(PipelineError):
    """A database persistence or relational invariant violation occurred.

    Raised when:

    - Statement sidecar does not match account domain.
    - Denormalized raw_payload_id contradicts extraction_run.
    - Statement opening or closing balance contradicts sidecar totals.
    - Foreign key or schema violation during persistence.
    """
