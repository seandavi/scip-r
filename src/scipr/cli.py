"""``scip-r`` command line interface.

Subcommands::

    scip-r index PKG_DIR [-o index.scip] [--stats] [--emit-positions FILE]
    scip-r stats INDEX [--json]
    scip-r print INDEX [--json] [--no-locals]
    scip-r export INDEX --format parquet|duckdb [-o PATH] [--overwrite]
    scip-r resolve INDEX --pkg DIR [-o OUT] [--meta FILE] [--rscript PATH]
"""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .inspect import load_index, render_text, summarize, write_index
from .parser import GuessedPosition, build_index

app = typer.Typer(
    name="scip-r",
    help="Static SCIP code-intelligence indexer for R packages. No R runtime required.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"scip-r {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            help="Show the scip-r version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    """Static SCIP code-intelligence indexer for R packages."""


def _err(msg: str) -> None:
    typer.echo(f"scip-r: error: {msg}", err=True)


class ExportFormat(str, Enum):
    parquet = "parquet"
    duckdb = "duckdb"


IndexArg = Annotated[
    Path,
    typer.Argument(
        exists=True,
        dir_okay=False,
        readable=True,
        show_default=False,
        help="A SCIP index file, as written by `scip-r index`.",
    ),
]


@app.command()
def index(
    pkg_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            readable=True,
            show_default=False,
            help="R package root (has DESCRIPTION and R/). Only files under R/ are indexed.",
        ),
    ],
    output: Annotated[
        Path,
        typer.Option("-o", "--output", help="Where to write the SCIP index."),
    ] = Path("index.scip"),
    stats: Annotated[
        bool, typer.Option("--stats", help="Print a one-line summary to stderr afterwards.")
    ] = False,
    emit_positions: Annotated[
        Path | None,
        typer.Option(
            "--emit-positions",
            metavar="FILE",
            show_default=False,
            help=(
                "Also write guessed (unresolved) call-site positions as JSON, for a "
                "second-pass consumer; `scip-r resolve` needs only the index."
            ),
        ),
    ] = None,
) -> None:
    """Index an R package's source tree into a SCIP index file."""
    positions: list[GuessedPosition] | None = [] if emit_positions else None
    try:
        idx = build_index(pkg_dir, positions_out=positions)
    except NotADirectoryError as e:
        _err(str(e))
        raise typer.Exit(code=2) from e

    write_index(idx, output)

    if emit_positions is not None and positions is not None:
        emit_positions.parent.mkdir(parents=True, exist_ok=True)
        emit_positions.write_text(json.dumps(positions, indent=2) + "\n", encoding="utf-8")
        if stats:
            typer.echo(f"{emit_positions}: {len(positions)} guessed positions", err=True)

    if stats:
        typer.echo(f"{output}: {summarize(idx).one_line()}", err=True)


@app.command()
def stats(
    index_file: IndexArg,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the full summary as JSON on stdout.")
    ] = False,
) -> None:
    """Summarise an existing SCIP index: document, symbol and occurrence counts."""
    summary = summarize(load_index(index_file))
    if as_json:
        typer.echo(json.dumps(summary.to_dict(), indent=2))
        return
    typer.echo(f"{index_file}: {summary.tool_name} {summary.tool_version}")
    typer.echo(f"  project root: {summary.project_root}")
    typer.echo(f"  {summary.one_line()}")
    typer.echo(f"  {summary.references} references, {summary.local_occurrences} local occurrences")
    if summary.external_packages:
        pkgs = ", ".join(f"{p} ({n})" for p, n in summary.external_packages.items())
        typer.echo(f"  external packages: {pkgs}")
    for d in summary.per_document:
        typer.echo(
            f"  {d.relative_path}: {d.symbols} symbols, "
            f"{d.occurrences} occurrences ({d.definitions} definitions)"
        )


@app.command(name="print")
def print_(
    index_file: IndexArg,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Emit the index as protobuf JSON instead of text."),
    ] = False,
    locals_: Annotated[
        bool,
        typer.Option("--locals/--no-locals", help="Include `local N` occurrences in text output."),
    ] = True,
) -> None:
    """Dump an index in a readable form (like `scip print`)."""
    idx = load_index(index_file)
    if as_json:
        from google.protobuf.json_format import MessageToJson

        typer.echo(MessageToJson(idx, preserving_proto_field_name=True))
        return
    sys.stdout.write(render_text(idx, show_locals=locals_))


