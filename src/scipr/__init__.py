"""scip-r: a static SCIP indexer for R packages.

Emits a SCIP (https://github.com/scip-code/scip) index for an R package's
source, using tree-sitter-r for parsing. No R runtime required.

Public API::

    from scipr import build_index, load_index, summarize

    index = build_index("path/to/pkg")      # -> scip_pb2.Index
    summary = summarize(index)              # -> IndexSummary
"""

from __future__ import annotations

__version__ = "0.2.0"

from .inspect import IndexSummary, load_index, summarize, write_index
from .parser import build_index
from .symbols import ParsedSymbol, parse_symbol

__all__ = [
    "IndexSummary",
    "ParsedSymbol",
    "__version__",
    "build_index",
    "load_index",
    "parse_symbol",
    "summarize",
    "write_index",
]
