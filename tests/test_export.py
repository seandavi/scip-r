from __future__ import annotations

from pathlib import Path

import pytest

from scipr import scip_pb2 as scip
from scipr.export import TABLES, MissingExtraError, index_to_rows


def test_index_to_rows_shape(testpkg_index: scip.Index) -> None:
    rows = index_to_rows(testpkg_index)
    assert tuple(rows) == TABLES
    assert len(rows["metadata"]) == 1
    assert rows["metadata"][0]["tool_name"] == "scip-r"
    assert rows["metadata"][0]["text_document_encoding"] == "UTF8"
    assert [d["relative_path"] for d in rows["documents"]] == [
        "R/classes.R",
        "R/pipeline.R",
        "R/stats_helpers.R",
    ]
    assert rows["documents"][2] == {
        "relative_path": "R/stats_helpers.R",
        "language": "R",
        "n_symbols": 2,
        "n_occurrences": 32,
    }
    assert len(rows["symbols"]) == 9
    assert len(rows["occurrences"]) == 69
    assert len(rows["external_symbols"]) == 15
    assert len(rows["relationships"]) == 1  # width(Interval). implements width().


def test_symbol_row_parsed_columns(testpkg_index: scip.Index) -> None:
    rows = index_to_rows(testpkg_index)
    z = next(r for r in rows["symbols"] if r["name"] == "zscore")
    assert z["relative_path"] == "R/stats_helpers.R"
    assert z["kind"] == "Function"
    assert z["package"] == "testpkg"
    assert z["version"] == "0.1.0"
    assert z["is_function"] is True
    assert z["is_local"] is False
    assert z["documentation"] == ["zscore <- function(x, na.rm = TRUE) {"]
    assert z["display_name"] is None


def test_external_rows_flag_guesses(testpkg_index: scip.Index) -> None:
    rows = index_to_rows(testpkg_index)
    by_name = {(r["package"], r["name"]): r for r in rows["external_symbols"]}
    assert by_name[("base", "mean")]["guessed"] is True
    assert by_name[("base", "sd")]["guessed"] is True  # NAMESPACE import, invisible statically
    assert by_name[("stats", "quantile")]["guessed"] is False
    assert all(r["relative_path"] is None for r in rows["external_symbols"])


def test_occurrence_rows(testpkg_index: scip.Index) -> None:
    rows = index_to_rows(testpkg_index)
    first = next(r for r in rows["occurrences"] if r["relative_path"] == "R/pipeline.R")
    assert first["symbol"] == "scip-r cran testpkg 0.1.0 run_pipeline()."
    assert (first["start_line"], first["start_char"], first["end_line"], first["end_char"]) == (
        1,
        0,
        1,
        12,
    )
    assert first["is_definition"] is True
    assert first["symbol_roles"] == scip.SymbolRole.Definition
    assert first["syntax_kind"] == "UnspecifiedSyntaxKind"
    local = next(r for r in rows["occurrences"] if r["is_local"])
    assert local["package"] is None and local["name"] is None


def test_multiline_occurrence_row_end_line() -> None:
    idx = scip.Index()
    doc = idx.documents.add(relative_path="R/a.R", language="R")
    occ = doc.occurrences.add(symbol="local 0")
    occ.range.extend([1, 2, 3, 4])
    row = index_to_rows(idx)["occurrences"][0]
    assert (row["start_line"], row["start_char"], row["end_line"], row["end_char"]) == (1, 2, 3, 4)


def test_relationship_rows() -> None:
    idx = scip.Index()
    doc = idx.documents.add(relative_path="R/a.R", language="R")
    info = doc.symbols.add(symbol="scip-r cran pkg 1.0 f().")
    info.relationships.add(symbol="scip-r cran pkg 1.0 g().", is_reference=True)
    rows = index_to_rows(idx)["relationships"]
    assert rows == [
        {
            "relative_path": "R/a.R",
            "symbol": "scip-r cran pkg 1.0 f().",
            "related_symbol": "scip-r cran pkg 1.0 g().",
            "is_reference": True,
            "is_implementation": False,
            "is_type_definition": False,
            "is_definition": False,
        }
    ]


