# scip-r: notes for coding agents

Static SCIP indexer for R packages. Python, `src/` layout, managed with uv.

- Setup: `uv sync --all-extras`. Needs a C compiler; no R needed.
- Test: `uv run pytest` (fast, ~1 s). Lint: `uv run ruff check . && uv run ruff format .`. Types: `uv run mypy`.
- Entry points: `src/scipr/cli.py` (typer), `src/scipr/parser.py` (the indexer).
- `src/scipr/scip_pb2.py` is generated from `proto/scip.proto`; never hand-edit.
- Parser changes must come with a test in `tests/test_parser.py` using the `make_package` fixture. If `tests/test_golden.py` fails, either the change is wrong or the fixture index must be regenerated deliberately (see CONTRIBUTING.md).
- Symbol strings follow `scip-r cran <pkg> <version> <name>().`; helpers live in `src/scipr/symbols.py`. Locals are `local N`.
- Keep the optional `export` extra optional: `scipr.export` must import without pyarrow/duckdb; only the writer functions may import them.
- Third-party licensing matters: tree-sitter-r is MIT, `scip.proto` is Apache-2.0. Anything vendored goes into `THIRD_PARTY_NOTICES.md`.
