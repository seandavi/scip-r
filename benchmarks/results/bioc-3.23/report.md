# scip-r robustness sweep

- scip-r 0.2.0 on Python 3.13.3, macOS-26.6.2-arm64-arm-64bit-Mach-O (16 CPUs, 8 workers), started 2026-09-14T13:11:27Z
- 2384 packages: **2384 indexed, 0 failed**
- 35,161 R files, 7,350,631 lines, 280.9 MB of source
- 175,995 symbols, 11,526,363 occurrences, 319,480 external symbols of which 224,845 guessed (70.4%)

## Throughput

- wall time 207 s; 690.8 packages/min
- static pass CPU time 1021 s; 7,198 lines/s

| per package | min | p50 | p90 | p99 | max |
| --- | --- | --- | --- | --- | --- |
| total seconds | 0.0259 | 0.5246 | 1.4189 | 3.1035 | 12.7179 |
| index ms per kLOC | 6.9281 | 175.2206 | 632.3438 | 2046.5856 | 57333.3333 |
| worker peak RSS MiB | 36.8 | 65.1 | 241.9 | 241.9 | 241.9 |

## Parse health

- 2 packages (0.08%) have tree-sitter parse errors: 17 error nodes in 2 files

### Packages with the most parse errors

| package | error_nodes | documents |
| --- | --- | --- |
| ggtree | 9 | 1 |
| PharmacoGx | 8 | 1 |

## Failures

_none_

_none_

## Slowest packages

| package | lines | total_s |
| --- | --- | --- |
| mzR | 1203 | 12.7179 |
| Rarr | 2836 | 5.891 |
| mixOmics | 41494 | 5.5467 |
| Rcpi | 8970 | 5.5449 |
| RbowtieCuda | 376 | 5.1819 |
| Rigraphlib | 119 | 5.0923 |
| GladiaTOX | 14625 | 4.7533 |
| affxparser | 10086 | 4.4686 |
| OmnipathR | 33116 | 4.3757 |
| Rhdf5lib | 175 | 4.2973 |
| MetMashR | 11530 | 4.0377 |
| Rbowtie2 | 971 | 3.9178 |
| goProfiles | 3470 | 3.8484 |
| RCy3 | 16900 | 3.6537 |
| openPrimeR | 22195 | 3.5394 |

### Slowest per line (packages over 2,000 lines)

| package | lines | ms_per_kloc |
| --- | --- | --- |
| goProfiles | 3470 | 874.8 |
| BiocGenerics | 2060 | 873.8 |
| rsbml | 2439 | 864.2 |
| mirTarRnaSeq | 2174 | 821.4 |
| combi | 3046 | 790.1 |
| Pigengene | 3687 | 668.2 |
| MSA2dist | 3317 | 609.3 |
| spiky | 2089 | 590.8 |
| rcellminer | 2665 | 565.0 |
| siggenes | 4303 | 542.3 |

## Implausible output (files but no symbols or occurrences)

| package | files | symbols | occurrences |
| --- | --- | --- | --- |
| AHMassBank | 1 | 0 | 0 |
| alabaster | 1 | 0 | 0 |
| assorthead | 1 | 0 | 0 |
| convert | 3 | 0 | 497 |

## Most guess-heavy (20+ external symbols)

| package | guessed | external |
| --- | --- | --- |
| ABSSeq | 80 | 80 |
| AIMS | 35 | 35 |
| AGDEX | 73 | 73 |
| ARRmNormalization | 46 | 46 |
| ASGSCA | 41 | 41 |
| AffyRNADegradation | 75 | 75 |
| AgiMicroRna | 133 | 133 |
| Anaquin | 110 | 110 |
| BAGS | 22 | 22 |
| BADER | 40 | 40 |
