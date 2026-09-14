"""Build call graphs from a scip-r DuckDB export with networkx.

Usage::

    python call_graph.py limma.duckdb out/

Reads the ``occurrences``, ``symbols``, ``external_symbols`` and
``relationships`` tables, builds

- ``full``: every call edge, callers are package functions, callees may be
  in the package or in a dependency (nodes carry ``package`` and
  ``manager``);
- ``internal``: the subgraph of package-to-package calls, plus
  ``implements`` edges from S3/S4 methods to their generics;

and writes GraphML for both, a DOT file of the busiest internal nodes
(rendered to PNG when Graphviz ``dot`` is on PATH), and ``metrics.json`` /
``metrics.md`` with the numbers a maintainer usually wants: most-called
helpers, PageRank, exported entry points and their reach, internal
functions nobody calls, recursion cycles, and dependency usage.

Requires ``duckdb`` (the scip-r export extra) and ``networkx``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import networkx as nx

EDGE_SQL = """
select o.caller as source, o.symbol as target, count(*) as weight,
       any_value(o.package) as target_package, any_value(o.manager) as target_manager,
       any_value(o.name) as target_name, any_value(o.is_member) as target_is_member,
       any_value(o.is_method) as target_is_method, any_value(o.internal_access) as internal
from occurrences o
where o.caller is not null and not o.is_definition and not o.is_local
group by o.caller, o.symbol
"""

NODE_SQL = """
select symbol, name, kind, exported, is_member, is_method, is_class, relative_path
from symbols
"""

IMPL_SQL = """
select symbol, related_symbol from relationships where is_implementation
"""

PKG_SQL = "select index_package, index_version, index_manager, resolve_run_id from metadata"


def build_graphs(db_path: Path | str) -> tuple[nx.DiGraph, nx.DiGraph, dict[str, Any]]:
    """Return ``(full, internal, package_info)``."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        pkg = dict(
            zip(
                ["package", "version", "manager", "run_id"],
                con.execute(PKG_SQL).fetchone(),
                strict=True,
            )
        )
        nodes = con.execute(NODE_SQL).fetchall()
        edges = con.execute(EDGE_SQL).fetchall()
        impls = con.execute(IMPL_SQL).fetchall()
    finally:
        con.close()

    full = nx.DiGraph(name=f"{pkg['package']} {pkg['version']} call graph")
    for symbol, name, kind, exported, is_member, is_method, is_class, path in nodes:
        full.add_node(
            symbol,
            name=name,
            kind=kind,
            exported=bool(exported),
            is_member=bool(is_member),
            is_method=bool(is_method),
            is_class=bool(is_class),
            package=pkg["package"],
            manager=pkg["manager"] or "",
            internal=True,
            file=path,
        )
    for source, target, weight, tpkg, tmgr, tname, tmember, tmethod, internal_access in edges:
        if target not in full:
            full.add_node(
                target,
                name=tname,
                kind="external",
                exported=True,
                is_member=bool(tmember),
                is_method=bool(tmethod),
                is_class=False,
                package=tpkg or "",
                manager=tmgr or "",
                internal=False,
                file="",
            )
        if source not in full:  # a caller whose definition is a member/class; keep it
            full.add_node(
                source,
                name=source,
                kind="unknown",
                exported=False,
                internal=True,
                package=pkg["package"],
                manager=pkg["manager"] or "",
                is_member=False,
                is_method=False,
                is_class=False,
                file="",
            )
        full.add_edge(
            source, target, weight=int(weight), kind="call", internal_access=bool(internal_access)
        )

    internal = nx.DiGraph(name=f"{pkg['package']} internal call graph")
    for n, d in full.nodes(data=True):
        if d["internal"]:
            internal.add_node(n, **d)
    for u, v, d in full.edges(data=True):
        if u in internal and v in internal:
            internal.add_edge(u, v, **d)
    for method, generic in impls:
        if method in internal and generic in internal:
            internal.add_edge(method, generic, weight=1, kind="implements", internal_access=False)
    return full, internal, pkg


def _short(symbol: str) -> str:
    """``scip-r cran limma 3.69.2 lmFit().`` -> ``lmFit``."""
    desc = symbol.split(" ", 4)[-1]
    return desc.rstrip(".").rstrip(")").rstrip("(") if desc.endswith("().") else desc.rstrip(".#")


