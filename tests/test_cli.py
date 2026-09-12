from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scipr import __version__, load_index
from scipr.cli import app

runner = CliRunner()


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage: scip-r" in result.output
    assert "index" in result.output and "export" in result.output


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"scip-r {__version__}"


def test_index_writes_file(testpkg_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.scip"
    result = runner.invoke(app, ["index", str(testpkg_dir), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert result.output == ""
    idx = load_index(out)
    assert len(idx.documents) == 3


def test_index_default_output_name(testpkg_dir: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["index", str(testpkg_dir)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "index.scip").is_file()


def test_index_stats_and_positions(testpkg_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.scip"
    pos = tmp_path / "pos.json"
    result = runner.invoke(
        app,
        ["index", str(testpkg_dir), "-o", str(out), "--stats", "--emit-positions", str(pos)],
    )
    assert result.exit_code == 0, result.output
    assert f"{out}: 3 documents, 11 defined symbols, 73 occurrences" in result.output
    positions = json.loads(pos.read_text())
    assert {
        "file": "R/stats_helpers.R",
        "line": 2,
        "character": 8,
        "name": "mean",
        "enclosing": "zscore",
    } in positions
    assert f"{pos}: {len(positions)} guessed positions" in result.output


def test_index_missing_dir_fails(tmp_path: Path) -> None:
    result = runner.invoke(app, ["index", str(tmp_path / "missing")])
    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_index_file_instead_of_dir_fails(testpkg_dir: Path) -> None:
    result = runner.invoke(app, ["index", str(testpkg_dir / "DESCRIPTION")])
    assert result.exit_code == 2
    assert "neither a directory nor a" in result.output


def test_stats_text(testpkg_index_file: Path) -> None:
    result = runner.invoke(app, ["stats", str(testpkg_index_file)])
    assert result.exit_code == 0, result.output
    assert "3 documents, 11 defined symbols, 73 occurrences" in result.output
    assert "external packages: R6 (1), base (10), methods (3), stats (2)" in result.output
    assert "R/pipeline.R: 1 symbols, 11 occurrences (4 definitions)" in result.output


def test_stats_json(testpkg_index_file: Path) -> None:
    result = runner.invoke(app, ["stats", str(testpkg_index_file), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["documents"] == 3
    assert data["external_packages"] == {"R6": 1, "base": 10, "methods": 3, "stats": 2}


def test_stats_missing_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["stats", str(tmp_path / "nope.scip")])
    assert result.exit_code != 0


def test_print_text(testpkg_index_file: Path) -> None:
    result = runner.invoke(app, ["print", str(testpkg_index_file)])
    assert result.exit_code == 0, result.output
    assert "== R/stats_helpers.R (R)" in result.output
    assert "local 0" in result.output


def test_print_no_locals(testpkg_index_file: Path) -> None:
    result = runner.invoke(app, ["print", str(testpkg_index_file), "--no-locals"])
    assert result.exit_code == 0, result.output
    assert "local " not in result.output
    assert "zscore()." in result.output


def test_print_json(testpkg_index_file: Path) -> None:
    result = runner.invoke(app, ["print", str(testpkg_index_file), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["metadata"]["tool_info"]["name"] == "scip-r"
    assert [d["relative_path"] for d in data["documents"]] == [
        "R/classes.R",
        "R/pipeline.R",
        "R/stats_helpers.R",
    ]


@pytest.mark.export
def test_export_parquet_default_dir(testpkg_index_file: Path, tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    idx_copy = tmp_path / "my-index.scip"
    idx_copy.write_bytes(testpkg_index_file.read_bytes())
    result = runner.invoke(app, ["export", str(idx_copy), "--format", "parquet"])
    assert result.exit_code == 0, result.output
    out_dir = tmp_path / "my-index-parquet"
    assert sorted(p.name for p in out_dir.iterdir()) == [
        "documents.parquet",
        "external_symbols.parquet",
        "metadata.parquet",
        "occurrences.parquet",
        "relationships.parquet",
        "symbols.parquet",
    ]


@pytest.mark.export
def test_export_duckdb_explicit_output(testpkg_index_file: Path, tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "idx.duckdb"
    result = runner.invoke(app, ["export", str(testpkg_index_file), "-f", "duckdb", "-o", str(db)])
    assert result.exit_code == 0, result.output
    assert "occurrences: 73 rows" in result.output
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert con.execute("select count(*) from occurrences").fetchone() == (73,)
    finally:
        con.close()
    # second run without --overwrite fails; with it succeeds
    again = runner.invoke(app, ["export", str(testpkg_index_file), "-f", "duckdb", "-o", str(db)])
    assert again.exit_code != 0
    again = runner.invoke(
        app, ["export", str(testpkg_index_file), "-f", "duckdb", "-o", str(db), "--overwrite"]
    )
    assert again.exit_code == 0, again.output


def test_export_requires_format(testpkg_index_file: Path) -> None:
    result = runner.invoke(app, ["export", str(testpkg_index_file)])
    assert result.exit_code != 0
    assert "--format" in result.output


def test_export_missing_extra_gives_clear_error(
    testpkg_index_file: Path, tmp_path: Path, monkeypatch
) -> None:
    import scipr.export as export_mod
    from scipr.export import MissingExtraError

    def boom(*_args, **_kwargs):
        raise MissingExtraError("pyarrow")

    monkeypatch.setattr(export_mod, "write_parquet", boom)
    result = runner.invoke(
        app, ["export", str(testpkg_index_file), "-f", "parquet", "-o", str(tmp_path / "p")]
    )
    assert result.exit_code == 3
    assert "scip-r[export]" in result.output


def test_index_creates_missing_output_dirs(testpkg_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "deep" / "er" / "index.scip"
    pos = tmp_path / "other" / "positions.json"
    result = runner.invoke(
        app, ["index", str(testpkg_dir), "-o", str(out), "--emit-positions", str(pos)]
    )
    assert result.exit_code == 0, result.output
    assert out.is_file() and pos.is_file()


@pytest.mark.export
def test_export_creates_missing_output_dirs(testpkg_index_file: Path, tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    db = tmp_path / "nested" / "dir" / "idx.duckdb"
    result = runner.invoke(app, ["export", str(testpkg_index_file), "-f", "duckdb", "-o", str(db)])
    assert result.exit_code == 0, result.output
    assert db.is_file()


def _make_tarball(src_dir: Path, dest: Path) -> Path:
    import tarfile

    with tarfile.open(dest, "w:gz") as tf:
        tf.add(src_dir, arcname=src_dir.name)
    return dest


def test_index_tarball(testpkg_dir: Path, tmp_path: Path) -> None:
    from scipr.parser import index_arguments

    tb = _make_tarball(testpkg_dir, tmp_path / "testpkg_0.1.0.tar.gz")
    out = tmp_path / "tb.scip"
    result = runner.invoke(app, ["index", str(tb), "-o", str(out), "--stats"])
    assert result.exit_code == 0, result.output
    assert "3 documents, 11 defined symbols, 73 occurrences" in result.output
    idx = load_index(out)
    stamps = index_arguments(idx)
    assert stamps["source_tarball"] == "testpkg_0.1.0.tar.gz"
    assert len(stamps["source_sha256"]) == 64
    assert idx.metadata.project_root == tb.resolve().as_uri()


def test_index_manager_override(testpkg_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "m.scip"
    result = runner.invoke(
        app, ["index", str(testpkg_dir), "-o", str(out), "--manager", "bioconductor"]
    )
    assert result.exit_code == 0, result.output
    idx = load_index(out)
    assert idx.documents[1].symbols[0].symbol.startswith("scip-r bioconductor testpkg ")


def test_resolve_requires_exactly_one_source(testpkg_index_file: Path, testpkg_dir: Path) -> None:
    result = runner.invoke(app, ["resolve", str(testpkg_index_file)])
    assert result.exit_code == 2 and "exactly one" in result.output
    result = runner.invoke(
        app, ["resolve", str(testpkg_index_file), "--pkg", str(testpkg_dir), "--installed", "x"]
    )
    assert result.exit_code == 2


@pytest.mark.export
def test_export_parquet_hive(testpkg_index_file: Path, tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    out = tmp_path / "hive"
    result = runner.invoke(
        app, ["export", str(testpkg_index_file), "-f", "parquet", "--hive", "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    parts = sorted(p.relative_to(out).parts[:3] for p in out.rglob("*.parquet"))
    assert ("occurrences", "index_package=testpkg", "index_version=0.1.0") in parts


def test_batch_cli(testpkg_dir: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    (other / "R").mkdir(parents=True)
    (other / "DESCRIPTION").write_text("Package: other\nVersion: 0.1\n")
    (other / "R" / "a.R").write_bytes(b"f <- function() 1\n")
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(f"# comment\n{testpkg_dir}\n")
    out = tmp_path / "out"
    result = runner.invoke(app, ["batch", str(other), "--manifest", str(manifest), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "2 ok, 0 failed" in result.output
    lines = [json.loads(line) for line in (out / "summary.jsonl").read_text().splitlines()]
    assert sorted(r["package"] for r in lines) == ["other", "testpkg"]
    assert (out / "testpkg" / "index.scip").is_file() and (out / "other" / "index.scip").is_file()


def test_batch_cli_reports_failures(tmp_path: Path) -> None:
    manifest = tmp_path / "m.txt"
    manifest.write_text(str(tmp_path / "does-not-exist") + "\n")
    out = tmp_path / "out"
    result = runner.invoke(app, ["batch", "--manifest", str(manifest), "-o", str(out)])
    assert result.exit_code == 1
    assert "1 failed" in result.output


def test_batch_cli_no_inputs(tmp_path: Path) -> None:
    result = runner.invoke(app, ["batch", "-o", str(tmp_path / "o")])
    assert result.exit_code == 2
