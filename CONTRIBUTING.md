# Contributing to scip-r

Thanks for your interest. scip-r is small and heuristic by design, so the
most valuable contributions are usually **R syntax cases that index wrong**,
with a minimal `.R` snippet that reproduces them.

## Ground rules

- Be kind; see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Open an issue before a large change so we can agree on the approach.
  Small fixes and test additions can go straight to a pull request.
- By contributing you agree your work is released under the
  [MIT License](LICENSE).

## Development setup

scip-r uses [uv](https://docs.astral.sh/uv/). You need a C compiler
(`tree-sitter-r` is built from its git tag; no R installation is needed).

```bash
git clone https://github.com/seandavi/scip-r.git
cd scip-r
uv sync --all-extras          # dev tools + the optional [export] extra
uv run pre-commit install     # ruff on every commit
```

Everyday commands:

```bash
uv run pytest                          # full suite (~1 s)
uv run pytest -m "not export"          # skip Parquet/DuckDB tests
uv run pytest -m r                     # only the tests that need R (auto-skipped without it)
uv run pytest --cov=scipr              # with coverage
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run scip-r index tests/fixtures/testpkg -o /tmp/index.scip --stats
```

## Project layout

| Path | What |
| --- | --- |
| `src/scipr/parser.py` | tree-sitter walk; two passes (collect top-level names, then emit occurrences) |
| `src/scipr/symbols.py` | the `scip-r cran <pkg> <version> <descriptor>` symbol convention |
| `src/scipr/inspect.py` | load / summarise / pretty-print an index (no optional deps) |
| `src/scipr/export.py` | flatten to rows; Parquet and DuckDB writers behind the `export` extra |
| `src/scipr/cli.py` | typer app: `index`, `stats`, `print`, `export` |
| `src/scipr/scip_pb2.py` | **generated** from `proto/scip.proto`; do not hand-edit |
| `src/scipr/resolve.py`, `src/scipr/r/resolve.R` | optional second pass with a real R session (`scip-r resolve`); see `docs/resolve.md` |
| `actions/resolve/` | composite GitHub Action running both passes |
| `docs/` | ADRs, Parquet schema, Python API, resolver guide, security stance |
| `examples/limma/` | reproducible call-graph example on a real package (networkx and igraph) |
| `benchmarks/` | robustness sweep over a whole repository (`sweep.py`), results, tier-2/3 plans |
| `tests/fixtures/testpkg` | tiny R package used by most tests, plus its golden index |

## Adding a parser change

1. Add a failing test in `tests/test_parser.py`. Use the `make_package`
   fixture to write a throwaway package and assert on
   `(range, symbol, roles)` tuples; keep snippets minimal.
2. Make it pass in `parser.py`.
3. If the golden test (`tests/test_golden.py`) now fails, the change
   affected the fixture package's output. If that's intended, regenerate:

   ```bash
   uv run scip-r index tests/fixtures/testpkg -o tests/fixtures/testpkg-index.scip
   ```

   and explain the diff in the commit message (`uv run scip-r print` on the
   old and new file is a quick way to see it).
4. Note user-visible changes in `CHANGELOG.md` under *Unreleased*.

## Regenerating the protobuf bindings

Only needed when bumping `proto/scip.proto` to a newer upstream revision.
The generated file pins a minimum `protobuf` runtime version; keep the
`protobuf` pin in `pyproject.toml` in step with it.

```bash
uvx --from grpcio-tools python -m grpc_tools.protoc \
    -I proto --python_out=src/scipr --pyi_out=src/scipr proto/scip.proto
```

## Commit and pull-request conventions

- Small, focused commits with descriptive messages (what and why).
- Branch from `main`; open the PR against `main`.
- CI must be green: ruff, mypy, pytest on Linux and macOS, and a
  successful `uv build`.
- Fill in the PR template. Link the issue if there is one.

## Releasing (maintainers)

1. Update `CHANGELOG.md`: move *Unreleased* into a new version section.
2. Bump `__version__` in `src/scipr/__init__.py`.
3. Commit, tag `vX.Y.Z`, push the tag. The release workflow builds the
   sdist and wheel, attaches them to a GitHub Release, and publishes to
   PyPI via trusted publishing if the `pypi` environment is configured.