def pagerank(g: nx.DiGraph, damping: float = 0.85, iterations: int = 100) -> dict[str, float]:
    """Weighted PageRank by power iteration (networkx's needs scipy)."""
    nodes = list(g.nodes())
    n = len(nodes)
    if n == 0:
        return {}
    rank = dict.fromkeys(nodes, 1.0 / n)
    out_weight = {
        u: sum(d.get("weight", 1) for _, _, d in g.out_edges(u, data=True)) for u in nodes
    }
    for _ in range(iterations):
        dangling = sum(rank[u] for u in nodes if out_weight[u] == 0)
        new = dict.fromkeys(nodes, (1 - damping) / n + damping * dangling / n)
        for u, v, d in g.edges(data=True):
            new[v] += damping * rank[u] * d.get("weight", 1) / out_weight[u]
        delta = sum(abs(new[x] - rank[x]) for x in nodes)
        rank = new
        if delta < 1e-9:
            break
    return rank


def compute_metrics(full: nx.DiGraph, internal: nx.DiGraph, top: int = 15) -> dict[str, Any]:
    calls = nx.DiGraph(((u, v, d) for u, v, d in internal.edges(data=True) if d["kind"] == "call"))
    calls.add_nodes_from(internal.nodes(data=True))
    in_deg = sorted(calls.in_degree(weight="weight"), key=lambda kv: -kv[1])
    out_deg = sorted(calls.out_degree(), key=lambda kv: -kv[1])
    pr = pagerank(calls) if calls.number_of_edges() else {}
    exported = [n for n, d in internal.nodes(data=True) if d.get("exported")]
    reach: dict[str, int] = {n: len(nx.descendants(calls, n)) for n in exported}
    implemented = {u for u, v, d in internal.edges(data=True) if d["kind"] == "implements"}
    orphans = sorted(
        n
        for n, d in internal.nodes(data=True)
        if d.get("kind") in ("Function",)
        and not d.get("exported")
        and n not in implemented
        and calls.in_degree(n) == 0
    )
    sccs = [sorted(c) for c in nx.strongly_connected_components(calls) if len(c) > 1]
    self_loops = sorted(u for u, v in calls.edges() if u == v)
    dep_usage: dict[str, int] = {}
    for _u, v, d in full.edges(data=True):
        nv = full.nodes[v]
        if not nv["internal"] and nv["package"]:
            key = f"{nv['package']} ({nv['manager'] or '.'})"
            dep_usage[key] = dep_usage.get(key, 0) + d["weight"]
    return {
        "nodes_full": full.number_of_nodes(),
        "edges_full": full.number_of_edges(),
        "nodes_internal": internal.number_of_nodes(),
        "edges_internal_calls": calls.number_of_edges(),
        "edges_implements": sum(
            1 for _, _, d in internal.edges(data=True) if d["kind"] == "implements"
        ),
        "exported_functions": len(exported),
        "most_called": [(_short(n), int(w)) for n, w in in_deg[:top]],
        "most_calling": [(_short(n), int(w)) for n, w in out_deg[:top]],
        "pagerank": [
            (_short(n), round(v, 4)) for n, v in sorted(pr.items(), key=lambda kv: -kv[1])[:top]
        ],
        "widest_entry_points": [
            (_short(n), c) for n, c in sorted(reach.items(), key=lambda kv: -kv[1])[:top]
        ],
        "internal_without_callers": [_short(n) for n in orphans],
        "recursive": [_short(n) for n in self_loops],
        "mutual_recursion": [[_short(n) for n in c] for c in sccs],
        "dependency_usage": sorted(dep_usage.items(), key=lambda kv: -kv[1]),
        "internal_access_edges": [
            (_short(u), f"{full.nodes[v]['package']}:::{full.nodes[v]['name']}")
            for u, v, d in full.edges(data=True)
            if d["internal_access"]
        ],
    }


