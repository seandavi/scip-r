from __future__ import annotations

from pathlib import Path

from scipr import load_index, summarize, write_index
from scipr import scip_pb2 as scip
from scipr.inspect import format_range, render_text


def test_write_and_load_roundtrip(testpkg_index: scip.Index, tmp_path: Path) -> None:
    out = write_index(testpkg_index, tmp_path / "idx.scip")
    assert out.is_file()
    assert load_index(out) == testpkg_index


def test_summarize_counts(testpkg_index: scip.Index) -> None:
    s = summarize(testpkg_index)
    assert (s.tool_name, s.documents, s.symbols, s.occurrences) == ("scip-r", 2, 3, 42)
    assert s.definitions == 13
    assert s.references == 29
    assert s.definitions + s.references == s.occurrences
    assert s.local_occurrences == 33
    assert s.external_symbols == 4
    assert s.guessed_external_symbols == 2
    assert s.external_packages == {"base": 2, "stats": 2}
    assert [
        (d.relative_path, d.symbols, d.occurrences, d.definitions) for d in s.per_document
    ] == [
        ("R/pipeline.R", 1, 11, 4),
        ("R/stats_helpers.R", 2, 31, 9),
    ]


def test_summary_to_dict_and_one_line(testpkg_index: scip.Index) -> None:
    s = summarize(testpkg_index)
    d = s.to_dict()
    assert d["documents"] == 2
    assert d["per_document"][0]["relative_path"] == "R/pipeline.R"
    assert s.one_line().startswith("2 documents, 3 defined symbols, 42 occurrences")


def test_summarize_empty_index() -> None:
    s = summarize(scip.Index())
    assert s.documents == 0 and s.occurrences == 0 and s.external_packages == {}


def test_format_range() -> None:
    assert format_range([0, 0, 5]) == "1:1-1:6"
    assert format_range([2, 3, 4, 1]) == "3:4-5:2"


def test_render_text(testpkg_index: scip.Index) -> None:
    text = render_text(testpkg_index)
    assert text.startswith("# scip-r ")
    assert "== R/pipeline.R (R)" in text
    assert "symbol Function   scip-r cran testpkg 0.1.0 zscore()." in text
    assert "2:1-2:13       def  scip-r cran testpkg 0.1.0 run_pipeline()." in text
    assert "local 0" in text
    assert "== external symbols" in text
    assert text.endswith("\n")


def test_render_text_without_locals(testpkg_index: scip.Index) -> None:
    text = render_text(testpkg_index, show_locals=False)
    assert "local " not in text
    assert "run_pipeline()." in text
