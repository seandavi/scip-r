# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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
- Minimum Python is 3.10; `protobuf` is pinned to `>=7.35.1,<8` to match
  the generated bindings.
- The example package and its index live under `tests/fixtures/`; the
  languageserver composite action moved from `.github-action/` to
  `actions/ls-resolve/`.

### Fixed

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

- The prebuilt Linux `tree-sitter-r` wheel is no longer tracked in the
  repository; install `tree-sitter-r` from its git tag instead (see README).

## [0.1.0] - 2026-09-12

Initial prototype: two-pass tree-sitter indexer, argparse CLI, untested
languageserver resolver script.

[Unreleased]: https://github.com/seandavi/scip-r/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/seandavi/scip-r/releases/tag/v0.1.0
