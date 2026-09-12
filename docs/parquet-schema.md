# Parquet / DuckDB export schema

`scip-r export --format parquet` writes one file per table into a
directory; `--format duckdb` writes the same tables into one database
file. Both come from `scipr.export.index_to_arrow`, which applies fixed
Arrow schemas, so empty tables still carry typed columns and files from
different packages can be unioned safely.

All positions are 0-based, matching SCIP. Strings are UTF-8.

## Shared columns: the parsed symbol

Every row that carries a `symbol` also carries it pre-parsed, so queries
need no string splitting. scip-r's symbol convention is
`scip-r cran <package> <version> <descriptor>`; see
[`scipr.symbols`](../src/scipr/symbols.py).

| column | type | meaning |
| --- | --- | --- |
| `symbol` | string | the raw SCIP symbol string |
| `is_local` | bool | `local N` symbol, only meaningful within `relative_path` |
| `scheme` | string | always `scip-r` for symbols this tool emits |
| `manager` | string | always `cran` |
| `package` | string | R package name; null for locals |
| `version` | string | package version; `.` when unknown (unresolved external reference) |
| `descriptor` | string | `name().`, `name(Sig).`, `name#` or `name.` |
| `name` | string | bare name without suffix or disambiguator |
| `disambiguator` | string | S4 signature classes joined by `,` (e.g. `Interval`, `A,B`); null otherwise |
| `is_function` | bool | descriptor ends in `).` (functions and S4 methods) |
| `is_method` | bool | S4 method (`name(Sig).`) |
| `is_class` | bool | class descriptor (`name#`): S4, reference or R6 class |

## `metadata` (one row)

| column | type | meaning |
| --- | --- | --- |
| `tool_name` | string | `scip-r` |
| `tool_version` | string | scip-r version that wrote the index |
| `tool_arguments` | list<string> | free-form provenance stamps (`resolve_run_id=…`, `resolver=…`, `r_version=…`) |
| `project_root` | string | `file://` URI of the indexed package root |
| `protocol_version` | string | SCIP protocol version name |
| `text_document_encoding` | string | `UTF8` |
| `resolve_run_id` | string | run id from `scip-r resolve`; null for a purely static index. Joins to the sidecar record's `run_id` |

## `documents`

| column | type | meaning |
| --- | --- | --- |
| `relative_path` | string | path under the package root, forward slashes (`R/foo.R`) |
| `language` | string | `R` |
| `n_symbols` | int64 | rows in `symbols` for this document |
| `n_occurrences` | int64 | rows in `occurrences` for this document |

## `symbols`

One row per symbol *defined* in a document (SCIP `SymbolInformation`).

| column | type | meaning |
| --- | --- | --- |
| `relative_path` | string | defining document |
| `kind` | string | `Function`, `Method`, `Class`, `Variable`, … (SCIP kind name) |
| `display_name` | string | unused by scip-r (null) |
| `documentation` | list<string> | first line of the definition (`f <- function(x, y) {`) |
| `enclosing_symbol` | string | unused by scip-r (null) |
| parsed symbol columns | | see above |

## `external_symbols`

Symbols referenced but defined elsewhere. Same columns as `symbols` with
`relative_path` null, plus:

| column | type | meaning |
| --- | --- | --- |
| `guessed` | bool | true when the static pass only guessed the target (documentation says so). Always false after `scip-r resolve` succeeds for that name |

`documentation` records provenance: `pkg::name`, optionally followed by
`(unresolved call target; guessed …)`, `(resolved by R namespace lookup
via imports|base|search|namespace)` or `(version from the installed
package)`.

## `occurrences`

One row per reference or definition site.

| column | type | meaning |
| --- | --- | --- |
| `relative_path` | string | document |
| `start_line`, `start_char` | int64 | 0-based start |
| `end_line`, `end_char` | int64 | 0-based end (exclusive char) |
| `symbol_roles` | int64 | SCIP role bitmask |
| `is_definition` | bool | `symbol_roles & Definition` |
| `syntax_kind` | string | unused by scip-r (`UnspecifiedSyntaxKind`) |
| parsed symbol columns | | see above |

## `relationships`

One row per SCIP `Relationship`. scip-r uses `is_implementation` to link
S3 and S4 methods to their generic.

| column | type | meaning |
| --- | --- | --- |
| `relative_path` | string | document of the owning symbol; null for external symbols |
| `symbol` | string | the method |
| `related_symbol` | string | the generic |
| `is_reference`, `is_implementation`, `is_type_definition`, `is_definition` | bool | SCIP relationship flags |

## Sidecar metadata record (`scip-r resolve`)

`scip-r resolve` writes `<output>.meta.json` next to the resolved index.
It is not a table, but it joins to the tables above on two keys:

- `run_id` equals `metadata.resolve_run_id`;
- `index.sha256` is the SHA-256 of the resolved `.scip` file.

Top-level fields: `schema_version`, `run_id`, `scip_r_version`,
`resolver{name,version}`, `package{name,version,path}`,
`environment{timestamp_utc,duration_seconds,r_version,r_version_string,platform,os{sysname,release,machine},locale,packages{name:version}}`,
`summary{names_resolved,names_unresolved,s3_methods,s4_methods,classes,merge{…}}`,
`index{path,sha256}`.

For ecosystem-scale runs, load these records into a `runs` table keyed by
`run_id` and join to `metadata` to slice results by R version or platform.

## Example queries

```sql
-- callers of each package function, ignoring locals and definitions
select o.relative_path, o.name, count(*) as n
from occurrences o
where o.package = 'testpkg' and not o.is_definition and not o.is_local
group by all order by n desc;

-- which dependency functions are used, at which installed version
select package, version, name, count(*) as call_sites
from occurrences
where not is_local and not is_definition and package <> 'testpkg'
group by all order by call_sites desc;

-- implementations of a generic
select r.symbol as method, r.related_symbol as generic
from relationships r where r.is_implementation;

-- what is still only a guess after resolution?
select name from external_symbols where guessed;
```
