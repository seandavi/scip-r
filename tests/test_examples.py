"""Smoke test for examples/limma/call_graph.py on the fixture package."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from scipr import scip_pb2 as scip

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "limma" / "call_graph.py"


def _load_example():
    spec = importlib.util.spec_from_file_location("limma_call_graph", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.export
def test_call_graph_example_on_fixture(testpkg_index: scip.Index, tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    pytest.importorskip("networkx")
    from scipr.export import write_duckdb

    db = tmp_path / "fixture.duckdb"
    write_duckdb(testpkg_index, db)
    mod = _load_example()
    full, internal, pkg = mod.build_graphs(db)
    assert pkg["package"] == "testpkg"
    assert internal.number_of_nodes() == 11
    short = {mod._short(n) for n in internal.nodes()}
    assert {"zscore", "winsorize", "run_pipeline", "Counter#add", "width(Interval)"} <= short
    calls = {
        (mod._short(u), mod._short(v))
        for u, v, d in internal.edges(data=True)
        if d["kind"] == "call"
    }
    assert {
        ("run_pipeline", "zscore"),
        ("run_pipeline", "winsorize"),
        ("Counter#add", "Counter#n"),
    } <= calls
    impls = {
        (mod._short(u), mod._short(v))
        for u, v, d in internal.edges(data=True)
        if d["kind"] == "implements"
    }
    assert ("width(Interval)", "width") in impls
    ext = {mod._short(v) for u, v in full.edges() if not full.nodes[v]["internal"]}
    assert "mean" in ext and "sd" in ext

    metrics = mod.compute_metrics(full, internal)
    assert metrics["nodes_internal"] == 11
    assert dict(metrics["most_called"])["zscore"] >= 1
    assert metrics["recursive"] == [] and metrics["mutual_recursion"] == []
    assert any(pkg.startswith("stats") for pkg, _ in metrics["dependency_usage"])
    md = mod.metrics_markdown(metrics, pkg)
    assert md.startswith("# testpkg 0.1.0 call graph")

    assert mod.main([str(EXAMPLE), str(db), str(tmp_path / "out")]) == 0
    out = tmp_path / "out"
    assert (out / "call_graph.graphml").is_file() and (out / "call_graph.top.dot").is_file()
    assert json.loads((out / "metrics.json").read_text())["nodes_internal"] == 11
