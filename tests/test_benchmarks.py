"""Tests for benchmarks/sweep.py that need no network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from typer.testing import CliRunner

SWEEP = Path(__file__).resolve().parents[1] / "benchmarks" / "sweep.py"


def _load():
    spec = importlib.util.spec_from_file_location("sweep", SWEEP)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_parse_packages_index() -> None:
    mod = _load()
    recs = mod.parse_packages_index(
        "Package: limma\nVersion: 3.68.5\nDepends: R (>= 3.6.0)\n"
        "Imports: stats,\n        utils\n\n"
        "Package: edgeR\nVersion: 4.6.3\n\nGarbage: x\n"
    )
    assert [(r["Package"], r["Version"]) for r in recs] == [
        ("limma", "3.68.5"),
        ("edgeR", "4.6.3"),
    ]
    assert mod.tarball_name(recs[0]) == "limma_3.68.5.tar.gz"


def _result(pkg: str, **over) -> dict:
    base = {
        "source": f"/x/{pkg}_1.0.tar.gz",
        "package": pkg,
        "version": "1.0",
        "status": "ok",
        "summary": {
            "symbols": 10,
            "occurrences": 100,
            "external_symbols": 30,
            "guessed_external_symbols": 12,
        },
        "n_files": 3,
        "n_lines": 3000,
        "source_bytes": 90000,
        "parse_errors": 0,
        "parse_error_documents": 0,
        "diagnostics": None,
        "timings": {"unpack": 0.01, "index": 0.3, "write": 0.01, "total": 0.33},
        "peak_rss_mib": 120.0,
        "error": None,
    }
    base.update(over)
    return base


def test_analyse_and_render(tmp_path: Path) -> None:
    mod = _load()
    results = [
        _result("a"),
        _result("b", parse_errors=5, parse_error_documents=2, diagnostics="/x/b/diagnostics.json"),
        _result(
            "c",
            summary={
                "symbols": 0,
                "occurrences": 0,
                "external_symbols": 0,
                "guessed_external_symbols": 0,
            },
        ),
        _result(
            "d",
            status="error",
            summary=None,
            error="NotADirectoryError: nope\n  trace",
            timings={"total": 0.0},
        ),
    ]
    meta = {
        "scip_r_version": "0.2.0",
        "python": "3.13",
        "platform": "test",
        "cpu_count": 4,
        "jobs": 2,
        "elapsed_seconds": 2.0,
        "started_utc": "2026-09-14T00:00:00Z",
    }
    a = mod.analyse(results, meta)
    assert a["packages"] == {"total": 4, "ok": 3, "failed": 1}
    assert a["size"]["lines"] == 9000
    assert a["parse_errors"]["packages_with_errors"] == 1 and a["parse_errors"]["error_nodes"] == 5
    assert a["failures"]["by_kind"] == {"NotADirectoryError": 1}
    assert [r["package"] for r in a["implausible_output"]] == ["c"]
    assert a["most_parse_errors"][0]["package"] == "b"
    assert a["timing_total_s"]["p50"] == 0.33
    assert a["throughput"]["lines_per_cpu_second"] == 10000
    md = mod.render_report(a)
    assert md.startswith("# scip-r robustness sweep")
    assert "**3 indexed, 1 failed**" in md and "| b | 5 | 2 |" in md and "NotADirectoryError" in md

    # the report command round-trips through files
    out = tmp_path / "sweep"
    (out / "batch").mkdir(parents=True)
    (out / "batch" / "summary.jsonl").write_text("\n".join(json.dumps(r) for r in results) + "\n")
    (out / "batch" / "batch-meta.json").write_text(json.dumps(meta))
    res = CliRunner().invoke(mod.app, ["report", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert (out / "report.md").is_file() and json.loads((out / "report.json").read_text())[
        "packages"
    ]["ok"] == 3


def test_report_without_run_fails_cleanly(tmp_path: Path) -> None:
    mod = _load()
    res = CliRunner().invoke(mod.app, ["report", "--out", str(tmp_path)])
    assert res.exit_code == 2
