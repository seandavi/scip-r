from __future__ import annotations

import json
from pathlib import Path

import pytest

from scipr.batch import BatchOptions, discover_sources, index_one, run_batch
from tests.conftest import MakePackage


def test_discover_sources(make_package: MakePackage, tmp_path: Path) -> None:
    a = make_package({"R/a.R": ""}, name="a")
    b = make_package({"R/b.R": ""}, name="b")
    (tmp_path / "junk").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    manifest = tmp_path / "m.txt"
    manifest.write_text("# c\na\n\n")
    found = discover_sources([tmp_path], manifest)
    assert found == [a, b, tmp_path / "a"]


def test_index_one_and_run_batch(make_package: MakePackage, tmp_path: Path) -> None:
    a = make_package({"R/a.R": "f <- function() g()\ng <- function() 1\n"}, name="a")
    b = make_package({"R/b.R": "h <- function() f()\n"}, name="b", version="2.0")
    out = tmp_path / "out"
    results = run_batch([a, b], out, BatchOptions(), jobs=1)
    assert [(r.package, r.version, r.status) for r in results] == [
        ("a", "1.0.0", "ok"),
        ("b", "2.0", "ok"),
    ]
    assert results[0].summary["symbols"] == 2
    assert (out / "a" / "index.scip").is_file() and (out / "b" / "index.scip").is_file()
    lines = (out / "summary.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["status"] == "ok"


def test_run_batch_isolates_failures(make_package: MakePackage, tmp_path: Path) -> None:
    good = make_package({"R/a.R": "f <- function() 1\n"}, name="good")
    bad = tmp_path / "does-not-exist"
    results = run_batch([bad, good], tmp_path / "out", BatchOptions(), jobs=1)
    by = {r.source: r for r in results}
    assert by[str(good)].status == "ok"
    assert by[str(bad)].status == "error" and "NotADirectoryError" in (by[str(bad)].error or "")


def test_run_batch_parallel(make_package: MakePackage, tmp_path: Path) -> None:
    pkgs = [make_package({"R/a.R": f"f{i} <- function() {i}\n"}, name=f"p{i}") for i in range(3)]
    results = run_batch(pkgs, tmp_path / "out", BatchOptions(), jobs=2)
    assert sorted(r.package for r in results) == ["p0", "p1", "p2"]
    assert all(r.status == "ok" for r in results)


@pytest.mark.export
def test_batch_export_hive(make_package: MakePackage, tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    a = make_package({"R/a.R": "f <- function() 1\n"}, name="a")
    b = make_package({"R/b.R": "g <- function() 2\n"}, name="b")
    out = tmp_path / "out"
    run_batch([a, b], out, BatchOptions(export="parquet", hive=True), jobs=1)
    parts = sorted(p.parent.parent.name for p in (out / "parquet" / "symbols").rglob("*.parquet"))
    assert parts == ["index_package=a", "index_package=b"]


@pytest.mark.export
def test_index_one_duckdb_export(make_package: MakePackage, tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    a = make_package({"R/a.R": "f <- function() 1\n"}, name="a")
    r = index_one(a, tmp_path / "out", BatchOptions(export="duckdb"))
    assert r.status == "ok" and (tmp_path / "out" / "a" / "index.duckdb").is_file()
