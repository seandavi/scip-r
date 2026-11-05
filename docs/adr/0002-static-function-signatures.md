# ADR 0002: Function signatures from the static pass, in `signature_documentation`

- Status: proposed
- Date: 2026-11-04
- Deciders: Marcel Ramos (proposer); Sean Davis (maintainer, review pending)

## Context

Consumers of the index want to know how to *call* a function, not only that
it exists. The motivating one is generating and checking R code against a
whole-repository index (Bioconductor 3.24 devel: 2,431 packages, 191k
symbols). Questions like "does `DESeq()` take `method =` or `test =`?" or
"what is `results()`'s default `alpha`?" need every formal and its default.

Before this change the only signature text was `documentation[0]`: the
first source line of the definition, cut at 120 characters. In that 3.24
index, 41% of the 44,062 exported `function(...)` definitions have
unbalanced parentheses in that line. Their formals are cut off mid-list,
for example:

```
DESeq <- function(object, test=c("Wald","LRT"),
```

S4 methods usually have no argument text at all (`documentation[0]` is
the `setMethod("f", c("A", "B"),` line, with the definition on the lines
after it), and parameters exist only as
`local N` symbols, which carry no names or defaults in `SymbolInformation`.

The obvious place to get exact formals is R itself (`formals()`), and
ADR 0001 already provides an R pass. But `scip-r resolve` needs `Rscript`
plus every target package's dependencies installed. That is manageable for
one package in CI, but a big lift for a 2,000+ package sweep, which today
runs the static pass only.

R formals, though, are syntax. For a literal `function(...)`, tree-sitter
gives the exact names and default expressions without evaluating anything,
and the parser already walks them to emit locals. Checked against R's
`formals()` on five Bioconductor packages (GenomicRanges, IRanges,
S4Vectors, SummarizedExperiment, MultiAssayExperiment), 1,239 of 1,246
plain functions matched exactly. All 7 differences were real API changes
between the checked-out source and the installed version, and the two
packages whose source and installed versions matched agreed 100%.

## Decision

The static pass fills SCIP's `SymbolInformation.signature_documentation`
(`language = "R"`) for every function-like symbol whose definition it can
see:

| symbol | formals taken from |
| --- | --- |
| `f <- function(...)` (chained assignments too) | the function literal |
| `setGeneric("f", def)` | `def`, or `useAsDefault` when `def` is absent or not a literal |
| `setMethod("f", sig, definition)` | `definition` when it is a literal; when it is a bare name, that function's formals if the package defines it |
| R6 / reference-class methods | the member's function literal |

Arguments to `setGeneric`/`setMethod` are matched the way R matches them
(exact names first, then positional fill), so
`setMethod("f", signature = "A", function(...))` finds its definition.

The text is a call signature on one line, in the form of R's usage
sections: `name(formal, formal = default, ...)`.

- The name is backticked only when R would need it (`` `%+%`(e1, e2) ``,
  `` `foo<-`(x, value) ``).
- Each default is its source text, with comments dropped and line breaks
  (plus their indentation) folded into one space, or into nothing next to
  a bracket. Statements inside brace blocks are joined with semicolons,
  so folding a newline cannot merge two expressions. Other spacing within
  a line and quoted tokens are kept verbatim, so the text still parses to
  the same expression (`2L` stays `2L`, and `"\n  "` stays as written).
  Literal newlines inside strings or backticked identifiers are preserved;
  these signatures can therefore span more than one line.
- For an S4 method, the formals are those of the **definition as
  written**, not the generic's formals that R rewrites the method to (with
  the real body moved into `.local()`). The generic's own symbol already
  carries the generic's formals; the method's text shows the arguments that
  method actually accepts.

Where the source cannot say, the field is left unset: function factories
(`f <- make_wrapper(g)`), aliases (`f <- g`), `setGeneric("summary")` with
no definition, `formals<-` rewrites, and method definitions that name a
function from another package. A null is a "don't know", never a guess.

`documentation[0]` keeps its first-line meaning, so existing queries do not
change.

## Alternatives considered

- **Get formals from the R pass (`scip-r resolve`).** It is exact, and it
  covers factories and aliases. Rejected as the *primary* source because it
  ties the most commonly needed data to the most expensive step: every
  package's dependencies must be installed, so whole-repository sweeps
  would lose signatures entirely. Kept as a natural follow-up: the resolver
  could fill `signature_documentation` only where the static pass left it
  unset.
- **Lengthen or un-truncate `documentation[0]`.** It is the smallest change,
  but it keeps raw source with comments, newlines and `{`. It still has
  nothing for S4 methods whose definition is a named function. It also
  overloads a field the SCIP spec says should hold prose, not signatures
  ("New indexers should only include non-code documentation in this
  field"). And it would change the meaning of a column that consumers
  already query.
- **Emit each parameter as a global symbol (`f().(x)`, kind `Parameter`).**
  It is structured and queryable per argument. Rejected *for now* because it
  replaces the `local N` ids every function parameter currently gets, which
  changes every occurrence row and the golden fixture, and SCIP gives no
  field for argument order (consumers would sort by definition position).
  It can build on this decision rather than replace it: the signature text
  stays the human-readable summary either way.
- **Structured formals in the export only** (a `parameters` column of
  `list<struct<name, default>>`). Rejected because the export is a pure
  flattening of the SCIP index. Data that exists only in the export would be
  invisible to other SCIP consumers (hover in editors, `scip print`).

## Consequences

- Every exported table row for a function, generic, S4 method or R6/RC
  method can carry its full signature. The Parquet/DuckDB export gains a
  nullable `signature` column on `symbols` and `external_symbols`. External
  symbols stay null until a resolver fills them.
- Whole-repository indexes get signatures at no extra cost or
  dependencies: no R, no installs, and one more walk over parameter nodes
  the parser already visits.
- Signatures reflect the **source**, not an installed build. That is what
  an index of a source tree should say, but it means a signature can
  disagree with an older or newer installed copy, as the 7 validation
  differences showed. Diffing two releases' indexes turns this into a
  feature: it is a cheap API-change report.
- The S4 method choice (definition formals, not R's rewritten ones) means
  `formals(getMethod(...))` in R can differ from the index for methods
  whose definition adds arguments behind the generic's `...`. This is
  documented here so consumers don't treat it as a bug.
- The golden fixture changes by exactly the seven new signatures. No
  occurrence, external symbol or other symbol field moves.
- The generated protobuf stub mistypes `signature_documentation` (inside
  `SymbolInformation`, the name `Signature` resolves to the `Kind.Signature`
  enum member), so code goes through a small typed accessor,
  `parser.signature_of()`, instead of touching the field directly.
