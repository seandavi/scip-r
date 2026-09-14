#!/usr/bin/env bash
# Reproduce the limma call-graph example end to end.
#
# Needs: scip-r with the export extra, R with limma installed (plus pkgload,
# codetools, jsonlite for the resolver; igraph and duckdb for the R graph),
# git, and optionally Graphviz `dot` for the PNG from the Python script.
#
#   examples/limma/run.sh            # writes into examples/limma/out/
#   OUT=/tmp/limma examples/limma/run.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$HERE/out}
mkdir -p "$OUT"

# 1. Source at exactly the installed version, so `resolve --installed` sees
#    the same code the static pass indexed.
VERSION=${LIMMA_VERSION:-$(Rscript -e 'cat(as.character(packageVersion("limma")))')}
if [ ! -d "$OUT/src/.git" ]; then
  git clone -q https://git.bioconductor.org/packages/limma "$OUT/src"
fi
commit=$(git -C "$OUT/src" log --format=%H -S"Version: $VERSION" -- DESCRIPTION | tail -1)
if [ -z "$commit" ]; then
  echo "no commit in limma's history sets Version: $VERSION" >&2
  exit 1
fi
git -C "$OUT/src" checkout -q "$commit"
echo "limma $VERSION at $commit"

# 2. Static index, R-backed resolution against the installed namespace, tables.
scip-r index "$OUT/src" -o "$OUT/index.scip" --stats
scip-r resolve "$OUT/index.scip" --installed limma -o "$OUT/index.resolved.scip" --stats
scip-r export "$OUT/index.resolved.scip" --format duckdb -o "$OUT/limma.duckdb" --overwrite
scip-r export "$OUT/index.resolved.scip" --format parquet -o "$OUT/parquet"

# 3. Call graphs.
if command -v uv >/dev/null; then
  uv run --with networkx python "$HERE/call_graph.py" "$OUT/limma.duckdb" "$OUT"
else
  python "$HERE/call_graph.py" "$OUT/limma.duckdb" "$OUT"
fi
Rscript "$HERE/call_graph.R" "$OUT/limma.duckdb" "$OUT"
echo "done: see $OUT"
