# `scip-r resolve`: second-level resolution with R

The static pass guesses that an unqualified call like `mean(x)` lands in
base R. `scip-r resolve` replaces those guesses with answers from R's own
namespace machinery, and records the R environment that produced them.

```bash
scip-r index path/to/pkg -o index.scip
scip-r resolve index.scip --pkg path/to/pkg --stats
# -> index.resolved.scip and index.resolved.scip.meta.json
```

## Requirements

- `Rscript` on `PATH` (or `--rscript /path/to/Rscript`).
- R packages `pkgload`, `codetools`, `jsonlite`.
- The target package's own dependencies installed
  (`pak::local_install_deps("path/to/pkg")`). The package itself is loaded
  from source and does not need to be installed.

Without R the core indexer works unchanged; `resolve` exits with code 4
and a message.

## What changes in the index

| before | after |
| --- | --- |
| `scip-r cran base . sd().` (guessed) | `scip-r cran stats 4.6.0 sd().` (imported via NAMESPACE) |
| `scip-r cran stats . quantile().` (explicit `stats::`) | `scip-r cran stats 4.6.0 quantile().` (version filled) |
| `print.zresult().` with no relationships | `print.zresult().` implements `scip-r cran base 4.6.0 print().` |
| S4 method `width(Interval).` | relationship to its generic, local or external |
| no provenance | `tool_info.arguments` gains `resolve_run_id=…`, `resolver=…`, `r_version=…` |

Names R cannot find stay guessed and keep their "unresolved" note, so a
downstream query can still tell the two apart (`external_symbols.guessed`).

## The metadata record

`<output>.meta.json` describes the run:

```json
{
  "run_id": "d5a0f9725cacd711dc17af0d7855eead",
  "scip_r_version": "0.2.0",
  "resolver": {"name": "scip-r-resolve", "version": "0.1.0"},
  "package": {"name": "testpkg", "version": "0.1.0", "path": "..."},
  "environment": {
    "r_version": "4.6.0", "platform": "aarch64-apple-darwin23",
    "os": {"sysname": "Darwin", "release": "25.6.0", "machine": "arm64"},
    "packages": {"base": "4.6.0", "stats": "4.6.0", "R6": "2.6.1", "...": "..."}
  },
  "summary": {"names_resolved": 17, "names_unresolved": 0, "merge": {"resolved": 14, "...": "..."}},
  "index": {"path": "index.resolved.scip", "sha256": "..."}
}
```

`run_id` is also stamped into the index and exposed as
`metadata.resolve_run_id` by `scip-r export`, and `index.sha256` is the
digest of the resolved file. Either key joins a fleet of records to the
tables they describe. See [parquet-schema.md](parquet-schema.md).

## How it works

`src/scipr/r/resolve.R` (runnable on its own) does:

1. `pkgload::load_all(pkg, export_all = FALSE)`.
2. For every function in the namespace, `codetools::findGlobals()` lists
   its free names. Each name is looked up along the namespace's
   environment chain (namespace, imports, base namespace, search path)
   and attributed to the package that owns it, with `packageVersion()`.
   Names scip-r guessed at top level (outside any function) are passed in
   with `--names` and resolved the same way.
3. S3 registrations come from the namespace's `S3methods` table, S4
   methods from the `.__T__generic:package` method tables, S4/reference
   classes from `methods::getClasses()`, R6 classes from
   `R6ClassGenerator` objects.
4. Everything is written as one JSON document with a fresh `run_id`.

Python (`scipr.resolve`) rewrites occurrences by name, fills versions,
adds `is_implementation` relationships, rebuilds `external_symbols`, and
stamps the run id.

## What it still does not do

It does not resolve *which* method a call dispatches to. That depends on
runtime classes. The index represents dispatch the SCIP way: the call
points at the generic and each method links to it as an implementation.

## In CI

[`actions/resolve/`](../actions/resolve/) is a composite GitHub Action
that sets up R, installs the package's dependencies, and runs both
passes. See its `action.yml` for inputs.
