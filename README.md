# scip-r

[![CI](https://github.com/seandavi/scip-r/actions/workflows/ci.yml/badge.svg)](https://github.com/seandavi/scip-r/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**scip-r reads the source code of an R package and writes down, for every
function and object it defines and every call it makes, *what* is defined
or called and *where*.** It does this by parsing the text of the `.R`
files, not by running them, so it works on any package without installing
R, the package, or its dependencies. The result is a small
[SCIP](https://github.com/scip-code/scip) index file, an open format for
code intelligence that tools like Sourcegraph and DuckDB can read.

## What is this for?

Think of the output as a queryable map of an R codebase. Questions it
lets you answer, at the scale of one package or all of CRAN and
Bioconductor:

- **"Who uses this?"** Find every package that calls `stats::sd`, a
  deprecated function, or a function you are about to change. Count how
  many call sites would break.
- **"What does this package depend on, really?"** Not what `DESCRIPTION`
  claims, but which functions from which packages the code actually
  calls, and how often.
- **"Where is this defined?"** Jump from a call in `R/pipeline.R` to the
  definition in `R/helpers.R`, across files, without opening R. Feed the
  index to Sourcegraph or another SCIP consumer and get hover and
  go-to-definition for R.
- **"How is the ecosystem shaped?"** Build call graphs, measure API
  surface, track adoption of a new function across thousands of
  packages, or spot copy-pasted helpers. Export to Parquet and query with
  DuckDB, or drop the files on object storage and query from a browser.
- **"Which version of `dplyr` was this built against?"** With R
  available, `scip-r resolve` records the exact installed versions and
  the R environment alongside the index.
- **"What changed?"** Index two versions of a package and diff the
  symbol tables to see added, removed and renamed functions.

It is the right tool when you need *approximate answers across many
packages cheaply*. It is the wrong tool when you need *exactly* which
method an S4 or R6 call dispatches to at runtime; that requires running
R (see [Optional: resolving call sites via a real R
session](#optional-resolving-call-sites-via-a-real-r-session-ci)).

## Why does it exist?

The [official SCIP indexer
list](https://github.com/scip-code/scip#tools-using-scip) covers Java,
TypeScript, Rust, C/C++, Ruby, Python, C#, Dart, and PHP. There is no R
indexer, static or otherwise. scip-r fills that gap with the cheapest
approach that gives useful answers: a syntax-directed pass over
[tree-sitter-r](https://github.com/r-lib/tree-sitter-r) parse trees.

## Quick start

```bash
scip-r index path/to/pkg -o index.scip --stats   # parse R/ and write the index
scip-r index pkg_1.0.tar.gz -o index.scip        # a CRAN/Bioconductor tarball works too
scip-r stats index.scip                          # what did we find?
scip-r print index.scip --no-locals              # readable dump
scip-r export index.scip --format duckdb         # then query it with SQL
scip-r resolve index.scip --pkg path/to/pkg      # optional: ask a real R session
scip-r batch pkgs/ -o out --jobs 8 --export parquet --hive   # many packages at once
```

## What it extracts

- Top-level definitions via `<-`, `=`, `<<-`, `->`, `->>` and chained
  assignment (functions and plain objects), including string-literal
  targets like `"%+%" <- function(a, b)`, resolved **across files in the
  same package**: a call in `R/pipeline.R` to a function defined in
  `R/helpers.R` resolves correctly.
- Function parameters, `for`-loop variables and in-body assignments, as
  SCIP "local" symbols scoped to that one function.
- `pkg::fun(...)` / `pkg:::fun(...)` calls, and `pkg::name` used as a
  value, emitted as references to synthetic external symbols (so you get
  *something* to hover/navigate to even without indexing the target
  package separately).
- Unqualified calls that don't resolve to a local or a package symbol are
  recorded as calls into a synthetic `base` package. This is a **guess**,
  not a semantic fact (see Limitations), and is labeled as such in the
  emitted documentation string on that symbol.

## What it does not do

This is a syntax-directed pass, not a compiler frontend. It has no model
of R's runtime name resolution, so it does not attempt:

- **S3/S4/R6 method dispatch.** `setMethod("show", "MyClass", ...)` is
  visible as a call to `setMethod`, but resolving *which* `show` a call
  site dispatches to at runtime requires evaluating class hierarchies.
- **`library()`/`require()`-driven scope changes.** If a package attaches
  `dplyr` and calls `filter()` unqualified, this tool cannot know that
  from source alone. Those calls fall into the "guessed base" bucket,
  which really means "unqualified call, unresolved".
- **NSE-aware argument matching** (`dplyr` verbs, formula-based APIs).
- **Version-pinned external symbols.** `pkg::fun` references use `.` as
  the version because source alone doesn't say which version of `pkg` is
  intended.

If you have R available, `scip-r resolve` (below) replaces the guesses
with real namespace lookups. scip-r itself is the cheap,
no-R-runtime alternative, good for ecosystem-wide static analytics (call
graphs, deprecated-API usage, cross-package reference counts) where
approximate resolution across thousands of packages beats perfect
resolution on one.

## Install

`tree-sitter-r` ships Python bindings in its repository but has no PyPI
release, so it is installed from its git tag. You need a C compiler
(`gcc`/`clang`) and `git`; no R is required.

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "scip-r[export] @ git+https://github.com/seandavi/scip-r" \
    --with "tree-sitter-r @ git+https://github.com/r-lib/tree-sitter-r@v1.3.0"
```

With pip:

```bash
pip install "tree-sitter-r @ git+https://github.com/r-lib/tree-sitter-r@v1.3.0"
pip install "scip-r[export] @ git+https://github.com/seandavi/scip-r"
```

Drop `[export]` if you don't need the Parquet/DuckDB writers.

## Usage

### `scip-r index`

```bash
scip-r index path/to/pkg -o index.scip --stats
```

`path/to/pkg` should be a package root (has `DESCRIPTION` and an `R/`
directory) or a source tarball. Only files under `R/` are indexed, in
`Collate` order; `tests/`, `vignettes/` and `src/` are out of scope. The
static pass also reads `NAMESPACE`: exported symbols are marked
`@export`, `importFrom` names resolve to their package without R, and
`S3method` registrations link methods to generics. `--manager` overrides
the manager inferred from DESCRIPTION (`biocViews` means `bioconductor`).
Add `--emit-positions positions.json` to also dump every guessed call
site (file, position, name, enclosing function).

### `scip-r batch`: many packages

```bash
scip-r batch pkgs/ tarballs/*.tar.gz --manifest more.txt -o out --jobs 8 \
    --resolve --export parquet --hive
```

Each package lands in `out/<package>/`; failures are isolated and listed
in `out/summary.jsonl`; with `--hive` all Parquet output forms one
partitioned dataset under `out/parquet/`.

### `scip-r stats` and `scip-r print`

```bash
scip-r stats index.scip            # counts per index and per document
scip-r stats index.scip --json
scip-r print index.scip --no-locals  # readable dump, like `scip print`
scip-r print index.scip --json       # protobuf JSON
```

The official [`scip` CLI](https://github.com/scip-code/scip) works on the
output too: `scip print --from index.scip`, `scip stats --from index.scip`.

### `scip-r export`: Parquet and DuckDB

Requires the `export` extra.

```bash
scip-r export index.scip --format parquet -o index-parquet/
scip-r export index.scip --format duckdb  -o index.duckdb [--overwrite]
```

Both produce the same six tables: `metadata`, `documents`, `symbols`,
`external_symbols`, `occurrences`, `relationships`. Every row carries the
indexed package, version, manager and resolve run id; every symbol-bearing
row also carries the symbol string pre-parsed into `package`, `version`,
`name`, `is_function`, `is_local` columns; occurrences carry the
`caller` (enclosing function) and whether the reference used `:::`.
Queries don't need to split strings:

```sql
-- who calls what, ignoring locals and definitions
select o.relative_path, o.package, o.name, count(*) as n
from occurrences o
where not o.is_local and not o.is_definition
group by all order by n desc;

-- which external packages does this package lean on?
select package, count(*) from external_symbols
where not guessed group by 1;

-- call graph
select caller, name, count(*) from occurrences
where caller is not null and not is_definition group by all;
```

Every column is documented in [docs/parquet-schema.md](docs/parquet-schema.md).

### Python API

```python
from scipr import build_index, summarize, write_index
from scipr.export import index_to_rows, write_parquet

index = build_index("path/to/pkg")  # scip_pb2.Index
print(summarize(index).one_line())
write_index(index, "index.scip")
rows = index_to_rows(index)  # {table: [dict, ...]}, no extras needed
write_parquet(index, "index-parquet/")  # needs scip-r[export]
```

## Symbol scheme

Package-level: `scip-r <manager> <package> <version> <descriptor>` where
the manager is `cran`, `bioconductor` or `r` (ships with R), and the
descriptor is `name().` for functions, `name(Sig).` for S4 methods,
`Name#` for classes, `Class#name().` / `Class#name.` for R6 and
reference-class members, and `name.` for other objects. External
references seen only from source use `.` for both manager and version;
`scip-r resolve` fills them in. Locals use SCIP's own `local N`
convention, scoped per document.

This is a project-specific convention, not part of the SCIP spec itself.
SCIP treats the symbol string as opaque and human-readable, so any
consistent scheme is valid. `scipr.symbols.parse_symbol` turns one back
into its parts.

## Optional: `scip-r resolve` with a real R session

The static pass guesses that an unqualified call like `mean(x)` lands in
base R. If R is installed (with `pkgload`, `codetools`, `jsonlite` and the
package's dependencies), a second pass replaces those guesses with what
R's namespace machinery actually says, fills in installed versions, links
S3 and S4 methods to their generics, and records the R environment:

```bash
scip-r resolve index.scip --pkg path/to/pkg --stats
# -> index.resolved.scip + index.resolved.scip.meta.json
```

The metadata record (R version, platform, every loaded namespace and its
version) shares a `run_id` with the index, so a fleet of runs can be
joined to the tables they produced. It does not resolve *which* method a
call dispatches to; that depends on runtime classes. Instead the index
models dispatch the SCIP way: call sites point at the generic and each
method carries an implementation relationship.

Details, requirements and the JSON contract: [docs/resolve.md](docs/resolve.md).
Design rationale: [ADR 0001](docs/adr/0001-r-based-resolution.md).
For CI, [`actions/resolve/`](actions/resolve/) runs both passes.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check . && uv run mypy
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout, how to add a
parser rule, and how to regenerate the golden index or the protobuf
bindings.

## License

scip-r is released under the [MIT License](LICENSE). It bundles the SCIP
protocol definition (Apache-2.0, Sourcegraph) and links against
tree-sitter-r (MIT); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
