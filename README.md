# scip-r

A static [SCIP](https://github.com/scip-code/scip) indexer for R packages.
No R installation, no `library()` calls, no running code — it parses R
source with [tree-sitter-r](https://github.com/r-lib/tree-sitter-r) and
emits an `index.scip` file that any SCIP consumer (Sourcegraph, `scip
print`/`scip stats`, or a custom DuckDB/Parquet pipeline) can read.

This fills a real gap: the [official SCIP indexer
list](https://github.com/scip-code/scip#tools-using-scip) covers Java,
TypeScript, Rust, C/C++, Ruby, Python, C#, Dart, and PHP — there is no R
indexer, static or otherwise. This is a first pass at one.

## What it extracts

- Top-level `name <- value` / `name = value` definitions (functions and
  plain objects), resolved **across files in the same package** — a call
  in `R/pipeline.R` to a function defined in `R/helpers.R` resolves
  correctly.
- Function parameters and in-body assignments, as SCIP "local" symbols
  scoped to that one function.
- `pkg::fun(...)` / `pkg:::fun(...)` calls, emitted as references to
  synthetic external symbols (so you get *something* to hover/navigate to
  even without indexing the target package separately).
- Unqualified calls that don't resolve to a local or a package symbol are
  recorded as calls into a synthetic `base` package — this is a **guess**,
  not a semantic fact (see Limitations), and is labeled as such in the
  emitted documentation string on that symbol.

## What it does NOT do

This is a syntax-directed pass, not a compiler frontend. It has no model
of R's actual runtime name resolution, so it cannot and does not attempt:

- **S3/S4/R6 method dispatch.** `setMethod("show", "MyClass", ...)` is
  visible as a call to `setMethod`, but resolving *which* `show` a given
  call site actually dispatches to at runtime requires evaluating class
  hierarchies — that needs R, not a parser.
- **`library()`/`require()`-driven scope changes.** If a package attaches
  `dplyr` and calls `filter()` unqualified, this tool has no way to know
  that without a static list of what's attached, and doesn't attempt one.
  Those calls fall into the "guessed base" bucket, which is honestly
  telling you "unqualified call, unresolved" more than "this is base R."
- **NSE-aware argument matching** (e.g. `dplyr` verbs, formula-based APIs).
- **Version-pinned external symbols.** `pkg::fun` references use `.` as
  the version because source alone doesn't tell you which version of
  `pkg` is installed or intended.

If your use case needs true go-to-definition across dispatch, you need
something that runs R (e.g. wrapping the `languageserver` package, which
already does live semantic analysis, and having it emit SCIP in batch
mode instead of serving LSP requests interactively). This tool is the
cheap, no-R-runtime alternative — good for ecosystem-wide static analytics
(call graphs, deprecated-API usage, cross-package reference counts) where
approximate resolution across thousands of packages beats perfect
resolution on one.

## Install

`tree-sitter-r` has no published PyPI wheel, so build it from source once
(needs `gcc` and `git`, no R required):

```bash
git clone https://github.com/r-lib/tree-sitter-r.git
pip install ./tree-sitter-r
pip install .   # this package
```

A prebuilt Linux x86_64 wheel for `tree-sitter-r` is included under
`dist/` as a convenience if you're on that platform and don't want to
build it yourself.

## Use

```bash
scip-r /path/to/some/R/package -o index.scip --stats
```

`path/to/some/R/package` should be a package root (has `DESCRIPTION` and
an `R/` directory). Only files under `R/` are indexed — `tests/`,
`vignettes/`, `src/` (compiled code) are out of scope for this pass.

Inspect the result with the official `scip` CLI:

```bash
scip print --from index.scip
scip stats --from index.scip
```

## Symbol scheme

Package-level: `scip-r cran <package> <version> <name>().` (functions) or
`scip-r cran <package> <version> <name>.` (non-function top-level
objects). External references use the same shape with the calling
package's own version replaced by `.` since it's unknown from source.
Locals use SCIP's own `local N` convention, scoped per-document.

This is a project-specific convention, not part of the SCIP spec itself —
SCIP treats the symbol string as opaque and human-readable, so any
consistent scheme is valid.

## Optional: resolving call sites via a real R session (CI)

The tree-sitter pass above guesses that an unqualified call like `mean(x)`
resolves to base/attached R — it's a guess, not a fact, and is labeled as
such. `.github-action/` has a second, **untested** piece that closes that
gap by actually asking a live R session:

- `scip-r ... --emit-positions positions.json` writes out every guessed
  call site (only the guessed ones, not every token) as `{file, line,
  character}`.
- `.github-action/ls_index.R` spawns `Rscript -e 'languageserver::run()'`,
  speaks LSP over its stdio (Content-Length-framed JSON-RPC — there's no
  public batch API to call instead, `languageserver`'s internals are
  unexported R6 classes), and asks `textDocument/definition` at each of
  those positions.
- `.github-action/action.yml` is a composite GitHub Action that runs this
  as a CI step (`r-lib/actions/setup-r` + `setup-r-dependencies`, then the
  script, then `upload-artifact`) — meant to sit alongside an existing
  r-universe build/check workflow.

Two things worth knowing before wiring this into CI:

1. **It resolves within-package calls reliably** (the full `R/` tree is on
   disk). Cross-package resolution — a call into a dependency — only
   works if that dependency was installed **with source references kept**
   (`options(keep.source.pkgs = TRUE)`), which most RSPM/binary-cached CI
   installs skip for speed. Without it, dependency calls will often come
   back with no location, same as today.
2. **It's not fast.** The driver is written for correctness (one blocking
   request at a time) not throughput. GEOquery — a fairly ordinary
   mid-size Bioconductor package — has **1,186 guessed call sites**, i.e.
   1,186 sequential round-trips to resolve fully. Budget CI time (or add
   a cap on how many positions get resolved per run) accordingly.

This half of the project has not been run against a real R session — I
had no R runtime available to verify it. Treat the wire framing and
request shapes as spec-correct, but test `ls_index.R` locally before
trusting it in a CI pipeline.

## Architecture note

This was built to slot into a DuckDB/Parquet-over-object-storage pipeline:
SCIP's `Document`/`SymbolInformation`/`Occurrence` messages are naturally
tabular, so `index.scip` converts cleanly into a few Parquet tables
(documents, symbols, occurrences) for querying with DuckDB or DuckDB-WASM,
the same pattern used for other zero-backend, R2-hosted analytics here.
