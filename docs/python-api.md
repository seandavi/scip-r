# Using scip-r as a library

Everything the CLI does is available from Python. The CLI in
[`scipr/cli.py`](../src/scipr/cli.py) is a thin typer layer over these
functions, so anything you can do from the shell you can do in a script or
a notebook.

```python
import scipr
```

## Index a package

```python
from scipr import build_index, write_index

index = build_index("path/to/pkg")  # scip_pb2.Index protobuf message
write_index(index, "index.scip")
```

`build_index(pkg_dir, *, positions_out=None, tool_version=None)`

- `pkg_dir`: package root with `DESCRIPTION` and `R/`. A bare directory of
  `.R` files also works (name falls back to the directory name).
- `positions_out`: pass a list to receive every guessed call site as
  `{"file", "line", "character", "name", "enclosing"}`.
- Raises `NotADirectoryError` for a missing directory.

The returned object is the generated protobuf class
`scipr.scip_pb2.Index`; all SCIP fields (`documents`, `external_symbols`,
`metadata`) are available directly.

Lower-level pieces in `scipr.parser`: `read_description(pkg_dir)`,
`find_r_files(pkg_dir)`, `collect_top_level_symbols(files)` and the
`DocumentIndexer` class that walks one file.

## Read, summarise, print

```python
from scipr import load_index, summarize
from scipr.inspect import render_text

index = load_index("index.scip")
summary = summarize(index)  # IndexSummary dataclass
summary.one_line()  # "3 documents, 9 defined symbols, ..."
summary.to_dict()  # JSON-ready
print(render_text(index, show_locals=False))
```

`IndexSummary` fields: `documents`, `symbols`, `occurrences`,
`definitions`, `references`, `local_occurrences`, `external_symbols`,
`guessed_external_symbols`, `external_packages` (dict), `per_document`
(list of `DocumentSummary`).

## Work with symbol strings

```python
from scipr import parse_symbol

p = parse_symbol("scip-r cran stats 4.6.0 sd().")
p.package, p.version, p.name, p.is_function  # 'stats', '4.6.0', 'sd', True
parse_symbol("scip-r cran pkg 1.0 width(Interval).").disambiguator  # 'Interval'
parse_symbol("scip-r cran pkg 1.0 Foo#").is_class  # True
parse_symbol("local 3").is_local  # True
```

`scipr.symbols` also exposes the constructors `symbol_string`,
`descriptor_for`, `method_descriptor` and `class_descriptor` if you emit
symbols in the same convention from another tool.

## Flatten to tables

`scipr.export.index_to_rows(index)` needs no optional dependencies and
returns `{table_name: [row_dict, ...]}` for the six tables described in
[parquet-schema.md](parquet-schema.md). With the `export` extra installed:

```python
from scipr.export import index_to_arrow, write_parquet, write_duckdb

tables = index_to_arrow(index)  # {name: pyarrow.Table}
write_parquet(index, "out-dir/")  # {name: Path}
write_duckdb(index, "index.duckdb", overwrite=True)  # {name: row_count}
```

A missing optional dependency raises `scipr.export.MissingExtraError`
(a subclass of `ImportError`).

## Resolve with R

```python
from scipr.resolve import resolve_index, metadata_record

result = resolve_index(index, "path/to/pkg")  # runs Rscript; mutates index
result.stats  # MergeStats
result.resolution  # raw JSON from resolve.R
record = metadata_record(
    result.resolution,
    result.stats,
    index_path="index.resolved.scip",
    index_bytes=index.SerializeToString(),
)
```

The two halves can be used separately:

- `run_resolver(pkg_dir, names=None, rscript=None, timeout=600)` runs the
  bundled `resolve.R` and returns its JSON.
- `merge_resolution(index, resolution)` applies a resolution record to an
  index in place and returns `MergeStats`. It is idempotent.
- `guessed_names(index)` lists the names worth asking R about;
  `resolve_run_id(index)` reads the stamp back.

Errors: `RscriptNotFoundError`, `ResolverError` (carries `returncode` and
`stderr`), and `subprocess.TimeoutExpired`.

## Stability

The package is pre-1.0. Names exported from `scipr/__init__.py`
(`build_index`, `load_index`, `write_index`, `summarize`, `parse_symbol`,
`IndexSummary`, `ParsedSymbol`) and the `scipr.export` / `scipr.resolve`
functions above are the intended public surface; anything else may change
without notice. Changes are listed in [CHANGELOG.md](../CHANGELOG.md).
