# Robustness sweep: Bioconductor 3.23 software packages

Tier-1 run of [`sweep.py`](sweep.py) over every package in the Bioconductor
3.23 software repository (`bioconductor.org/packages/release/bioc`), static
pass only, no R. Raw outputs are in [`results/bioc-3.23/`](results/bioc-3.23/).

- Date: 2026-09-14. scip-r 0.2.0 (this branch), Python 3.13.3, tree-sitter-r 1.3.0.
- Machine: Apple Silicon laptop, 16 cores, macOS 26.6; 8 batch workers.
- Corpus: 2,384 tarballs, 7.2 GB; 35,161 R files, 7.35 million lines, 281 MB of R source.

## Headline

| | |
| --- | --- |
| packages indexed | **2,384 of 2,384**, no failures |
| packages with parse errors | **2** (0.08%), both the same upstream grammar bug ([#41](https://github.com/seandavi/scip-r/issues/41)) |
| symbols / occurrences | 175,995 / 11,526,363 |
| external symbols, guessed | 319,480, of which 224,845 (70.4%) are guesses the static pass cannot resolve without R |
| wall time, 8 workers | 207 s for the whole repository (690 packages/min) |
| worker peak RSS | p50 65 MiB, max 242 MiB |

## Throughput

Two numbers, because they answer different questions.

**Whole-repository wall time** (8 workers, tarballs already on disk):
140 to 207 s across three runs on the same machine. About 40% of the
per-package total is tarball extraction, which is dominated by `src/`
and `inst/` content the indexer never reads (mzR: 1,203 lines of R, 12.7 s,
almost all unpacking its bundled C++). Parallel per-package timings are
therefore contention-dominated and not reproducible to better than 2x;
`report.md`'s per-kLOC distribution should be read as an upper bound.

**Static-pass cost, one worker, median of three runs**, on the packages
the parallel run flagged as slowest per line plus the four largest:

| package | lines | files | index s | ms per kLOC | lines/s |
| --- | --- | --- | --- | --- | --- |
| RnBeads | 46,449 | 61 | 1.312 | 28 | 35,406 |
| xcms | 44,395 | 66 | 0.963 | 22 | 46,086 |
| mixOmics | 41,494 | 124 | 1.249 | 30 | 33,211 |
| OmnipathR | 33,116 | 82 | 0.898 | 27 | 36,861 |
| limma | 14,741 | 102 | 0.925 | 63 | 15,928 |
| edgeR | 9,422 | 119 | 0.762 | 81 | 12,363 |
| siggenes | 4,303 | 102 | 0.408 | 95 | 10,554 |
| goProfiles | 3,470 | 119 | 0.633 | 182 | 5,484 |
| combi | 3,046 | 57 | 0.271 | 89 | 11,256 |
| rsbml | 2,439 | 55 | 0.275 | 113 | 8,876 |
| mirTarRnaSeq | 2,174 | 45 | 0.252 | 116 | 8,624 |
| BiocGenerics | 2,060 | 74 | 0.239 | 116 | 8,612 |
| **all twelve** | 207,109 | | 8.19 | 40 | 25,295 |

The cost model that fits this table is roughly **5 ms per file plus 20 µs
per line**: the "slow per line" packages are simply packages with many
tiny files. tree-sitter parsing itself is a small fraction (goProfiles:
0.02 s of parsing in 0.6 s); the rest is file I/O and the Python walker.
There is no pathological input in the corpus.

## Parse health

Two packages, one grammar bug. `ggtree` (`R/tree-utilities.R`) and
`PharmacoGx` (`R/computeDrugSensitivity.R`) both close a `[[` subscript
with two separate `]` tokens (`x[[i] ]`, or `]` on the next line), which
R accepts and tree-sitter-r does not. The error node spans the rest of
each file, so occurrences after that point in those two files are
unreliable. Reported as [#41](https://github.com/seandavi/scip-r/issues/41)
for upstream; the per-file positions are in
`results/bioc-3.23/diagnostics-*.json`.

A third package, `nucleR`, showed up in the first run with a genuinely
broken file in `R/TODO/`. R CMD INSTALL ignores subdirectories of `R/`,
and scip-r now does too; that run found four packages keeping drafts,
examples or notebook checkpoints under `R/`.

## Output sanity

Four packages have R files but no symbols:

- `AHMassBank`, `alabaster`, `assorthead`: a single roxygen-only
  `pkg-package.R`. Correct.
- `convert`: three files whose whole API is `setAs()` coercion methods,
  which the static pass walks but does not treat as definitions (497
  occurrences, 0 symbols). Filed as
  [#42](https://github.com/seandavi/scip-r/issues/42).

## What the guessed fraction means

70% of external symbols are guesses, and 143 packages with 20 or more
external references have every one guessed. The dominant cause is not a
parser weakness but NAMESPACE style: those packages use whole-namespace
`import(pkg)` rather than `importFrom(pkg, name)`, and without R (or an
export list for `pkg`) the static pass cannot know which names come from
where. `scip-r resolve` closes this gap deterministically: on limma
it resolved 243 of 250 guesses. Resolving `import()` statically from a
fleet-wide export table is a natural extension of
[#27](https://github.com/seandavi/scip-r/issues/27).

## What changed because of this run

- `find_r_files` no longer recurses into subdirectories of `R/`.
- `build_index` reads each source file once instead of three times;
  freshly extracted files cost about 3 ms per open on this machine, and
  that was most of the per-package time (goProfiles 1.6 s to 0.5 s).
- Parse diagnostics, size stamps and per-phase timings now exist, so the
  next run needs no ad-hoc scripts to explain its outliers.

## Reproducing

```bash
uv run python benchmarks/sweep.py all --out sweeps/bioc --jobs 8
# sequential timing of a few packages, three runs
uv run scip-r batch --manifest some.txt -o run1 --jobs 1
```

Tier 2 (accuracy against R) and tier 3 (a pinned corpus in CI) are
[#39](https://github.com/seandavi/scip-r/issues/39) and
[#40](https://github.com/seandavi/scip-r/issues/40).
