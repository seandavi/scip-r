"""Index (and optionally resolve and export) many packages in one run."""

from __future__ import annotations

import json
import os
import platform
import resource
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .inspect import summarize, write_index
from .package import find_r_files, is_tarball, read_description, sha256_file, unpack_tarball
from .parser import ParseDiagnostic, build_index, index_arguments


@dataclass
class BatchOptions:
    resolve: bool = False
    export: str | None = None  # "parquet" | "duckdb" | None
    hive: bool = False
    manager: str | None = None
    rscript: str | None = None
    timeout: float = 600.0


@dataclass
class BatchResult:
    source: str
    package: str | None
    version: str | None
    status: str  # ok | error
    out_dir: str | None = None
    index: str | None = None
    resolved_index: str | None = None
    metadata: str | None = None
    summary: dict[str, Any] | None = None
    resolve_run_id: str | None = None
    error: str | None = None
    # size of the input
    n_files: int = 0
    n_lines: int = 0
    source_bytes: int = 0
    # parse health
    parse_errors: int = 0
    parse_error_documents: int = 0
    diagnostics: str | None = None  # path of diagnostics.json when there were errors
    # timings in seconds per phase; ``total`` covers everything for this package
    timings: dict[str, float] = field(default_factory=dict)
    # peak resident set size of the worker process so far, in MiB (monotone
    # within a worker, so only the first package a worker handles is exact)
    peak_rss_mib: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _peak_rss_mib() -> float | None:
    try:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):  # pragma: no cover
        return None
    # ru_maxrss is bytes on macOS and kilobytes on Linux.
    return round(rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024, 1)


def run_metadata(opts: BatchOptions, jobs: int, n_sources: int) -> dict[str, Any]:
    """Facts about the machine and configuration that produced a batch,
    written to ``batch-meta.json`` so a summary can be compared later."""
    return {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scip_r_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "jobs": jobs,
        "n_sources": n_sources,
        "options": asdict(opts),
    }


def discover_sources(inputs: list[Path], manifest: Path | None) -> list[Path]:
    """Expand CLI inputs: package dirs and tarballs as given; a directory
    without DESCRIPTION is treated as a collection of packages; a manifest
    lists one path per line (``#`` comments allowed)."""
    out: list[Path] = []
    for p in inputs:
        if is_tarball(p) or (p / "DESCRIPTION").is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(
                sorted(
                    c
                    for c in p.iterdir()
                    if is_tarball(c) or (c.is_dir() and (c / "DESCRIPTION").is_file())
                )
            )
    if manifest is not None:
        base = manifest.parent
        for line in manifest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = Path(line)
            out.append(p if p.is_absolute() else base / p)
    return out


def index_one(source: Path, out_root: Path, opts: BatchOptions) -> BatchResult:
    """Index a single source into ``out_root/<package>/``. Never raises."""
    result = BatchResult(source=str(source), package=None, version=None, status="error")
    t0 = time.perf_counter()
    timings = result.timings

    def lap(name: str, since: float) -> float:
        now = time.perf_counter()
        timings[name] = round(now - since, 4)
        return now

    try:
        with tempfile.TemporaryDirectory(prefix="scipr-batch-") as tmp:
            extra: dict[str, str] = {}
            t = t0
            if is_tarball(source):
                pkg_dir = unpack_tarball(source, tmp)
                extra = {"source_tarball": source.name, "source_sha256": sha256_file(source)}
                t = lap("unpack", t)
            else:
                pkg_dir = source
            info = read_description(pkg_dir, manager=opts.manager)
            result.package, result.version = info.name, info.version
            out_dir = out_root / info.name
            out_dir.mkdir(parents=True, exist_ok=True)
            result.out_dir = str(out_dir)
            for f in find_r_files(pkg_dir, info.collate):
                data = f.read_bytes()
                result.n_files += 1
                result.source_bytes += len(data)
                result.n_lines += data.count(b"\n") + (
                    1 if data and not data.endswith(b"\n") else 0
                )

            diagnostics: list[ParseDiagnostic] = []
            index = build_index(
                pkg_dir, manager=opts.manager, extra_arguments=extra, diagnostics_out=diagnostics
            )
            t = lap("index", t)
            stamps = index_arguments(index)
            result.parse_errors = int(stamps.get("parse_errors", "0"))
            result.parse_error_documents = int(stamps.get("parse_error_documents", "0"))
            if diagnostics:
                diag_path = out_dir / "diagnostics.json"
                diag_path.write_text(json.dumps(diagnostics, indent=1) + "\n", encoding="utf-8")
                result.diagnostics = str(diag_path)
            index_path = write_index(index, out_dir / "index.scip")
            result.index = str(index_path)
            t = lap("write", t)
            final = index

            if opts.resolve:
                from .resolve import metadata_record, resolve_index

                r = resolve_index(index, pkg_dir, rscript=opts.rscript, timeout=opts.timeout)
                t = lap("resolve", t)
                payload = r.index.SerializeToString()
                resolved_path = out_dir / "index.resolved.scip"
                resolved_path.write_bytes(payload)
                record = metadata_record(
                    r.resolution,
                    r.stats,
                    index_path=resolved_path,
                    index_bytes=payload,
                    source=index_arguments(r.index),
                )
                meta_path = out_dir / "index.resolved.scip.meta.json"
                meta_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
                result.resolved_index = str(resolved_path)
                result.metadata = str(meta_path)
                result.resolve_run_id = r.resolution["run_id"]
                final = r.index

            if opts.export == "parquet":
                from .export import write_parquet

                target = out_root / "parquet" if opts.hive else out_dir / "parquet"
                write_parquet(final, target, hive=opts.hive)
                t = lap("export", t)
            elif opts.export == "duckdb":
                from .export import write_duckdb

                write_duckdb(final, out_dir / "index.duckdb", overwrite=True)
                t = lap("export", t)

            result.summary = summarize(final).to_dict()
            result.summary.pop("per_document", None)
            result.status = "ok"
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    timings["total"] = round(time.perf_counter() - t0, 4)
    result.peak_rss_mib = _peak_rss_mib()
    return result


def run_batch(
    sources: list[Path],
    out_root: Path,
    opts: BatchOptions,
    *,
    jobs: int = 1,
    on_result: Any = None,
) -> list[BatchResult]:
    """Index every source; write ``summary.jsonl`` (one line per package)
    and ``batch-meta.json`` (machine, versions, options, elapsed) in
    ``out_root``."""
    out_root.mkdir(parents=True, exist_ok=True)
    results: list[BatchResult] = []
    meta = run_metadata(opts, jobs, len(sources))
    meta_path = out_root / "batch-meta.json"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    started = time.perf_counter()
    summary_path = out_root / "summary.jsonl"
    with summary_path.open("w", encoding="utf-8") as summary:
        if jobs <= 1:
            for src in sources:
                r = index_one(src, out_root, opts)
                results.append(r)
                summary.write(json.dumps(r.to_dict()) + "\n")
                summary.flush()
                if on_result:
                    on_result(r)
        else:
            with ProcessPoolExecutor(max_workers=jobs) as pool:
                futures = {pool.submit(index_one, src, out_root, opts): src for src in sources}
                for fut in as_completed(futures):
                    r = fut.result()
                    results.append(r)
                    summary.write(json.dumps(r.to_dict()) + "\n")
                    summary.flush()
                    if on_result:
                        on_result(r)
    results.sort(key=lambda r: r.source)
    meta["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    meta["ok"] = sum(1 for r in results if r.status == "ok")
    meta["failed"] = len(results) - meta["ok"]
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return results
