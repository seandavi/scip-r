# ADR 0001: Second-level resolution with an R session, as an optional add-on

- Status: accepted
- Date: 2026-09-12

## Context

scip-r's core pass is syntax-directed: it parses R source with tree-sitter
and never runs R. That makes it cheap and dependency-free, but it cannot
know where an unqualified call such as `mean(x)` resolves. It records a
guess (`base`) with an "unresolved" note, and it cannot say which version
of a dependency a `pkg::fn` reference points at.

An earlier design (`actions/ls-resolve/ls_index.R`, never executed) drove
R's `languageserver` over LSP to resolve those call sites. Reviewing it:

- languageserver does not resolve dispatch either. Its go-to-definition on
  `print(x)` lands on the generic, never on `print.data.frame`. Nothing
  static can know the runtime class of `x`.
- LSP is a poor fit for batch work: one blocking round trip per position,
  and cross-package definitions only work if dependencies were installed
  with source references kept.

What an R runtime *can* give, deterministically:

1. exact namespace resolution for every free name in the package's
   functions, with `NAMESPACE` imports applied and installed versions;
2. the inventory of S3 methods registered, S4 generics/classes/methods and
   R6 classes defined;
3. names that only exist after evaluation (`assign()`, generated code).

## Decision

Add `scip-r resolve` as an optional second pass that requires R but is not
needed for the core indexer.

- **Mechanism**: a standalone script, `src/scipr/r/resolve.R`, run through
  `Rscript`. It loads the package from source with `pkgload::load_all()`,
  walks every function with `codetools::findGlobals()`, resolves each free
  name through the namespace's environment chain (namespace, imports,
  base, search path), and inventories S3/S4/R6 definitions. The package's
  dependencies must be installed; the package itself need not be.
- **Contract**: the R side emits JSON (`schema_version` 1). Python owns the
  SCIP emission and the symbol convention and merges the JSON into the
  index. RProtoBuf was rejected: it adds a system libprotobuf dependency
  for no benefit over a JSON join.
- **Join key**: resolution is keyed by name at namespace level. Locals are
  already excluded by the tree-sitter pass, and every top-level function
  shares the namespace as its enclosing environment, so per-function keys
  add nothing. Positions are still emitted with `name` and `enclosing`
  for other consumers.
- **Dispatch**: represented the SCIP way. Call sites point at the generic;
  each S3/S4 method carries a `Relationship{is_implementation}` to it.
  Consumers get "find implementations" rather than a false claim of
  resolved dispatch.
- **Provenance**: every run gets a `run_id`. It is stamped into the index
  (`metadata.tool_info.arguments`, and the `resolve_run_id` column of the
  exported `metadata` table) and into a sidecar JSON record describing the
  R environment (R version, platform, every loaded namespace and version)
  plus the SHA-256 of the resolved index. Either key joins the record to
  the data it describes.
- **Versions**: resolved external references carry the installed version.
  Ecosystem analytics wants the real version; consumers that need
  version-agnostic joins can normalise at query time.

## Consequences

- The core `pip install scip-r` path is unchanged and R-free.
- `scip-r resolve` needs `Rscript`, `pkgload`, `codetools`, `jsonlite` and
  the target package's dependencies. CI runs it in a dedicated job with
  `r-lib/actions/setup-r`; unit tests use a canned JSON fixture so the
  merge logic is tested without R.
- The static pass now also emits class, generic and S4 method symbols from
  `setClass`/`setGeneric`/`setMethod`/`setRefClass`/`R6Class` calls, so
  the R pass has symbols to attach relationships to.
- `actions/ls-resolve` is replaced by `actions/resolve`, which runs
  `scip-r index` and `scip-r resolve` in one step.
- True per-call-site dispatch would require dynamic tracing under a test
  suite. Out of scope.
