# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `scip-r index` accepts a source tarball (`.tar.gz`); the digest is
  stamped as provenance. `--manager` overrides the manager inferred from
  DESCRIPTION (`biocViews` gives `bioconductor`, `Priority: base` gives
  `r`, default `cran`).
- `scip-r batch`: index (and optionally resolve and export) many
  packages with `--jobs`, per-package output directories, isolated
  failures and a `summary.jsonl`.
- `scip-r resolve --installed NAME` resolves against an installed
  package via `loadNamespace()` instead of compiling a checkout.
- `scip-r export --format parquet --hive` writes a hive-partitioned
  dataset (`<table>/index_package=…/index_version=…/`).
- NAMESPACE is parsed statically: exported symbols carry `@export` (and
  an `exported` column), `importFrom` names resolve to their package
  without R, `S3method` registrations link methods to generics.
- R6 and reference-class members become `Class#name().` / `Class#name.`
  symbols; `self$x` / `private$x` resolve inside their methods. S4
  `contains` and R6 `inherit` become class-hierarchy relationships;
  `setMethod` named arguments are parsed and signatures drop trailing
  `ANY` so they match R's method tables.
- Every definition occurrence carries `enclosing_range`; the export
  derives a `caller` column from it. `pkg:::name` references are marked
  (`internal_access`). Every exported row carries `index_package`,
  `index_version`, `index_manager`, `resolve_run_id`.
- Provenance stamps in `tool_info.arguments`: `package`, `version`,
  `manager`, `git_commit`, `git_dirty`, `source_tarball`,
  `source_sha256`, Bioconductor/CRAN build fields; the resolver adds
  `bioc_version` and records package managers (resolution schema 2).
  The sidecar record gains a `source` block.
- `scip-r resolve`: an optional second pass that loads the package in a
  real R session (`pkgload`) and replaces guessed call targets with
  namespace-resolved packages and installed versions, links S3 and S4
  methods to their generics with `is_implementation` relationships, and
  writes a sidecar metadata record (R version, platform, loaded
  namespaces) keyed by a `run_id` that is also stamped into the index and
  exposed as `metadata.resolve_run_id` on export. See `docs/resolve.md`
  and ADR 0001.
- The static pass emits class, generic and S4 method symbols for
  `setClass`, `setRefClass`, `R6Class`, `setGeneric` and `setMethod`
  calls (`Name#`, `generic().`, `generic(Sig).`), and links a method to a
  generic defined in the same package.
- Guessed positions (`--emit-positions`) carry `name` and `enclosing`.
- Export tables gain `tool_arguments`, `resolve_run_id` (metadata) and
  `disambiguator`, `is_method`, `is_class` (parsed symbol columns).
- Docs: `docs/parquet-schema.md`, `docs/python-api.md`, `docs/resolve.md`,
  `docs/security.md`, `docs/adr/0001-r-based-resolution.md`, and an
  architecture diagram in the README.
- `actions/resolve/` composite action (index + resolve with R in CI); an
  R job in CI runs the resolver end to end on the fixture package.
- `scip-r` is now a typer application with subcommands: `index`, `stats`,
  `print`, and `export`; `--version`/`-V`.
- `scip-r export --format parquet|duckdb` converts an index into flat
  tables (`metadata`, `documents`, `symbols`, `external_symbols`,
  `occurrences`, `relationships`), with the symbol string pre-parsed into
  `package`/`version`/`name`/`is_function`/`is_local` columns. Requires the
  optional `export` extra (`pip install "scip-r[export]"`).
- Public Python API: `scipr.build_index`, `load_index`, `write_index`,
  `summarize`, `parse_symbol`; `scipr.export.index_to_rows` needs no
  optional dependencies.
- Test suite (pytest) with a golden index for the fixture package; CI on
  Linux and macOS for Python 3.10 to 3.13; ruff, mypy, pre-commit.
- LICENSE (MIT), THIRD_PARTY_NOTICES (tree-sitter-r, SCIP proto, and
  friends), CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue and PR
  templates, Dependabot, release workflow.

### Changed

- Source moved to a `src/` layout; build backend is hatchling; version is
  single-sourced from `scipr.__init__`.
- `for`-loop variables inside functions are now emitted as locals.
- Parameter default expressions resolve names in the function's own scope
  (previously the enclosing scope), matching R's lazy evaluation.
- `metadata.tool_info.version` records the installed scip-r version
  instead of a hard-coded string.
- Document `relative_path` always uses forward slashes.
- External symbols seen only from source now use `.` for the manager
  (`scip-r . stats . sd().`) instead of asserting `cran`; the resolver
  fills in `cran`, `bioconductor` or `r`.
- Minimum Python is 3.10; `protobuf` is pinned to `>=7.35.1,<8` to match
  the generated bindings.
- The example package and its index live under `tests/fixtures/`. The
  fixture now has a NAMESPACE, an S3 method, an S4 class/generic/method
  and an R6 class.

### Fixed

- Documents now declare `UTF8CodeUnitOffsetFromLineStart`, matching the
  byte columns tree-sitter reports (#11).
- `x$field` / `x@slot` no longer emit a reference to a top-level symbol
  that happens to share the field's name.
- `Collate` order is honoured when deciding which duplicate definition
  wins (#14). Dotfiles (including macOS `._*` files in tarballs) are
  skipped, as R does.
- Named-argument labels (`f(na.rm = na.rm)`) were indexed as variable
  reads of `na.rm`, producing a spurious reference occurrence at the label
  position whenever the label matched an in-scope name. Only the value is
  walked now. The fixture golden index was regenerated (45 to 42
  occurrences).
- `pkg::name` used as a value rather than called (`sapply(x, stats::median)`)
  was silently dropped; it is now a non-call external reference with a
  `name.` descriptor and `UnspecifiedKind`.
- Chained top-level assignment (`a <- b <- 1`) lost the inner name; every
  name in the chain is now defined.
- Right assignment (`1 -> x`, `->>`) and string-literal targets
  (`"%+%" <- function(a, b) ...`, `"foo<-" <- function(x, value) ...`) are
  now recognised as definitions.
- `index -o`, `--emit-positions` and `export -o` create missing parent
  directories instead of failing with a traceback.

### Removed

- The untested languageserver-based resolver (`ls_index.R` and the
  `.github-action/` composite action). `scip-r resolve` replaces it; see
  ADR 0001 for why LSP was the wrong mechanism.
- The prebuilt Linux `tree-sitter-r` wheel is no longer tracked in the
  repository; install `tree-sitter-r` from its git tag instead (see README).

## [0.1.0] - 2026-09-12

Initial prototype: two-pass tree-sitter indexer, argparse CLI, untested
languageserver resolver script.

[Unreleased]: https://github.com/seandavi/scip-r/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/seandavi/scip-r/releases/tag/v0.1.0
