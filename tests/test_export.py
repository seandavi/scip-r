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
        "index_package": "testpkg",
        "index_version": "0.1.0",
        "index_manager": "cran",
        "resolve_run_id": None,
        "relative_path": "R/stats_helpers.R",
        "language": "R",
        "n_symbols": 2,
        "n_occurrences": 32,
    }
    assert len(rows["symbols"]) == 11
    assert len(rows["occurrences"]) == 73
    assert len(rows["external_symbols"]) == 16
    assert len(rows["relationships"]) == 2  # width(Interval). -> width(); print.zresult -> print


def test_symbol_row_parsed_columns(testpkg_index: scip.Index) -> None:
    rows = index_to_rows(testpkg_index)
    z = next(r for r in rows["symbols"] if r["name"] == "zscore")
    assert z["relative_path"] == "R/stats_helpers.R"
    assert z["kind"] == "Function"
    assert z["package"] == "testpkg"
    assert z["version"] == "0.1.0"
    assert z["is_function"] is True
    assert z["is_local"] is False
    assert z["documentation"] == ["zscore <- function(x, na.rm = TRUE) {", "@export"]
    assert z["exported"] is True
    assert z["display_name"] is None
    assert z["manager"] == "cran" and z["owner"] is None and z["is_member"] is False
    p = next(r for r in rows["symbols"] if r["name"] == "print.zresult")
    assert p["exported"] is False
    add = next(r for r in rows["symbols"] if r["name"] == "add")
    assert (add["owner"], add["is_member"], add["is_function"], add["kind"]) == (
        "Counter",
        True,
        True,
        "Method",
    )


def test_external_rows_flag_guesses(testpkg_index: scip.Index) -> None:
    rows = index_to_rows(testpkg_index)
    by_name = {(r["package"], r["name"]): r for r in rows["external_symbols"]}
    assert by_name[("base", "mean")]["guessed"] is True
    assert by_name[("stats", "sd")]["guessed"] is False  # importFrom(stats, sd)
    assert by_name[("stats", "quantile")]["guessed"] is False
    assert by_name[("base", "print")]["guessed"] is True  # S3 generic, package guessed
    assert by_name[("base", "mean")]["manager"] == "."
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
            "index_package": None,
            "index_version": None,
            "index_manager": None,
            "resolve_run_id": None,
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
    assert tables["occurrences"].num_rows == 73
    assert tables["occurrences"].schema.field("start_line").type == pa.int64()
    assert tables["occurrences"].schema.field("caller").type == pa.string()
    assert tables["symbols"].schema.field("documentation").type == pa.list_(pa.string())
    assert tables["relationships"].num_rows == 2
    assert tables["relationships"].schema.names[:5] == [
        "index_package",
        "index_version",
        "index_manager",
        "resolve_run_id",
        "relative_path",
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
    assert len(occ) == 73
    defs = [o for o in occ if o["is_definition"] and not o["is_local"]]
    assert sorted(o["name"] for o in defs) == [
        "Counter",
        "Counter",
        "Interval",
        "add",
        "n",
        "print.zresult",
        "run_pipeline",
        "width",
        "width",
        "winsorize",
        "zscore",
    ]
    assert all(o["index_package"] == "testpkg" for o in occ)
    ext = pq.read_table(written["external_symbols"]).to_pylist()
    rows = sorted((e["package"], e["name"], e["guessed"]) for e in ext)
    assert ("base", "mean", True) in rows and ("stats", "sd", False) in rows
    assert ("stats", "quantile", False) in rows


@pytest.mark.export
def test_write_duckdb_and_query(testpkg_index: scip.Index, tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    from scipr.export import write_duckdb

    db = tmp_path / "idx.duckdb"
    counts = write_duckdb(testpkg_index, db)
    assert counts == {
        "metadata": 1,
        "documents": 3,
        "symbols": 11,
        "external_symbols": 16,
        "occurrences": 73,
        "relationships": 2,
    }
    con = duckdb.connect(str(db), read_only=True)
    try:
        callers = con.execute(
            """
            select o.relative_path, o.name
            from occurrences o
            where not o.is_local and not o.is_definition and not o.is_member
              and o.package = 'testpkg'
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


# --- review-gap columns ------------------------------------------------------------


def test_index_columns(testpkg_index: scip.Index) -> None:
    from scipr.export import index_columns

    assert index_columns(testpkg_index) == {
        "index_package": "testpkg",
        "index_version": "0.1.0",
        "index_manager": "cran",
        "resolve_run_id": None,
    }
    assert index_columns(scip.Index()) == dict.fromkeys(index_columns(testpkg_index))


def test_caller_column(testpkg_index: scip.Index) -> None:
    occ = index_to_rows(testpkg_index)["occurrences"]
    by = {(r["relative_path"], r["start_line"], r["start_char"]): r for r in occ}
    mean = by[("R/stats_helpers.R", 2, 8)]
    assert mean["name"] == "mean" and mean["caller"] == "scip-r cran testpkg 0.1.0 zscore()."
    zdef = by[("R/stats_helpers.R", 1, 0)]
    assert zdef["is_definition"] and zdef["caller"] is None
    local = by[("R/stats_helpers.R", 2, 2)]  # mu <-
    assert local["is_local"] and local["caller"] == "scip-r cran testpkg 0.1.0 zscore()."
    inv = next(r for r in occ if r["name"] == "invisible" and r["start_line"] == 19)
    assert inv["caller"] == "scip-r cran testpkg 0.1.0 Counter#add()."
    top = next(r for r in occ if r["name"] == "representation")
    assert top["caller"] == "scip-r cran testpkg 0.1.0 Interval#"  # inside the setClass call
    sd = next(r for r in occ if r["name"] == "sd")
    assert sd["internal_access"] is False


def test_internal_access_column(make_package) -> None:
    from scipr import build_index

    root = make_package({"R/a.R": "f <- function() { ext:::s(); ext::o() }\n"})
    rows = index_to_rows(build_index(root))["occurrences"]
    flags = {r["name"]: r["internal_access"] for r in rows if r["package"] == "ext"}
    assert flags == {"s": True, "o": False}


@pytest.mark.export
def test_write_parquet_hive_layout(testpkg_index: scip.Index, tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    from scipr.export import write_parquet

    written = write_parquet(testpkg_index, tmp_path / "hive", hive=True)
    assert written["occurrences"] == tmp_path / "hive" / "occurrences"
    files = list((tmp_path / "hive" / "occurrences").rglob("*.parquet"))
    assert len(files) == 1
    assert files[0].parent.name == "index_version=0.1.0"
    assert files[0].parent.parent.name == "index_package=testpkg"
    table = pq.read_table(str(tmp_path / "hive" / "occurrences"))
    assert table.num_rows == 73
    assert "index_package" in table.column_names  # restored from the path
