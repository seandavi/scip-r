# Security policy

## Scope

scip-r parses untrusted R source with tree-sitter and writes a protobuf
file. It never executes R code. The main risks are therefore in the
parser/runtime stack (tree-sitter, protobuf) and in the optional writers
(pyarrow, duckdb). Keep those dependencies current; Dependabot is
configured to open update PRs.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Email
seandavi@gmail.com with a description and, if possible, a reproducer.
You should hear back within a week. Fixes are released as a patch version
and noted in `CHANGELOG.md`.

## Supported versions

Only the latest release receives fixes.
