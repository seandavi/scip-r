# Third-party notices

scip-r is MIT-licensed (see [LICENSE](LICENSE)). It depends on, vendors, or
derives from the following third-party work. Each is redistributed under its
own license, reproduced or linked below as that license requires.

## Vendored / derived files in this repository

### SCIP protocol definition (`proto/scip.proto`, `src/scipr/scip_pb2.py`, `src/scipr/scip_pb2.pyi`)

- Source: <https://github.com/scip-code/scip> (`scip.proto`)
- Copyright: Sourcegraph, Inc. and SCIP contributors
- License: Apache License 2.0, reproduced in full at [`proto/LICENSE`](proto/LICENSE)

`proto/scip.proto` is an unmodified copy of the upstream file. The two
`scip_pb2*` files are machine-generated from it by `protoc` and are therefore
derivative works of the same Apache-2.0 material. Per Apache-2.0 §4, this
notice and the license text travel with any redistribution of those files.

## Runtime dependencies

### tree-sitter-r

- Source: <https://github.com/r-lib/tree-sitter-r>
- Copyright: (c) 2025 tree-sitter-r authors
- License: MIT (below)

scip-r links against the compiled R grammar at runtime. It is installed
from the upstream git repository (there is no PyPI release); if you
redistribute a built wheel of `tree-sitter-r`, that wheel already contains
`LICENSE` in its `dist-info`. The license text is:

```
MIT License

Copyright (c) 2025 tree-sitter-r authors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```

### tree-sitter (`py-tree-sitter` and the tree-sitter C runtime)

- Source: <https://github.com/tree-sitter/py-tree-sitter>,
  <https://github.com/tree-sitter/tree-sitter>
- Copyright: (c) 2019 Max Brunsfeld, GitHub (bindings); (c) 2018 Max Brunsfeld (runtime)
- License: MIT

### protobuf (`google.protobuf` Python runtime)

- Source: <https://github.com/protocolbuffers/protobuf>
- Copyright: Google LLC
- License: BSD 3-Clause

### typer, click, rich

- Licenses: MIT (typer, rich), BSD 3-Clause (click)

## Optional `export` extra

### pyarrow

- Source: <https://github.com/apache/arrow>
- License: Apache License 2.0

### duckdb

- Source: <https://github.com/duckdb/duckdb>
- License: MIT

## Documentation

`CODE_OF_CONDUCT.md` is adapted from the Contributor Covenant, version 2.1,
available under CC BY 4.0 at
<https://www.contributor-covenant.org/version/2/1/code_of_conduct/>.