def test_missing_extra_error_message() -> None:
    err = MissingExtraError("pyarrow")
    assert "pyarrow" in str(err)
    assert "scip-r[export]" in str(err)
    assert isinstance(err, ImportError)


# --- writers (need the optional extra) --------------------------------------


@pytest.mark.export
def test_index_to_arrow_schemas(testpkg_index: scip.Index) -> None:
    pa = pytest.importorskip("pyarrow")
    from scipr.export import index_to_arrow

    tables = index_to_arrow(testpkg_index)
    assert tuple(tables) == TABLES
    assert tables["occurrences"].num_rows == 69
    assert tables["occurrences"].schema.field("start_line").type == pa.int64()
    assert tables["symbols"].schema.field("documentation").type == pa.list_(pa.string())
    # empty tables still carry their schema
    assert tables["relationships"].num_rows == 1
    assert tables["relationships"].schema.names[:3] == [
        "relative_path",
        "symbol",
        "related_symbol",
    ]


@pytest.mark.export
def test_index_to_arrow_empty_index() -> None:
    pytest.importorskip("pyarrow")
    from scipr.export import index_to_arrow

    tables = index_to_arrow(scip.Index())
    assert tables["metadata"].num_rows == 1
    assert all(tables[t].num_rows == 0 for t in TABLES if t != "metadata")


@pytest.mark.export
def test_write_parquet_roundtrip(testpkg_index: scip.Index, tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    from scipr.export import write_parquet

    written = write_parquet(testpkg_index, tmp_path / "nested" / "out")
    assert set(written) == set(TABLES)
    occ = pq.read_table(written["occurrences"]).to_pylist()
    assert len(occ) == 69
    defs = [o for o in occ if o["is_definition"] and not o["is_local"]]
    assert sorted(o["name"] for o in defs) == [
        "Counter",
        "Counter",
        "Interval",
        "print.zresult",
        "run_pipeline",
        "width",
        "width",
        "winsorize",
        "zscore",
    ]
    ext = pq.read_table(written["external_symbols"]).to_pylist()
    rows = sorted((e["package"], e["name"], e["guessed"]) for e in ext)
    assert ("base", "mean", True) in rows and ("base", "sd", True) in rows
    assert [r for r in rows if not r[2]] == [("stats", "quantile", False)]


@pytest.mark.export
def test_write_duckdb_and_query(testpkg_index: scip.Index, tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    from scipr.export import write_duckdb

    db = tmp_path / "idx.duckdb"
    counts = write_duckdb(testpkg_index, db)
    assert counts == {
        "metadata": 1,
        "documents": 3,
        "symbols": 9,
        "external_symbols": 15,
        "occurrences": 69,
        "relationships": 1,
    }
    con = duckdb.connect(str(db), read_only=True)
    try:
        callers = con.execute(
            """
            select o.relative_path, o.name
            from occurrences o
            where not o.is_local and not o.is_definition and o.package = 'testpkg'
            order by 1, 2
            """
        ).fetchall()
        assert callers == [("R/pipeline.R", "winsorize"), ("R/pipeline.R", "zscore")]
        assert con.execute("select tool_name from metadata").fetchone() == ("scip-r",)
        doc = con.execute(
            "select documentation[1] from symbols where name = 'run_pipeline'"
        ).fetchone()
        assert doc == ("run_pipeline <- function(x, clip = TRUE) {",)
    finally:
        con.close()


@pytest.mark.export
def test_write_duckdb_overwrite_semantics(testpkg_index: scip.Index, tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    from scipr.export import write_duckdb

    db = tmp_path / "idx.duckdb"
    write_duckdb(testpkg_index, db)
    with pytest.raises(duckdb.CatalogException):
        write_duckdb(testpkg_index, db)
    write_duckdb(testpkg_index, db, overwrite=True)
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select count(*) from documents").fetchone() == (3,)
    finally:
        con.close()
