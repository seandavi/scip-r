"""Regression guard: the checked-in fixture index was produced by the
original prototype. Documents and external symbols must stay byte-identical
unless a change is deliberate (then regenerate with
``scip-r index tests/fixtures/testpkg -o tests/fixtures/testpkg-index.scip``
and explain the diff in the commit message)."""

from __future__ import annotations

from pathlib import Path

from scipr import load_index
from scipr import scip_pb2 as scip


def test_documents_match_golden(testpkg_index: scip.Index, testpkg_index_file: Path) -> None:
    golden = load_index(testpkg_index_file)
    assert [d.SerializeToString() for d in testpkg_index.documents] == [
        d.SerializeToString() for d in golden.documents
    ]


def test_external_symbols_match_golden(
    testpkg_index: scip.Index, testpkg_index_file: Path
) -> None:
    golden = load_index(testpkg_index_file)
    assert [s.SerializeToString() for s in testpkg_index.external_symbols] == [
        s.SerializeToString() for s in golden.external_symbols
    ]


def test_golden_metadata_shape(testpkg_index_file: Path) -> None:
    golden = load_index(testpkg_index_file)
    assert golden.metadata.tool_info.name == "scip-r"
    assert golden.metadata.text_document_encoding == scip.TextEncoding.UTF8
