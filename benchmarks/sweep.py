"""Tier-1 benchmark: the robustness sweep.

Indexes every package of a CRAN-style repository (Bioconductor software by
default) with the static pass only, no R, and reports what happened:
failures, parse errors, timing and memory distributions, throughput, and
packages whose output looks implausible. It is the cheapest way to find R
syntax the parser mishandles.

Usage::

    uv run python benchmarks/sweep.py fetch  --out sweeps/bioc-3.22 [--repo URL] [--limit N]
    uv run python benchmarks/sweep.py run    --out sweeps/bioc-3.22 [--jobs N]
    uv run python benchmarks/sweep.py report --out sweeps/bioc-3.22
    uv run python benchmarks/sweep.py all    --out sweeps/bioc-3.22 --jobs 8

``fetch`` downloads the repository's PACKAGES index and every tarball into
``<out>/tarballs/`` (skipping ones already present with the right size),
``run`` calls ``scip-r batch`` on them, ``report`` turns
``<out>/batch/summary.jsonl`` into ``<out>/report.md`` and ``report.json``.
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated, Any

import typer

from scipr.batch import BatchOptions, run_batch
from scipr.package import parse_dcf

BIOC_RELEASE = "https://bioconductor.org/packages/release/bioc/src/contrib"
CRAN = "https://cloud.r-project.org/src/contrib"

app = typer.Typer(
    name="sweep",
    help=__doc__,
    no_args_is_help=True,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)

OutOpt = Annotated[Path, typer.Option("--out", "-o", help="Sweep directory.")]


# ---- fetch ----------------------------------------------------------------------


def parse_packages_index(text: str) -> list[dict[str, str]]:
    """Records of a CRAN-style ``PACKAGES`` file (blank-line separated DCF)."""
    out: list[dict[str, str]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        rec = parse_dcf(block)
        if "Package" in rec and "Version" in rec:
            out.append(rec)
    return out


def tarball_name(rec: dict[str, str]) -> str:
    return f"{rec['Package']}_{rec['Version']}.tar.gz"


def _download(url: str, dest: Path, retries: int = 3) -> tuple[str, int, str | None]:
    """Fetch ``url`` to ``dest`` unless present with the same size.
    Returns ``(status, bytes, error)`` with status ``cached|ok|error``."""
    last: str | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "scip-r-sweep"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                length = resp.headers.get("Content-Length")
                if length and dest.is_file() and dest.stat().st_size == int(length):
                    return "cached", int(length), None
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as f:
                    shutil.copyfileobj(resp, f, length=1 << 20)
                tmp.replace(dest)
                return "ok", dest.stat().st_size, None
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    return "error", 0, last


@app.command()
def fetch(
    out: OutOpt,
    repo: Annotated[str, typer.Option(help="Repository contrib URL.")] = BIOC_RELEASE,
    limit: Annotated[int, typer.Option(help="Only the first N packages (0 = all).")] = 0,
    workers: Annotated[int, typer.Option(help="Parallel downloads.")] = 8,
    only: Annotated[
        str | None, typer.Option(help="Comma-separated package names to restrict to.")
    ] = None,
) -> None:
    """Download the PACKAGES index and every tarball into <out>/tarballs/."""
    out.mkdir(parents=True, exist_ok=True)
    tarballs = out / "tarballs"
    tarballs.mkdir(exist_ok=True)
    index_url = f"{repo.rstrip('/')}/PACKAGES"
    typer.echo(f"fetching {index_url}", err=True)
    with urllib.request.urlopen(index_url, timeout=60) as resp:
        text = resp.read().decode("utf-8", "replace")
    (out / "PACKAGES").write_text(text, encoding="utf-8")
    records = parse_packages_index(text)
    if only:
        wanted = {n.strip() for n in only.split(",")}
        records = [r for r in records if r["Package"] in wanted]
    if limit:
        records = records[:limit]
    typer.echo(f"{len(records)} packages listed", err=True)

    counts = {"ok": 0, "cached": 0, "error": 0}
    total_bytes = 0
    failures: list[dict[str, str]] = []
    manifest_lines: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                _download, f"{repo.rstrip('/')}/{tarball_name(r)}", tarballs / tarball_name(r)
            ): r
            for r in records
        }
        for i, fut in enumerate(as_completed(futs), 1):
            rec = futs[fut]
            status, nbytes, err = fut.result()
            counts[status] += 1
            total_bytes += nbytes
            if status == "error":
                failures.append(
                    {
                        "package": rec["Package"],
                        "url": f"{repo}/{tarball_name(rec)}",
                        "error": err or "",
                    }
                )
            else:
                manifest_lines.append(str((tarballs / tarball_name(rec)).resolve()))
            if i % 100 == 0 or i == len(records):
                typer.echo(
                    f"  {i}/{len(records)}: {counts['ok']} downloaded, {counts['cached']} cached, "
                    f"{counts['error']} failed, {total_bytes / 1e9:.2f} GB",
                    err=True,
                )
    manifest_lines.sort()
    (out / "manifest.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    (out / "fetch.json").write_text(
        json.dumps(
            {
                "repo": repo,
                "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "listed": len(records),
                **counts,
                "total_bytes": total_bytes,
                "failures": failures,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    typer.echo(f"{out / 'manifest.txt'}: {len(manifest_lines)} tarballs", err=True)
    if failures:
        raise typer.Exit(code=1)


# ---- run ----------------------------------------------------------------------------


@app.command()
def run(
    out: OutOpt,
    jobs: Annotated[int, typer.Option("--jobs", "-j")] = 1,
    export: Annotated[
        str | None, typer.Option(help="Also export each index (parquet|duckdb).")
    ] = None,
) -> None:
    """Run scip-r batch over <out>/manifest.txt into <out>/batch/."""
    manifest = out / "manifest.txt"
    if not manifest.is_file():
        typer.echo(f"no manifest at {manifest}; run `fetch` first", err=True)
        raise typer.Exit(code=2)
    sources = [Path(line) for line in manifest.read_text().splitlines() if line.strip()]
    batch_dir = out / "batch"
    opts = BatchOptions(export=export, hive=export == "parquet")
    done = 0

    def progress(r: Any) -> None:
        nonlocal done
        done += 1
        if r.status != "ok":
            first = (r.error or "").splitlines()[0]
            typer.echo(f"  error {Path(r.source).name}: {first}", err=True)
        if done % 100 == 0 or done == len(sources):
            typer.echo(f"  {done}/{len(sources)}", err=True)

    typer.echo(f"indexing {len(sources)} packages with {jobs} workers", err=True)
    results = run_batch(sources, batch_dir, opts, jobs=jobs, on_result=progress)
    failed = sum(1 for r in results if r.status != "ok")
    typer.echo(
        f"{batch_dir / 'summary.jsonl'}: {len(results) - failed} ok, {failed} failed", err=True
    )


# ---- report -------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    k = (len(vs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(vs) - 1)
    return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 4)


def _dist(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "p50": _pct(values, 0.5),
        "p90": _pct(values, 0.9),
        "p99": _pct(values, 0.99),
        "max": round(max(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "sum": round(sum(values), 3),
    }


def analyse(results: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    ok = [r for r in results if r["status"] == "ok"]
    failed = [r for r in results if r["status"] != "ok"]
    totals = [r["timings"].get("total", 0.0) for r in ok]
    index_t = [r["timings"].get("index", 0.0) for r in ok]
    lines = sum(r["n_lines"] for r in ok)
    files = sum(r["n_files"] for r in ok)
    occ = sum((r["summary"] or {}).get("occurrences", 0) for r in ok)
    syms = sum((r["summary"] or {}).get("symbols", 0) for r in ok)
    guessed = sum((r["summary"] or {}).get("guessed_external_symbols", 0) for r in ok)
    external = sum((r["summary"] or {}).get("external_symbols", 0) for r in ok)
    with_errors = [r for r in ok if r["parse_errors"]]
    err_lines = sum(r["parse_errors"] for r in ok)

    def per_kloc(r: dict[str, Any]) -> float:
        return r["timings"].get("index", 0.0) / max(r["n_lines"], 1) * 1000

    slowest = sorted(ok, key=lambda r: -r["timings"].get("total", 0))[:15]
    slow_per_line = sorted((r for r in ok if r["n_lines"] >= 2000), key=lambda r: -per_kloc(r))[
        :10
    ]
    most_errors = sorted(with_errors, key=lambda r: -r["parse_errors"])[:20]
    implausible = [
        r
        for r in ok
        if r["n_files"] > 0
        and (
            (r["summary"] or {}).get("symbols", 0) == 0
            or (r["summary"] or {}).get("occurrences", 0) == 0
        )
    ]
    guess_heavy = sorted(
        (r for r in ok if (r["summary"] or {}).get("external_symbols", 0) >= 20),
        key=lambda r: (
            -(
                (r["summary"] or {}).get("guessed_external_symbols", 0)
                / max((r["summary"] or {}).get("external_symbols", 1), 1)
            )
        ),
    )[:10]
    failure_kinds: dict[str, int] = {}
    for r in failed:
        kind = (r["error"] or "?").split(":", 1)[0]
        failure_kinds[kind] = failure_kinds.get(kind, 0) + 1
    elapsed = meta.get("elapsed_seconds") or sum(totals)
    return {
        "meta": meta,
        "packages": {"total": len(results), "ok": len(ok), "failed": len(failed)},
        "size": {
            "files": files,
            "lines": lines,
            "source_mb": round(sum(r["source_bytes"] for r in ok) / 1e6, 1),
        },
        "output": {
            "symbols": syms,
            "occurrences": occ,
            "external_symbols": external,
            "guessed_external_symbols": guessed,
            "guessed_fraction": round(guessed / external, 4) if external else None,
        },
        "throughput": {
            "wall_seconds": elapsed,
            "cpu_seconds_index": round(sum(index_t), 2),
            "lines_per_cpu_second": round(lines / sum(index_t)) if sum(index_t) else None,
            "packages_per_minute_wall": round(len(ok) / elapsed * 60, 1) if elapsed else None,
        },
        "timing_total_s": _dist(totals),
        "timing_index_s": _dist(index_t),
        "timing_index_ms_per_kloc": _dist([per_kloc(r) * 1000 for r in ok if r["n_lines"]]),
        "peak_rss_mib": _dist([r["peak_rss_mib"] for r in ok if r.get("peak_rss_mib")]),
        "parse_errors": {
            "packages_with_errors": len(with_errors),
            "packages_with_errors_pct": round(100 * len(with_errors) / len(ok), 2) if ok else None,
            "documents_with_errors": sum(r["parse_error_documents"] for r in ok),
            "error_nodes": err_lines,
        },
        "failures": {
            "by_kind": failure_kinds,
            "packages": [
                {
                    "package": r["package"] or Path(r["source"]).name,
                    "error": (r["error"] or "").splitlines()[0],
                }
                for r in failed
            ],
        },
        "slowest": [
            {"package": r["package"], "lines": r["n_lines"], "total_s": r["timings"].get("total")}
            for r in slowest
        ],
        "slowest_per_kloc": [
            {
                "package": r["package"],
                "lines": r["n_lines"],
                "ms_per_kloc": round(per_kloc(r) * 1000, 1),
            }
            for r in slow_per_line
        ],
        "most_parse_errors": [
            {
                "package": r["package"],
                "error_nodes": r["parse_errors"],
                "documents": r["parse_error_documents"],
                "diagnostics": r["diagnostics"],
            }
            for r in most_errors
        ],
        "implausible_output": [
            {
                "package": r["package"],
                "files": r["n_files"],
                "symbols": (r["summary"] or {}).get("symbols"),
                "occurrences": (r["summary"] or {}).get("occurrences"),
            }
            for r in implausible
        ],
        "guess_heavy": [
            {
                "package": r["package"],
                "guessed": (r["summary"] or {}).get("guessed_external_symbols"),
                "external": (r["summary"] or {}).get("external_symbols"),
            }
            for r in guess_heavy
        ],
    }


def _table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_none_\n"
    head = "| " + " | ".join(columns) + " |\n|" + "|".join(" --- " for _ in columns) + "|\n"
    body = "".join("| " + " | ".join(str(r.get(c, "")) for c in columns) + " |\n" for r in rows)
    return head + body


def _row(label: str, d: dict[str, float]) -> str:
    cells = " | ".join(str(d.get(k, "")) for k in ("min", "p50", "p90", "p99", "max"))
    return f"| {label} | {cells} |"


def render_report(a: dict[str, Any]) -> str:
    m, p, s, o, t = a["meta"], a["packages"], a["size"], a["output"], a["throughput"]
    e = a["parse_errors"]
    guessed_pct = (o["guessed_fraction"] or 0) * 100
    lines = [
        "# scip-r robustness sweep",
        "",
        f"- scip-r {m.get('scip_r_version')} on Python {m.get('python')}, {m.get('platform')} "
        f"({m.get('cpu_count')} CPUs, {m.get('jobs')} workers), started {m.get('started_utc')}",
        f"- {p['total']} packages: **{p['ok']} indexed, {p['failed']} failed**",
        f"- {s['files']:,} R files, {s['lines']:,} lines, {s['source_mb']} MB of source",
        f"- {o['symbols']:,} symbols, {o['occurrences']:,} occurrences, "
        f"{o['external_symbols']:,} external symbols of which "
        f"{o['guessed_external_symbols']:,} guessed ({guessed_pct:.1f}%)",
        "",
        "## Throughput",
        "",
        f"- wall time {t['wall_seconds']:.0f} s; {t['packages_per_minute_wall']} packages/min",
        f"- static pass CPU time {t['cpu_seconds_index']:.0f} s; "
        f"{t['lines_per_cpu_second']:,} lines/s",
        "",
        "| per package | min | p50 | p90 | p99 | max |",
        "| --- | --- | --- | --- | --- | --- |",
        _row("total seconds", a["timing_total_s"]),
        _row("index ms per kLOC", a["timing_index_ms_per_kloc"]),
    ]
    if a["peak_rss_mib"]:
        lines.append(_row("worker peak RSS MiB", a["peak_rss_mib"]))
    lines += [
        "",
        "## Parse health",
        "",
        f"- {e['packages_with_errors']} packages ({e['packages_with_errors_pct']}%) have "
        f"tree-sitter parse errors: {e['error_nodes']} error nodes in "
        f"{e['documents_with_errors']} files",
        "",
        "### Packages with the most parse errors",
        "",
        _table(a["most_parse_errors"], ["package", "error_nodes", "documents"]),
        "## Failures",
        "",
        _table(
            [{"kind": k, "count": v} for k, v in sorted(a["failures"]["by_kind"].items())],
            ["kind", "count"],
        ),
        _table(a["failures"]["packages"], ["package", "error"]),
        "## Slowest packages",
        "",
        _table(a["slowest"], ["package", "lines", "total_s"]),
        "### Slowest per line (packages over 2,000 lines)",
        "",
        _table(a["slowest_per_kloc"], ["package", "lines", "ms_per_kloc"]),
        "## Implausible output (files but no symbols or occurrences)",
        "",
        _table(a["implausible_output"], ["package", "files", "symbols", "occurrences"]),
        "## Most guess-heavy (20+ external symbols)",
        "",
        _table(a["guess_heavy"], ["package", "guessed", "external"]),
    ]
    return "\n".join(lines) + "\n"


@app.command()
def report(out: OutOpt) -> None:
    """Summarise <out>/batch/summary.jsonl into report.md and report.json."""
    summary = out / "batch" / "summary.jsonl"
    if not summary.is_file():
        typer.echo(f"no {summary}; run `run` first", err=True)
        raise typer.Exit(code=2)
    results = [json.loads(line) for line in summary.read_text().splitlines() if line.strip()]
    meta_path = out / "batch" / "batch-meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    a = analyse(results, meta)
    (out / "report.json").write_text(json.dumps(a, indent=2) + "\n", encoding="utf-8")
    (out / "report.md").write_text(render_report(a), encoding="utf-8")
    typer.echo(render_report(a).split("\n## ")[0])
    typer.echo(f"wrote {out / 'report.md'}", err=True)


@app.command(name="all")
def all_(
    out: OutOpt,
    repo: Annotated[str, typer.Option()] = BIOC_RELEASE,
    limit: Annotated[int, typer.Option()] = 0,
    jobs: Annotated[int, typer.Option("--jobs", "-j")] = 1,
    workers: Annotated[int, typer.Option()] = 8,
) -> None:
    """fetch, run and report in one go."""
    try:
        fetch(out=out, repo=repo, limit=limit, workers=workers, only=None)
    except typer.Exit as e:
        if e.exit_code not in (0, 1):
            raise
    run(out=out, jobs=jobs, export=None)
    report(out=out)


if __name__ == "__main__":
    sys.exit(app())
