# Architecture decision records

Decisions that change scip-r's output or behaviour, are expensive to
reverse, or are not obvious from the code. Each record follows Nygard's
format (context, decision, alternatives considered, consequences). An
accepted record is never rewritten; a later decision supersedes or amends
it and the old record says so at the top. New records start from
[template.md](template.md).

| ADR | Decision | Status |
| --- | --- | --- |
| [0001](0001-r-based-resolution.md) | Second-level resolution with an R session, as an optional add-on | accepted |
| [0002](0002-static-function-signatures.md) | Function signatures from the static pass, in `signature_documentation` | proposed |
