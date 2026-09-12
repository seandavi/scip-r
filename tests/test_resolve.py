"""Tests for scip-r resolve.

The merge logic is tested against a canned resolve.R output
(tests/fixtures/testpkg-resolution.json) so it needs no R. The last test
runs the real script and is skipped when Rscript or pkgload is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scipr import build_index, load_index
from scipr import scip_pb2 as scip
from scipr.cli import app
from scipr.parser import GUESSED_NOTE
from scipr.resolve import (
    RESOLVED_NOTE,
    MergeStats,
    RscriptNotFoundError,
    find_rscript,
    guessed_names,
    merge_resolution,
    metadata_record,
    resolve_run_id,
    resolver_script,
)
from tests.conftest import FIXTURES, symbols_at

RESOLUTION = FIXTURES / "testpkg-resolution.json"


def _have_r() -> bool:
    if shutil.which("Rscript") is None:
        return False
    probe = subprocess.run(
        [
            "Rscript",
            "-e",
            "pk <- c('pkgload', 'codetools', 'jsonlite')\n"
            "quit(status = !all(sapply(pk, requireNamespace, quietly = TRUE)))",
        ],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_r = pytest.mark.skipif(
    not _have_r(), reason="needs Rscript with pkgload, codetools, jsonlite"
)


@pytest.fixture
def resolution() -> dict:
    return json.loads(RESOLUTION.read_text())


@pytest.fixture
def fresh_index(testpkg_dir: Path) -> scip.Index:
    return build_index(testpkg_dir)


def test_resolver_script_is_bundled() -> None:
    p = resolver_script()
    assert p.name == "resolve.R" and p.is_file()
    assert "pkgload::load_all" in p.read_text()


def test_guessed_names(fresh_index: scip.Index) -> None:
    names = guessed_names(fresh_index)
    assert "mean" in names and "sd" in names and "setMethod" in names
    assert "quantile" not in names  # explicit stats::quantile is not a guess
    assert names == sorted(names)


def test_find_rscript_explicit_missing(tmp_path: Path) -> None:
    with pytest.raises(RscriptNotFoundError):
        find_rscript(tmp_path / "nope")


def test_find_rscript_not_on_path(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(RscriptNotFoundError, match="not on PATH"):
        find_rscript()


def test_merge_rewrites_guessed_references(fresh_index: scip.Index, resolution: dict) -> None:
    stats = merge_resolution(fresh_index, resolution)
    assert stats.guessed_before == 14
    assert stats.resolved == 14 and stats.still_guessed == 0 and stats.resolved_to_self == 0
    occ = symbols_at(fresh_index, "R/stats_helpers.R")
    assert occ["scip-r cran base 4.6.0 mean()."] == [[2, 8, 12]]
    assert occ["scip-r cran stats 4.6.0 sd()."] == [[3, 7, 9]]  # was guessed base, is an import
    assert occ["scip-r cran stats 4.6.0 quantile()."] == [[9, 15, 23]]  # version filled
    assert stats.versions_filled == 1
    occ = symbols_at(fresh_index, "R/classes.R")
    assert occ["scip-r cran methods 4.6.0 setMethod()."] == [[5, 0, 9]]
    assert occ["scip-r cran R6 2.6.1 R6Class()."] == [[14, 11, 18]]
    assert not any("base . " in sym for sym in occ)


def test_merge_rebuilds_external_symbols(fresh_index: scip.Index, resolution: dict) -> None:
    merge_resolution(fresh_index, resolution)
    ext = {s.symbol: s for s in fresh_index.external_symbols}
    assert list(ext) == sorted(ext)
    assert not any(GUESSED_NOTE in s.documentation[0] for s in ext.values())
    assert ext["scip-r cran stats 4.6.0 sd()."].documentation == [
        f"stats::sd  {RESOLVED_NOTE.format(via='imports')}"
    ]
    assert ext["scip-r cran stats 4.6.0 quantile()."].documentation == [
        "stats::quantile  (version from the installed package)"
    ]
    # the S3 generic was added so the relationship target exists
    assert ext["scip-r cran base 4.6.0 print()."].documentation == ["base::print"]
    assert not any(s.symbol.startswith("scip-r cran testpkg") for s in ext.values())


def test_merge_adds_s3_relationship(fresh_index: scip.Index, resolution: dict) -> None:
    stats = merge_resolution(fresh_index, resolution)
    assert stats.s3_relationships == 1
    doc = fresh_index.documents[0]
    m = next(s for s in doc.symbols if s.symbol.endswith("print.zresult()."))
    assert [(r.symbol, r.is_implementation) for r in m.relationships] == [
        ("scip-r cran base 4.6.0 print().", True)
    ]


def test_merge_keeps_static_s4_relationship_without_duplicating(
    fresh_index: scip.Index, resolution: dict
) -> None:
    stats = merge_resolution(fresh_index, resolution)
    assert stats.s4_relationships == 0  # already present from the static pass
    doc = fresh_index.documents[0]
    m = next(s for s in doc.symbols if s.symbol.endswith("width(Interval)."))
    assert len(m.relationships) == 1
    assert stats.unmatched_methods == []


def test_merge_is_idempotent(fresh_index: scip.Index, resolution: dict) -> None:
    merge_resolution(fresh_index, resolution)
    first = fresh_index.SerializeToString()
    merge_resolution(fresh_index, resolution)
    assert fresh_index.SerializeToString() == first


def test_merge_stamps_run_id(fresh_index: scip.Index, resolution: dict) -> None:
    assert resolve_run_id(fresh_index) is None
    merge_resolution(fresh_index, resolution)
    assert resolve_run_id(fresh_index) == resolution["run_id"]
    args = list(fresh_index.metadata.tool_info.arguments)
    assert "resolver=scip-r-resolve/0.1.0" in args and "r_version=4.6.0" in args


def test_merge_leaves_unresolved_names_guessed(fresh_index: scip.Index, resolution: dict) -> None:
    resolution["resolutions"] = [r for r in resolution["resolutions"] if r["name"] != "mean"]
    stats = merge_resolution(fresh_index, resolution)
    assert stats.still_guessed == 1
    ext = {s.symbol: s for s in fresh_index.external_symbols}
    assert ext["scip-r cran base . mean()."].documentation == [f"base::mean  {GUESSED_NOTE}"]


def test_merge_resolution_to_self_package(fresh_index: scip.Index, resolution: dict) -> None:
    # pretend `mean` is defined by the package itself via assign()
    for r in resolution["resolutions"]:
        if r["name"] == "mean":
            r.update(package="testpkg", version="0.1.0", via="namespace")
    stats = merge_resolution(fresh_index, resolution)
    assert stats.resolved_to_self == 1
    assert symbols_at(fresh_index, "R/stats_helpers.R")["scip-r cran testpkg 0.1.0 mean()."]


def test_merge_reports_unmatched_methods(fresh_index: scip.Index, resolution: dict) -> None:
    resolution["methods"].append(
        {
            "system": "S3",
            "generic": "format",
            "generic_package": "base",
            "class": "x",
            "method": "format.x",
        }
    )
    resolution["methods"].append(
        {
            "system": "S4",
            "generic": "show",
            "generic_package": "methods",
            "signature": ["Nope"],
            "method": None,
        }
    )
    stats = merge_resolution(fresh_index, resolution)
    assert stats.unmatched_methods == ["S3 format.x", "S4 show(Nope)."]


def test_metadata_record(resolution: dict, tmp_path: Path) -> None:
    rec = metadata_record(
        resolution, MergeStats(resolved=3), index_path=tmp_path / "x.scip", index_bytes=b"abc"
    )
    assert rec["run_id"] == resolution["run_id"]
    assert rec["environment"]["r_version"] == "4.6.0"
    assert rec["summary"]["merge"]["resolved"] == 3
    assert rec["summary"]["s3_methods"] == 1
    assert rec["index"] == {
        "path": "x.scip",
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    }


def test_cli_resolve_without_rscript(
    testpkg_index_file: Path, testpkg_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = CliRunner().invoke(
        app,
        [
            "resolve",
            str(testpkg_index_file),
            "--pkg",
            str(testpkg_dir),
            "-o",
            str(tmp_path / "r.scip"),
        ],
    )
    assert result.exit_code == 4
    assert "Rscript is not on PATH" in result.output


def test_cli_resolve_with_canned_resolution(
    testpkg_index_file: Path, testpkg_dir: Path, tmp_path: Path, resolution: dict, monkeypatch
) -> None:
    import scipr.resolve as mod

    monkeypatch.setattr(mod, "run_resolver", lambda *a, **k: resolution)
    out = tmp_path / "nested" / "r.scip"
    result = CliRunner().invoke(
        app,
        [
            "resolve",
            str(testpkg_index_file),
            "--pkg",
            str(testpkg_dir),
            "-o",
            str(out),
            "--stats",
            "--keep-json",
            str(tmp_path / "raw.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "14 of 14 guessed references resolved" in result.output
    idx = load_index(out)
    assert resolve_run_id(idx) == resolution["run_id"]
    meta = json.loads((tmp_path / "nested" / "r.scip.meta.json").read_text())
    assert meta["run_id"] == resolution["run_id"]
    assert meta["index"]["path"] == "r.scip"
    import hashlib

    assert meta["index"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert json.loads((tmp_path / "raw.json").read_text())["run_id"] == resolution["run_id"]


@pytest.mark.export
def test_export_exposes_run_id(fresh_index: scip.Index, resolution: dict) -> None:
    from scipr.export import index_to_rows

    merge_resolution(fresh_index, resolution)
    md = index_to_rows(fresh_index)["metadata"][0]
    assert md["resolve_run_id"] == resolution["run_id"]
    assert f"resolve_run_id={resolution['run_id']}" in md["tool_arguments"]


@pytest.mark.r
@needs_r
def test_real_resolver_end_to_end(testpkg_dir: Path, tmp_path: Path) -> None:
    from scipr.resolve import resolve_index

    idx = build_index(testpkg_dir)
    result = resolve_index(idx, testpkg_dir)
    assert result.stats.still_guessed == 0
    assert result.stats.resolved == result.stats.guessed_before == 14
    assert result.stats.s3_relationships == 1
    assert result.resolution["package"] == {
        "name": "testpkg",
        "version": "0.1.0",
        "path": str(testpkg_dir.resolve()),
    }
    env = result.resolution["environment"]
    assert env["r_version"].count(".") == 2 and "stats" in env["packages"]
    assert len(result.resolution["run_id"]) == 32
    occ = symbols_at(idx, "R/stats_helpers.R")
    sd = next(s for s in occ if s.endswith(" sd()."))
    assert sd.startswith("scip-r cran stats ") and " . " not in sd
