"""Convert a SCIP index into flat tables, and write them as Parquet or DuckDB.

The row-building half (:func:`index_to_rows`) is pure Python and always
available. The writers need the optional ``export`` extra::

    pip install "scip-r[export]"

Every table carries four **index columns** so files from many packages can
be unioned or hive-partitioned: ``index_package``, ``index_version``,
``index_manager`` (from the ``package=``/``version=``/``manager=`` stamps
scip-r writes into ``tool_info.arguments``) and ``resolve_run_id`` (null
for a purely static index).

Tables (all string columns unless noted):

``metadata``
    one row: ``tool_name``, ``tool_version``, ``tool_arguments``
    (list<string>: every ``key=value`` provenance stamp), ``project_root``,
    ``protocol_version``, ``text_document_encoding``
``documents``
    ``relative_path``, ``language``, ``n_symbols`` (int), ``n_occurrences``
    (int)
``symbols``
    per-document ``SymbolInformation``: ``relative_path``, ``symbol``,
    ``kind``, ``display_name``, ``documentation`` (list<string>),
    ``enclosing_symbol``, ``exported`` (bool), plus the parsed symbol columns
``external_symbols``
    same shape as ``symbols`` with ``relative_path`` null and no ``exported``,
    plus ``guessed`` (bool)
``occurrences``
    ``relative_path``, ``symbol``, ``start_line``, ``start_char``,
    ``end_line``, ``end_char`` (ints, 0-based; characters are UTF-8 bytes),
    ``symbol_roles`` (int), ``is_definition`` (bool), ``syntax_kind``,
    ``caller`` (symbol of the innermost enclosing top-level definition, from
    ``enclosing_range``), ``internal_access`` (bool: reached via ``:::``),
    plus the parsed symbol columns
``relationships``
    ``relative_path``, ``symbol``, ``related_symbol``, ``is_reference``,
    ``is_implementation``, ``is_type_definition``, ``is_definition``

Parsed symbol columns: ``is_local`` (bool), ``scheme``, ``manager``,
``package``, ``version``, ``descriptor``, ``name``, ``owner`` (R6/RC class
of a member), ``disambiguator`` (S4 signature), ``is_function``,
``is_method``, ``is_member``, ``is_class`` (bools).

See ``docs/parquet-schema.md`` for the full column reference.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import scip_pb2 as scip
from .parser import EXPORTED_DOC, index_arguments
from .symbols import parse_symbol

TABLES: tuple[str, ...] = (
    "metadata",
    "documents",
    "symbols",
    "external_symbols",
    "occurrences",
    "relationships",
)
INDEX_COLUMNS: tuple[str, ...] = (
    "index_package",
    "index_version",
    "index_manager",
    "resolve_run_id",
)
HIVE_PARTITION_COLUMNS: tuple[str, ...] = ("index_package", "index_version")

Row = dict[str, Any]


class MissingExtraError(ImportError):
    """Raised when a writer needs a dependency from the ``export`` extra."""

    def __init__(self, module: str) -> None:
        super().__init__(
            f"{module} is not installed; install the export extra with "
            "`pip install 'scip-r[export]'` (or `uv sync --extra export`)."
        )


def index_columns(index: scip.Index) -> Row:
    """The four columns stamped onto every row."""
    stamps = index_arguments(index)
    return {
        "index_package": stamps.get("package"),
        "index_version": stamps.get("version"),
        "index_manager": stamps.get("manager"),
        "resolve_run_id": stamps.get("resolve_run_id"),
    }


def _symbol_columns(symbol: str) -> Row:
    p = parse_symbol(symbol)
    return {
        "is_local": p.is_local,
        "scheme": p.scheme,
        "manager": p.manager,
        "package": p.package,
        "version": p.version,
        "descriptor": p.descriptor,
        "name": p.name,
        "owner": p.owner,
        "disambiguator": p.disambiguator,
        "is_function": p.is_function,
        "is_method": p.is_method,
        "is_member": p.is_member,
        "is_class": p.is_class,
    }


def _symbol_info_row(info: scip.SymbolInformation, relative_path: str | None) -> Row:
    return {
        "relative_path": relative_path,
        "symbol": info.symbol,
        "kind": scip.SymbolInformation.Kind.Name(info.kind),
        "display_name": info.display_name or None,
        "documentation": list(info.documentation),
        "enclosing_symbol": info.enclosing_symbol or None,
        **_symbol_columns(info.symbol),
    }


def _relationship_rows(info: scip.SymbolInformation, relative_path: str | None) -> list[Row]:
    return [
        {
            "relative_path": relative_path,
            "symbol": info.symbol,
            "related_symbol": rel.symbol,
            "is_reference": rel.is_reference,
            "is_implementation": rel.is_implementation,
            "is_type_definition": rel.is_type_definition,
            "is_definition": rel.is_definition,
        }
        for rel in info.relationships
    ]


Span = tuple[tuple[int, int], tuple[int, int]]


def _span(rng: Any) -> Span:
    r = list(rng)
    if len(r) == 3:
        return (r[0], r[1]), (r[0], r[2])
    return (r[0], r[1]), (r[2], r[3])


def _contains(outer: Span, inner: Span) -> bool:
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _callers(doc: scip.Document) -> list[str | None]:
    """For each occurrence, the symbol of the innermost top-level definition
    whose ``enclosing_range`` contains it (None at top level, and None for a
    definition occurrence's own definer)."""
    definers: list[tuple[Span, Span, str]] = []
    for occ in doc.occurrences:
        if occ.enclosing_range:
            definers.append((_span(occ.enclosing_range), _span(occ.range), occ.symbol))
    out: list[str | None] = []
    for occ in doc.occurrences:
        span = _span(occ.range)
        best: tuple[Span, str] | None = None
        for enclosing, name_span, symbol in definers:
            if not _contains(enclosing, span):
                continue
            if name_span == span and symbol == occ.symbol:
                continue  # the definition itself
            if best is None or _contains(best[0], enclosing):
                best = (enclosing, symbol)
        out.append(best[1] if best else None)
    return out


def _occurrence_row(occ: scip.Occurrence, relative_path: str, caller: str | None) -> Row:
    (sl, sc), (el, ec) = _span(occ.range)
    return {
        "relative_path": relative_path,
        "symbol": occ.symbol,
        "start_line": sl,
        "start_char": sc,
        "end_line": el,
        "end_char": ec,
        "symbol_roles": occ.symbol_roles,
        "is_definition": bool(occ.symbol_roles & scip.SymbolRole.Definition),
        "syntax_kind": scip.SyntaxKind.Name(occ.syntax_kind),
        "caller": caller,
        "internal_access": any(":::" in d for d in occ.override_documentation),
        **_symbol_columns(occ.symbol),
    }


def index_to_rows(index: scip.Index) -> dict[str, list[Row]]:
    """Flatten an index into ``{table_name: [row, ...]}``. Pure Python."""
    md = index.metadata
    common = index_columns(index)
    rows: dict[str, list[Row]] = {name: [] for name in TABLES}
    rows["metadata"].append(
        {
            **common,
            "tool_name": md.tool_info.name,
            "tool_version": md.tool_info.version,
            "tool_arguments": list(md.tool_info.arguments),
            "project_root": md.project_root,
            "protocol_version": scip.ProtocolVersion.Name(md.version),
            "text_document_encoding": scip.TextEncoding.Name(md.text_document_encoding),
        }
    )
    for doc in index.documents:
        rows["documents"].append(
            {
                **common,
                "relative_path": doc.relative_path,
                "language": doc.language,
                "n_symbols": len(doc.symbols),
                "n_occurrences": len(doc.occurrences),
            }
        )
        for info in doc.symbols:
            row = _symbol_info_row(info, doc.relative_path)
            row["exported"] = EXPORTED_DOC in info.documentation
            rows["symbols"].append({**common, **row})
            rows["relationships"].extend(
                {**common, **r} for r in _relationship_rows(info, doc.relative_path)
            )
        for occ, caller in zip(doc.occurrences, _callers(doc), strict=True):
            rows["occurrences"].append(
                {**common, **_occurrence_row(occ, doc.relative_path, caller)}
            )
    for info in index.external_symbols:
        row = _symbol_info_row(info, None)
        row["guessed"] = any("guessed" in d for d in info.documentation)
        rows["external_symbols"].append({**common, **row})
        rows["relationships"].extend({**common, **r} for r in _relationship_rows(info, None))
    return rows


def _arrow_schemas() -> dict[str, Any]:
    try:
        import pyarrow as pa
    except ImportError as e:  # pragma: no cover - exercised via MissingExtraError test
        raise MissingExtraError("pyarrow") from e

    common = [(c, pa.string()) for c in INDEX_COLUMNS]
    sym_cols = [
        ("is_local", pa.bool_()),
        ("scheme", pa.string()),
        ("manager", pa.string()),
        ("package", pa.string()),
        ("version", pa.string()),
        ("descriptor", pa.string()),
        ("name", pa.string()),
        ("owner", pa.string()),
        ("disambiguator", pa.string()),
        ("is_function", pa.bool_()),
        ("is_method", pa.bool_()),
        ("is_member", pa.bool_()),
        ("is_class", pa.bool_()),
    ]
    info_cols = [
        ("relative_path", pa.string()),
        ("symbol", pa.string()),
        ("kind", pa.string()),
        ("display_name", pa.string()),
        ("documentation", pa.list_(pa.string())),
        ("enclosing_symbol", pa.string()),
        *sym_cols,
    ]
    return {
        "metadata": pa.schema(
            [
                *common,
                ("tool_name", pa.string()),
                ("tool_version", pa.string()),
                ("tool_arguments", pa.list_(pa.string())),
                ("project_root", pa.string()),
                ("protocol_version", pa.string()),
                ("text_document_encoding", pa.string()),
            ]
        ),
        "documents": pa.schema(
            [
                *common,
                ("relative_path", pa.string()),
                ("language", pa.string()),
                ("n_symbols", pa.int64()),
                ("n_occurrences", pa.int64()),
            ]
        ),
        "symbols": pa.schema([*common, *info_cols, ("exported", pa.bool_())]),
        "external_symbols": pa.schema([*common, *info_cols, ("guessed", pa.bool_())]),
        "occurrences": pa.schema(
            [
                *common,
                ("relative_path", pa.string()),
                ("symbol", pa.string()),
                ("start_line", pa.int64()),
                ("start_char", pa.int64()),
                ("end_line", pa.int64()),
                ("end_char", pa.int64()),
                ("symbol_roles", pa.int64()),
                ("is_definition", pa.bool_()),
                ("syntax_kind", pa.string()),
                ("caller", pa.string()),
                ("internal_access", pa.bool_()),
                *sym_cols,
            ]
        ),
        "relationships": pa.schema(
            [
                *common,
                ("relative_path", pa.string()),
                ("symbol", pa.string()),
                ("related_symbol", pa.string()),
                ("is_reference", pa.bool_()),
                ("is_implementation", pa.bool_()),
                ("is_type_definition", pa.bool_()),
                ("is_definition", pa.bool_()),
            ]
        ),
    }


def index_to_arrow(index: scip.Index) -> dict[str, Any]:
    """Flatten an index into ``{table_name: pyarrow.Table}`` with fixed
    schemas (so empty tables still have typed columns)."""
    try:
        import pyarrow as pa
    except ImportError as e:
        raise MissingExtraError("pyarrow") from e

    schemas = _arrow_schemas()
    rows = index_to_rows(index)
    return {name: pa.Table.from_pylist(rows[name], schema=schemas[name]) for name in TABLES}


def write_parquet(
    index: scip.Index, out_dir: Path | str, *, hive: bool = False
) -> dict[str, Path]:
    """Write the tables under ``out_dir``.

    Flat layout (default): one ``<table>.parquet`` per table. Hive layout
    (``hive=True``): ``<table>/index_package=<pkg>/index_version=<v>/*.parquet``,
    so many packages can share one directory and DuckDB's
    ``read_parquet('<table>/**/*.parquet', hive_partitioning = true)`` reads
    them all.

    Returns ``{table_name: path}`` (file or table directory).
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise MissingExtraError("pyarrow") from e

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, table in index_to_arrow(index).items():
        if hive:
            root = out_dir / name
            pq.write_to_dataset(
                table,
                root_path=str(root),
                partition_cols=list(HIVE_PARTITION_COLUMNS),
                existing_data_behavior="overwrite_or_ignore",
                compression="zstd",
            )
            written[name] = root
        else:
            path = out_dir / f"{name}.parquet"
            pq.write_table(table, path, compression="zstd")
            written[name] = path
    return written


def write_duckdb(
    index: scip.Index, db_path: Path | str, *, overwrite: bool = False
) -> dict[str, int]:
    """Write every table into a DuckDB database file.

    Args:
        index: the index to export.
        db_path: database file to create or append into.
        overwrite: if True, existing tables with the same names are replaced;
            otherwise an existing table raises ``duckdb.CatalogException``.

    Returns ``{table_name: row_count}``.
    """
    try:
        import duckdb
    except ImportError as e:
        raise MissingExtraError("duckdb") from e

    tables = index_to_arrow(index)
    counts: dict[str, int] = {}
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        for name, table in tables.items():
            con.register("_scipr_src", table)
            verb = "CREATE OR REPLACE TABLE" if overwrite else "CREATE TABLE"
            con.execute(f'{verb} "{name}" AS SELECT * FROM _scipr_src')
            con.unregister("_scipr_src")
            counts[name] = table.num_rows
    finally:
        con.close()
    return counts
