"""Index (and optionally resolve and export) many packages in one run."""

from __future__ import annotations

import json
import tempfile
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .inspect import summarize, write_index
from .package import is_tarball, read_description, sha256_file, unpack_tarball
from .parser import build_index, index_arguments


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    try:
        with tempfile.TemporaryDirectory(prefix="scipr-batch-") as tmp:
            extra: dict[str, str] = {}
            if is_tarball(source):
                pkg_dir = unpack_tarball(source, tmp)
                extra = {"source_tarball": source.name, "source_sha256": sha256_file(source)}
            else:
                pkg_dir = source
            info = read_description(pkg_dir, manager=opts.manager)
            result.package, result.version = info.name, info.version
            out_dir = out_root / info.name
            out_dir.mkdir(parents=True, exist_ok=True)
            result.out_dir = str(out_dir)

            index = build_index(pkg_dir, manager=opts.manager, extra_arguments=extra)
            index_path = write_index(index, out_dir / "index.scip")
            result.index = str(index_path)
            final = index

            if opts.resolve:
                from .resolve import metadata_record, resolve_index

                r = resolve_index(index, pkg_dir, rscript=opts.rscript, timeout=opts.timeout)
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
            elif opts.export == "duckdb":
                from .export import write_duckdb

                write_duckdb(final, out_dir / "index.duckdb", overwrite=True)

            result.summary = summarize(final).to_dict()
            result.summary.pop("per_document", None)
            result.status = "ok"
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    return result


def run_batch(
    sources: list[Path],
    out_root: Path,
    opts: BatchOptions,
    *,
    jobs: int = 1,
    on_result: Any = None,
) -> list[BatchResult]:
    """Index every source; write ``summary.jsonl`` in ``out_root``."""
    out_root.mkdir(parents=True, exist_ok=True)
    results: list[BatchResult] = []
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
    return results
