"""Abstract base class that every brokerage statement adapter must implement.

Adapters are responsible for two concerns:

1. **Detection** - given a file path, determine whether this adapter can
   handle the document (e.g. by inspecting the first page for broker-specific
   watermarks or layout cues).
2. **Extraction** - parse the document into a :class:`~app.models.canonical.RawStatement`.

Adapters MUST NOT silently skip or coerce malformed data.  If a field
cannot be extracted with certainty, raise a ``ValueError`` with a
descriptive message.
"""

from __future__ import annotations

import abc
from pathlib import Path

from app.models.canonical import RawStatement


class StatementAdapter(abc.ABC):
    """Contract that every brokerage-specific parser must satisfy.

    Sub-classes are discovered at runtime and matched against incoming
    documents via :meth:`matches`.  The first adapter that claims a
    document is used for extraction.
    """

    @abc.abstractmethod
    def matches(self, file_path: Path) -> bool:
        """Return ``True`` if this adapter can parse the file at *file_path*.

        Implementations should perform lightweight heuristic checks
        (e.g. scanning the first PDF page for a broker logo or header
        string) without fully parsing the document.

        Args:
            file_path: Absolute or relative path to a PDF statement.

        Returns:
            ``True`` when the adapter is confident it can handle this file.
        """

    @abc.abstractmethod
    def parse(self, file_path: Path) -> RawStatement:
        """Extract structured data from the statement at *file_path*.

        The returned :class:`~app.models.canonical.RawStatement` must
        satisfy the cash-balance invariant documented on
        :class:`~app.models.canonical.StatementSummary`.

        Args:
            file_path: Path to the PDF file to parse.

        Returns:
            A fully populated ``RawStatement``.

        Raises:
            FileNotFoundError: If *file_path* does not exist.
            ValueError: If the document is malformed or contains
                ambiguous / unparseable data.
        """