@app.command()
def export(
    index_file: IndexArg,
    fmt: Annotated[
        ExportFormat,
        typer.Option("-f", "--format", show_default=False, help="Target format."),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "-o",
            "--output",
            show_default=False,
            help=(
                "Output path: a directory for parquet (default: <index stem>-parquet/), "
                "a file for duckdb (default: <index stem>.duckdb)."
            ),
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace existing DuckDB tables with the same names."),
    ] = False,
) -> None:
    """Convert an index into Parquet files or a DuckDB database.

    Requires the optional export extra: pip install 'scip-r[export]'.
    """
    from .export import MissingExtraError, write_duckdb, write_parquet

    idx = load_index(index_file)
    stem = index_file.with_suffix("").name
    try:
        if fmt is ExportFormat.parquet:
            out_dir = output or index_file.parent / f"{stem}-parquet"
            written = write_parquet(idx, out_dir)
            for name, path in written.items():
                typer.echo(f"{name}: {path}", err=True)
        else:
            db_path = output or index_file.parent / f"{stem}.duckdb"
            counts = write_duckdb(idx, db_path, overwrite=overwrite)
            for name, n in counts.items():
                typer.echo(f"{name}: {n} rows -> {db_path}", err=True)
    except MissingExtraError as e:
        _err(str(e))
        raise typer.Exit(code=3) from e


@app.command()
def resolve(
    index_file: IndexArg,
    pkg_dir: Annotated[
        Path,
        typer.Option(
            "--pkg",
            exists=True,
            file_okay=False,
            readable=True,
            show_default=False,
            help="The package source directory the index was built from.",
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "-o",
            "--output",
            show_default=False,
            help="Resolved index path (default: <index stem>.resolved.scip).",
        ),
    ] = None,
    meta: Annotated[
        Path | None,
        typer.Option(
            "--meta",
            show_default=False,
            help="Sidecar metadata record path (default: <output>.meta.json).",
        ),
    ] = None,
    keep_json: Annotated[
        Path | None,
        typer.Option(
            "--keep-json",
            show_default=False,
            help="Also save the raw JSON produced by resolve.R here.",
        ),
    ] = None,
    rscript: Annotated[
        Path | None,
        typer.Option("--rscript", show_default=False, help="Rscript executable (default: PATH)."),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to allow the R session.")
    ] = 600.0,
    stats: Annotated[
        bool, typer.Option("--stats", help="Print a one-line summary to stderr afterwards.")
    ] = False,
) -> None:
    """Resolve guessed call targets with a real R session (needs R + pkgload).

    Loads the package from source with pkgload, asks R where every free name
    resolves, rewrites guessed references, links S3/S4 methods to their
    generics, and writes a metadata record describing the R environment.
    """
    import subprocess

    from .resolve import (
        ResolverError,
        RscriptNotFoundError,
        metadata_record,
        resolve_index,
    )

    idx = load_index(index_file)
    out = output or index_file.with_name(f"{index_file.with_suffix('').name}.resolved.scip")
    meta_path = meta or out.with_name(out.name + ".meta.json")
    try:
        result = resolve_index(idx, pkg_dir, rscript=rscript, timeout=timeout)
    except RscriptNotFoundError as e:
        _err(str(e))
        raise typer.Exit(code=4) from e
    except ResolverError as e:
        _err(str(e))
        raise typer.Exit(code=4) from e
    except subprocess.TimeoutExpired as e:
        _err(f"resolve.R did not finish within {timeout:g}s")
        raise typer.Exit(code=4) from e

    payload = result.index.SerializeToString()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)
    record = metadata_record(result.resolution, result.stats, index_path=out, index_bytes=payload)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if keep_json is not None:
        keep_json.parent.mkdir(parents=True, exist_ok=True)
        keep_json.write_text(json.dumps(result.resolution, indent=2) + "\n", encoding="utf-8")

    if stats:
        s = result.stats
        typer.echo(
            f"{out}: {s.resolved} of {s.guessed_before} guessed references resolved "
            f"({s.resolved_to_self} to this package, {s.still_guessed} still guessed), "
            f"{s.versions_filled} versions filled, "
            f"{s.s3_relationships} S3 and {s.s4_relationships} S4 method links; "
            f"metadata in {meta_path} (run {result.resolution['run_id']})",
            err=True,
        )
        for u in s.unmatched_methods:
            typer.echo(f"  note: no static symbol matched {u}", err=True)


def main() -> None:
    """Console-script entry point (kept for backwards compatibility)."""
    app()


if __name__ == "__main__":
    main()
