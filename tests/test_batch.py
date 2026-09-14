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


def test_batch_records_sizes_timings_and_parse_errors(
    make_package: MakePackage, tmp_path: Path
) -> None:
    pkg = make_package(
        {"R/a.R": "f <- function() 1\n", "R/b.R": "g <- function(x) {\n  x +\n}\n"}, name="p"
    )
    out = tmp_path / "out"
    (r,) = run_batch([pkg], out, BatchOptions(), jobs=1)
    assert r.status == "ok"
    assert r.n_files == 2 and r.n_lines == 4 and r.source_bytes > 0
    assert r.parse_errors >= 1 and r.parse_error_documents == 1
    assert (
        r.diagnostics is not None
        and json.loads(Path(r.diagnostics).read_text())[0]["file"] == "R/b.R"
    )
    assert set(r.timings) == {"index", "write", "total"}
    assert r.timings["total"] >= r.timings["index"] > 0
    assert r.peak_rss_mib is None or r.peak_rss_mib > 0
    meta = json.loads((out / "batch-meta.json").read_text())
    assert meta["n_sources"] == 1 and meta["ok"] == 1 and meta["failed"] == 0
    assert meta["scip_r_version"] and meta["elapsed_seconds"] >= 0
    assert meta["options"]["export"] is None
    line = json.loads((out / "summary.jsonl").read_text().splitlines()[0])
    assert line["parse_errors"] == r.parse_errors and "timings" in line


def test_batch_tarball_records_unpack_timing(make_package: MakePackage, tmp_path: Path) -> None:
    import tarfile

    pkg = make_package({"R/a.R": "f <- function() 1\n"}, name="tp")
    tb = tmp_path / "tp_1.0.0.tar.gz"
    with tarfile.open(tb, "w:gz") as tf:
        tf.add(pkg, arcname="tp")
    (r,) = run_batch([tb], tmp_path / "out", BatchOptions(), jobs=1)
    assert r.status == "ok" and "unpack" in r.timings and r.parse_errors == 0
