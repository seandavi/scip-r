# limma 3.69.2 call graph

Resolve run `1244de7bbee973b2447732303853f3fa`. 375 package symbols, 252 internal call edges, 56 method-to-generic edges, 4004 edges into dependencies, 326 exported.

### Most-called internal functions (weighted in-degree)

| function | calls |
| --- | --- |
| `loessFit` | 36 |
| `makeUnique` | 12 |
| `.meanHalf` | 12 |
| `lmFit` | 10 |
| `unwrapdups` | 10 |
| `read.columns` | 10 |
| `squeezeVar` | 9 |
| `plotWithHighlights` | 9 |
| `removeExt` | 8 |
| `eBayes` | 7 |
| `asMatrixWeights` | 7 |
| `chooseLowessSpan` | 7 |
| `propTrueNull` | 7 |
| `normalizeQuantiles` | 7 |
| `getEAWP` | 6 |

### Functions with the most distinct callees

| function | callees |
| --- | --- |
| `read.maimages` | 8 |
| `lmFit` | 7 |
| `fry.default` | 6 |
| `romer.default` | 6 |
| `normalizeWithinArrays` | 5 |
| `genas` | 5 |
| `roast.default` | 5 |
| `kegga.default` | 5 |
| `normalizeBetweenArrays` | 5 |
| `voomaLmFit` | 5 |
| `arrayWeights` | 4 |
| `normexp.fit` | 4 |
| `backgroundCorrect.matrix` | 4 |
| `squeezeVar` | 4 |
| `propTrueNull` | 4 |

### PageRank

| function | score |
| --- | --- |
| `plotWithHighlights` | 0.0172 |
| `protectMetachar` | 0.0135 |
| `unwrapdups` | 0.0132 |
| `backgroundCorrect.matrix` | 0.0121 |
| `makeUnique` | 0.012 |
| `backgroundCorrect` | 0.012 |
| `squeezeVar` | 0.0092 |
| `weightedLowess` | 0.0082 |
| `removeExt` | 0.0078 |
| `lmFit` | 0.0077 |
| `loessFit` | 0.0075 |
| `.matvec` | 0.0074 |
| `trimWhiteSpace` | 0.0073 |
| `trigammaInverse` | 0.0072 |
| `rankSumTestWithCorrelation` | 0.0069 |

### Exported entry points reaching the most functions

| function | reachable |
| --- | --- |
| `voomWithQualityWeights` | 34 |
| `voom` | 29 |
| `genas` | 27 |
| `predFCm` | 23 |
| `neqc` | 22 |
| `roast.default` | 21 |
| `mroast.default` | 20 |
| `romer.default` | 19 |
| `decideTests.MArrayLM` | 18 |
| `normalizeBetweenArrays` | 18 |
| `as.matrix.RGList` | 17 |
| `camera.default` | 17 |
| `write.fit` | 17 |
| `eBayes` | 16 |
| `normalizeWithinArrays` | 16 |

### Dependency usage (call sites)

| package | calls |
| --- | --- |
| `base (r)` | 8521 |
| `graphics (r)` | 327 |
| `stats (r)` | 322 |
| `methods (r)` | 120 |
| `grDevices (r)` | 54 |
| `utils (r)` | 19 |
| `statmod (cran)` | 17 |
| `AnnotationDbi (bioconductor)` | 16 |
| `BiasedUrn (.)` | 16 |
| `Biobase (bioconductor)` | 14 |
| `base (.)` | 11 |
| `MASS (cran)` | 8 |
| `gplots (.)` | 5 |
| `ellipse (.)` | 4 |
| `vsn (.)` | 3 |
| `illuminaio (bioconductor)` | 3 |
| `splines (r)` | 2 |
| `locfit (cran)` | 2 |
| `affy (.)` | 1 |
| `GO.db (.)` | 1 |

### Internal functions with no static caller

`.onAttach`, `.onLoad`, `.onUnload`


Not proof of dead code: calls through `do.call`, `match.fun` or dispatch are invisible to the static pass.

### Recursion

Self-recursive: none

Mutually recursive groups: none
