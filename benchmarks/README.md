# Benchmarks

Three tiers, each answering a different question. Only the first is
implemented here; the other two are tracked as issues.

| tier | question | needs | status |
| --- | --- | --- | --- |
| 1. robustness sweep (`sweep.py`) | does the static pass survive the ecosystem, how fast, and where does the parser stumble? | Python only | this directory |
| 2. accuracy run | are the answers right? scored against R as ground truth | R + an installed corpus | [#39](https://github.com/seandavi/scip-r/issues/39) |
| 3. pinned regression corpus | did a parser change move the numbers? | CI | [#40](https://github.com/seandavi/scip-r/issues/40) |

## Tier 1: the robustness sweep

```bash
uv run python benchmarks/sweep.py all --out sweeps/bioc --jobs 8
# or step by step
uv run python benchmarks/sweep.py fetch  --out sweeps/bioc          # PACKAGES + every tarball
uv run python benchmarks/sweep.py run    --out sweeps/bioc --jobs 8 # scip-r batch, static pass only
uv run python benchmarks/sweep.py report --out sweeps/bioc          # report.md + report.json
```

Defaults to the current Bioconductor release's software repository; pass
`--repo https://cloud.r-project.org/src/contrib` for CRAN. Tarballs are
cached under `<out>/tarballs/` and re-used when their size matches.

Per package the batch records (in `batch/summary.jsonl`): status, files,
lines, bytes, symbol and occurrence counts, guessed fraction, tree-sitter
**parse errors** (error and missing nodes, with per-file positions in
`diagnostics.json`), per-phase **timings** (unpack, index, write) and the
worker's peak RSS. `batch/batch-meta.json` records scip-r and Python
versions, platform, CPU count, workers and wall time so runs can be
compared.

`report.md` summarises: throughput (lines per CPU-second, packages per
minute), timing and memory distributions (p50/p90/p99), parse-error
prevalence with the worst offenders, failures by exception type, the
slowest packages in absolute terms and per line, packages with files but
no symbols (parser blind spots), and the most guess-heavy packages.

### Reading the parse-error list

A parse error means tree-sitter-r could not parse a region of a file. The
index is still written, but occurrences inside the recovery region may be
wrong or missing. Causes, roughly in order of frequency:

- genuinely invalid R that the package never sources (files kept for
  reference, platform-specific stubs, templates with placeholders);
- R syntax the grammar does not handle yet (report to
  [r-lib/tree-sitter-r](https://github.com/r-lib/tree-sitter-r/issues));
- encoding problems (Latin-1 files without a declaration).

Each `diagnostics.json` gives the file and 0-based position, so the
offending construct is one `sed -n` away.

### What a run costs

Timing claims should come from a quiet local machine, medians over
repeated runs. CI runners are shared and noisy; use them for smoke checks
only. Results from a full run are in [RESULTS.md](RESULTS.md).
