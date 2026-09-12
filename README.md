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
scip-r stats index.scip                          # what did we find?
scip-r print index.scip --no-locals              # readable dump
scip-r export index.scip --format duckdb         # then query it with SQL
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

If your use case needs true go-to-definition across dispatch, you need
something that runs R; see [`actions/ls-resolve/`](actions/ls-resolve/)
for an optional, R-based second pass. scip-r itself is the cheap,
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
directory). Only files under `R/` are indexed; `tests/`, `vignettes/`
and `src/` are out of scope. Add `--emit-positions positions.json` to
also dump every guessed call site for the languageserver pass below.

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
`external_symbols`, `occurrences`, `relationships`. Every symbol-bearing
row also carries the symbol string pre-parsed into `package`, `version`,
`name`, `is_function`, `is_local` columns, so queries don't need to
split strings:

```sql
-- who calls what, ignoring locals and definitions
select o.relative_path, o.package, o.name, count(*) as n
from occurrences o
where not o.is_local and not o.is_definition
group by all order by n desc;

-- which external packages does this package lean on?
select package, count(*) from external_symbols
where not guessed group by 1;
```

Column definitions are in the [`scipr.export`](src/scipr/export.py)
module docstring.

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

Package-level: `scip-r cran <package> <version> <name>().` (functions) or
`scip-r cran <package> <version> <name>.` (non-function top-level
objects). External references use the same shape with the version
replaced by `.` since it's unknown from source. Locals use SCIP's own
`local N` convention, scoped per document.

This is a project-specific convention, not part of the SCIP spec itself.
SCIP treats the symbol string as opaque and human-readable, so any
consistent scheme is valid. `scipr.symbols.parse_symbol` turns one back
into its parts.

## Optional: resolving call sites via a real R session (CI)

The tree-sitter pass guesses that an unqualified call like `mean(x)`
resolves to base/attached R. [`actions/ls-resolve/`](actions/ls-resolve/)
holds a second, **untested** piece that closes the gap by asking a live R
session:

- `scip-r index ... --emit-positions positions.json` writes out every
  guessed call site (only the guessed ones, not every token) as
  `{file, line, character}`.
- `ls_index.R` spawns `Rscript -e 'languageserver::run()'`, speaks LSP
  over its stdio, and asks `textDocument/definition` at each position.
- `action.yml` is a composite GitHub Action that runs this as a CI step
  (`r-lib/actions/setup-r` + `setup-r-dependencies`, then the script,
  then `upload-artifact`), meant to sit alongside an r-universe
  build/check workflow.

Two things worth knowing before wiring this into CI:

1. **It resolves within-package calls reliably** (the full `R/` tree is on
   disk). Cross-package resolution only works if that dependency was
   installed **with source references kept**
   (`options(keep.source.pkgs = TRUE)`), which most RSPM/binary-cached CI
   installs skip.
2. **It's not fast.** One blocking request per guessed position. A
   mid-size Bioconductor package can have over a thousand of them.

This half has not been run against a real R session. Treat the wire
framing and request shapes as spec-correct, but test `ls_index.R`
locally before trusting it in a pipeline.

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