def metrics_markdown(m: dict[str, Any], pkg: dict[str, Any]) -> str:
    def table(title: str, rows: list, headers: tuple[str, str]) -> str:
        out = [f"### {title}", "", f"| {headers[0]} | {headers[1]} |", "| --- | --- |"]
        out += [f"| `{a}` | {b} |" for a, b in rows]
        return "\n".join(out) + "\n"

    parts = [
        f"# {pkg['package']} {pkg['version']} call graph\n",
        f"Resolve run `{pkg['run_id']}`. {m['nodes_internal']} package symbols, "
        f"{m['edges_internal_calls']} internal call edges, {m['edges_implements']} "
        f"method-to-generic edges, {m['edges_full'] - m['edges_internal_calls']} edges into "
        f"dependencies, "
        f"{m['exported_functions']} exported.\n",
        table(
            "Most-called internal functions (weighted in-degree)",
            m["most_called"],
            ("function", "calls"),
        ),
        table(
            "Functions with the most distinct callees", m["most_calling"], ("function", "callees")
        ),
        table("PageRank", m["pagerank"], ("function", "score")),
        table(
            "Exported entry points reaching the most functions",
            m["widest_entry_points"],
            ("function", "reachable"),
        ),
        table("Dependency usage (call sites)", m["dependency_usage"], ("package", "calls")),
    ]
    parts.append("### Internal functions with no static caller\n")
    parts.append(", ".join(f"`{n}`" for n in m["internal_without_callers"]) or "none")
    parts.append(
        "\n\nNot proof of dead code: calls through `do.call`, `match.fun` or dispatch "
        "are invisible to the static pass.\n"
    )
    parts.append("### Recursion\n")
    parts.append(
        "Self-recursive: " + (", ".join(f"`{n}`" for n in m["recursive"]) or "none") + "\n"
    )
    parts.append(
        "Mutually recursive groups: "
        + (
            "; ".join("{" + ", ".join(f"`{n}`" for n in c) + "}" for c in m["mutual_recursion"])
            or "none"
        )
        + "\n"
    )
    if m["internal_access_edges"]:
        parts.append("### `:::` access into other packages\n")
        parts.append("\n".join(f"- `{a}` -> `{b}`" for a, b in m["internal_access_edges"]) + "\n")
    return "\n".join(parts)


def write_dot(internal: nx.DiGraph, path: Path, top: int = 60) -> nx.DiGraph:
    """DOT of the ``top`` internal nodes by weighted degree and the edges
    among them; nodes left without an edge inside that set are dropped so
    the picture shows structure rather than a list."""
    calls = [(u, v, d) for u, v, d in internal.edges(data=True) if d["kind"] == "call"]
    deg: dict[str, int] = {}
    for u, v, d in calls:
        deg[u] = deg.get(u, 0) + d["weight"]
        deg[v] = deg.get(v, 0) + d["weight"]
    keep = {n for n, _ in sorted(deg.items(), key=lambda kv: -kv[1])[:top]}
    sub = internal.subgraph(keep).copy()
    sub.remove_nodes_from([n for n in list(sub.nodes()) if sub.degree(n) == 0])
    lines = [
        "digraph calls {",
        '  graph [rankdir=LR, overlap=false, splines=true, fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10];',
        '  edge [color="#888888", arrowsize=0.6];',
    ]
    for n, d in sub.nodes(data=True):
        fill = "#cfe8ff" if d.get("exported") else "#eeeeee"
        lines.append(f'  "{_short(n)}" [fillcolor="{fill}"];')
    for u, v, d in sub.edges(data=True):
        style = (
            ' [style=dashed, color="#c06000"]'
            if d["kind"] == "implements"
            else (f" [penwidth={min(1 + d['weight'] / 3, 4):.1f}]")
        )
        lines.append(f'  "{_short(u)}" -> "{_short(v)}"{style};')
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sub


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    db, out = Path(argv[1]), Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    full, internal, pkg = build_graphs(db)
    nx.write_graphml(full, out / "call_graph.graphml")
    nx.write_graphml(internal, out / "call_graph.internal.graphml")
    metrics = compute_metrics(full, internal)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    (out / "metrics.md").write_text(metrics_markdown(metrics, pkg), encoding="utf-8")
    sub = write_dot(internal, out / "call_graph.top.dot")
    dot = shutil.which("dot")
    if dot:
        subprocess.run(
            [dot, "-Tpng", "-o", str(out / "call_graph.top.png"), str(out / "call_graph.top.dot")],
            check=True,
        )
        subprocess.run(
            [dot, "-Tsvg", "-o", str(out / "call_graph.top.svg"), str(out / "call_graph.top.dot")],
            check=True,
        )
    print(
        f"{pkg['package']} {pkg['version']}: {internal.number_of_nodes()} internal nodes, "
        f"{metrics['edges_internal_calls']} internal call edges, "
        f"{full.number_of_edges()} edges total; "
        f"top subgraph has {sub.number_of_nodes()} nodes and {sub.number_of_edges()} edges; "
        f"PNG {'written' if dot else 'skipped (no dot)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
