# `scip-r resolve`: second-level resolution with R

The static pass guesses that an unqualified call like `mean(x)` lands in
base R. `scip-r resolve` replaces those guesses with answers from R's own
namespace machinery, and records the R environment that produced them.

```bash
scip-r index path/to/pkg -o index.scip
scip-r resolve index.scip --pkg path/to/pkg --stats
# -> index.resolved.scip and index.resolved.scip.meta.json

# on a build machine where the package is already installed (no toolchain,
# no pkgload compile step): resolve against the installed namespace
scip-r resolve index.scip --installed pkgname --stats
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
| `scip-r . base . mean().` (guessed) | `scip-r r base 4.6.0 mean().` |
| `scip-r . stats . quantile().` (explicit `stats::`) | `scip-r r stats 4.6.0 quantile().` (manager and version filled) |
| `scip-r . dplyr . filter().` | `scip-r cran dplyr 1.1.4 filter().` |
| `print.zresult().` implements `scip-r . base . print().` (static guess) | implements `scip-r r base 4.6.0 print().` |
| S4 method `width(Interval).` | relationship to its generic, local or external, signatures normalised (`ANY` padding) |
| class `B#` | relationship to each superclass (`contains`, R6 `inherit`) with package and version |
| static provenance stamps only | `tool_info.arguments` gains `resolve_run_id=…`, `resolver=…`, `r_version=…`, `bioc_version=…` |

Manager values: `cran`, `bioconductor` (DESCRIPTION has `biocViews`), `r`
(ships with R, `Priority: base`), `.` unknown.

Names R cannot find stay guessed and keep their "unresolved" note, so a
downstream query can still tell the two apart (`external_symbols.guessed`).

## The metadata record

`<output>.meta.json` describes the run:

```json
{
  "schema_version": 2,
  "run_id": "d5a0f9725cacd711dc17af0d7855eead",
  "scip_r_version": "0.2.0",
  "resolver": {"name": "scip-r-resolve", "version": "0.2.0"},
  "package": {"name": "testpkg", "version": "0.1.0", "manager": "cran", "path": "...", "load_mode": "load_all"},
  "source": {"package": "testpkg", "version": "0.1.0", "manager": "cran",
             "git_commit": "3ca29f5…", "git_dirty": "false"},
  "environment": {
    "r_version": "4.6.0", "platform": "aarch64-apple-darwin23", "bioc_version": "3.24",
    "os": {"sysname": "Darwin", "release": "25.6.0", "machine": "arm64"},
    "packages": {"base": {"version": "4.6.0", "manager": "r"}, "R6": {"version": "2.6.1", "manager": "cran"}}
  },
  "summary": {"names_resolved": 17, "names_unresolved": 0, "merge": {"resolved": 10, "...": "..."}},
  "index": {"path": "index.resolved.scip", "sha256": "..."}
}
```

`source` is whatever the static pass could see: git commit and dirty
flag for a checkout, `source_tarball` and `source_sha256` for a tarball,
and the Bioconductor/CRAN build fields from DESCRIPTION (`git_url`,
`git_branch`, `git_last_commit`, `date_publication`).

`run_id` is also stamped into the index and exposed as
`metadata.resolve_run_id` by `scip-r export`, and `index.sha256` is the
digest of the resolved file. Either key joins a fleet of records to the
tables they describe. See [parquet-schema.md](parquet-schema.md).

## How it works

`src/scipr/r/resolve.R` (runnable on its own) does:

1. `pkgload::load_all(pkg, export_all = FALSE)`, or `loadNamespace(name)`
   with `--installed`.
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
4. Each package touched gets a manager from `packageDescription()`
   (`biocViews`, `Priority`, `Repository`) and a version;
   `BiocManager::version()` is recorded when available.
5. Everything is written as one JSON document (schema 2) with a fresh
   `run_id`.

Python (`scipr.resolve`) rewrites occurrences by name, fills versions,
adds `is_implementation` relationships, rebuilds `external_symbols`, and
stamps the run id.

## Security note

Loading a package runs its `.onLoad` hook and, with `--pkg`, may compile
its `src/`. For untrusted packages (an ecosystem crawl), run the resolver
in a container without network access and as an unprivileged user, and
keep `--timeout`. The fuller stance is in [security.md](security.md).

## What it still does not do

It does not resolve *which* method a call dispatches to. That depends on
runtime classes. The index represents dispatch the SCIP way: the call
points at the generic and each method links to it as an implementation.

## In CI

[`actions/resolve/`](../actions/resolve/) is a composite GitHub Action
that sets up R, installs the package's dependencies, and runs both
passes. See its `action.yml` for inputs.
