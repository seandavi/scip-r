# Security stance

scip-r has two halves with very different risk profiles.

## The static pass (`scip-r index`, `batch`, `export`)

It parses text with tree-sitter and writes protobuf, Parquet or DuckDB.
It never evaluates R code. Risks are limited to the parser and writer
stack (tree-sitter, protobuf, pyarrow, duckdb) and to tarball extraction:

- Tarballs are extracted with Python's `filter="data"` (Python 3.12+),
  which refuses absolute paths, `..` components, links pointing outside
  the destination, and special files. On Python 3.10 and 3.11 the filter
  argument is unavailable and extraction is unfiltered; run the static
  pass on 3.12+ when indexing tarballs you did not build yourself.
- Only files under `R/` are read (dotfiles skipped). DESCRIPTION and
  NAMESPACE are parsed, not sourced.
- Keep dependencies current; Dependabot is configured for this repository.

Indexing untrusted source with the static pass is safe in the sense
that no code from the package runs.

## The resolver (`scip-r resolve`, `batch --resolve`, `actions/resolve`)

This half **runs the package's code**. Loading a namespace executes
`.onLoad` and `.onAttach` hooks, top-level code in `R/` is evaluated at
load time, and with `--pkg` pkgload may compile `src/` with the system
toolchain. Any of that can read files, open network connections, or spawn
processes with the privileges of the user running `Rscript`.

For packages you trust (your own, or a curated set on a build machine)
this is the same exposure as `R CMD INSTALL`. For anything else, treat
the resolver like running a test suite from an unknown author:

- **Isolate.** Run it in a container or VM with no network access, a
  read-only checkout, and a throwaway writable working directory. The
  bundled composite action runs on an ephemeral GitHub runner, which
  covers this for CI.
- **Least privilege.** Use an unprivileged user; do not run as root or
  with credentials in the environment (`GITHUB_TOKEN`, cloud keys,
  `~/.Renviron` secrets). scip-r itself needs none.
- **Bound it.** Keep `--timeout` (default 600 s) and, for batches, run
  each package in its own process (the default; a persistent R worker,
  issue #32, would trade this isolation for speed).
- **Prefer `--installed`** on machines where packages are already
  installed from a trusted repository: no compile step, no source tree
  needed, and the code that runs is what the repository shipped.
- **Record provenance.** The metadata record captures the git commit or
  tarball digest and the R environment, so a result can be traced back to
  exactly what was executed.

## Reporting

See [SECURITY.md](../SECURITY.md) for how to report a vulnerability in
scip-r itself.
