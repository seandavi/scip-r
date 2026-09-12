"""Static symbol extraction for R source, emitted as a SCIP index.

Design notes (see README for the full write-up):

- Parsing is done with tree-sitter-r. No R interpreter is invoked, so this
  works on any R source tree without installing the package or its
  dependencies.
- Symbol resolution is heuristic, not semantic. It knows about:
    * top-level `name <- value` / `name = value` assignments (functions and
      plain objects) -- these become package-level SCIP symbols
    * function parameters and in-body assignments -- these become SCIP
      "local" symbols, scoped to one function
    * `pkg::fun(...)` / `pkg:::fun(...)` calls -- these become references to
      external symbols (one synthetic SymbolInformation per external name,
      so consumers get *something* to show even if the target package was
      never indexed)
    * bare calls to names that resolve to neither a local nor a package
      symbol are treated as calls into "base" R and recorded the same way
      external calls are -- this is a guess, not a fact (R's actual lookup
      also depends on the search path and can be shadowed), and is flagged
      as such in the emitted documentation string.
- What it does NOT do: S3/S4/R6 method dispatch, `library()`-driven scope
  changes, NSE-aware argument matching, or cross-package version pinning.
  A tree-sitter pass fundamentally can't resolve dispatch -- that needs
  running R. Treat this as "go to a plausible definition", not a compiler.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import tree_sitter_r as tsr
from tree_sitter import Language, Node, Parser

from . import scip_pb2 as scip

R_LANGUAGE = Language(tsr.language())

ASSIGN_OPS = {"<-", "="}
# `<<-` writes to an enclosing/global scope; we still treat the LHS name as
# a definition site the first time we see it, since tracking the *actual*
# enclosing scope would require full lexical analysis.
ASSIGN_OPS_ALL = ASSIGN_OPS | {"<<-"}


def read_description(pkg_dir: str) -> Tuple[str, str]:
    """Best-effort package name/version from a DESCRIPTION file."""
    name, version = os.path.basename(os.path.normpath(pkg_dir)), "0.0.0"
    desc_path = os.path.join(pkg_dir, "DESCRIPTION")
    if os.path.isfile(desc_path):
        text = open(desc_path, encoding="utf-8", errors="replace").read()
        m = re.search(r"^Package:\s*(\S+)", text, re.MULTILINE)
        if m:
            name = m.group(1)
        m = re.search(r"^Version:\s*(\S+)", text, re.MULTILINE)
        if m:
            version = m.group(1)
    return name, version


def find_r_files(pkg_dir: str) -> List[str]:
    r_dir = os.path.join(pkg_dir, "R")
    root = r_dir if os.path.isdir(r_dir) else pkg_dir
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith((".R", ".r")):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _node_range(node: Node) -> Tuple[int, int, int, int]:
    """SCIP ranges are [start_line, start_col, end_line, end_col], 0-based,
    with the end_line omitted when it equals start_line (3-element form)."""
    sr, sc = node.start_point
    er, ec = node.end_point
    if sr == er:
        return (sr, sc, ec)
    return (sr, sc, er, ec)


def symbol_string(scheme: str, package: str, version: str, descriptor: str) -> str:
    return f"{scheme} cran {package} {version} {descriptor}"


@dataclass
class TopLevelSymbol:
    name: str
    kind: str  # "function" or "value"
    signature: str
    file: str
    range: Tuple[int, ...]


@dataclass
class ExternalRef:
    package: str
    name: str
    is_call: bool
    guessed: bool  # True if resolved by base-R fallback rather than pkg::fn


class Scope:
    """One function's local names -> SCIP local symbol id."""

    def __init__(self, counter: "LocalCounter"):
        self.names: Dict[str, str] = {}
        self._counter = counter

    def define(self, name: str) -> str:
        if name in self.names:
            return self.names[name]
        sym = f"local {self._counter.next()}"
        self.names[name] = sym
        return sym

    def get(self, name: str) -> Optional[str]:
        return self.names.get(name)


class LocalCounter:
    def __init__(self):
        self._n = 0

    def next(self) -> int:
        n = self._n
        self._n += 1
        return n


def collect_top_level_symbols(
    files: List[str], package: str, version: str
) -> Dict[str, TopLevelSymbol]:
    """Pass 1: find every `name <- value` at module top level, across all
    files, so cross-file calls within the package resolve correctly."""
    parser = Parser(R_LANGUAGE)
    symbols: Dict[str, TopLevelSymbol] = {}
    for path in files:
        src = open(path, "rb").read()
        tree = parser.parse(src)
        for stmt in tree.root_node.children:
            node = stmt
            if node.type == "binary_operator":
                op = node.child_by_field_name("operator")
                lhs = node.child_by_field_name("lhs")
                rhs = node.child_by_field_name("rhs")
                if op is None or lhs is None or rhs is None:
                    continue
                if op.type not in ASSIGN_OPS_ALL or lhs.type != "identifier":
                    continue
                name = src[lhs.start_byte : lhs.end_byte].decode(
                    "utf-8", "replace"
                )
                kind = "function" if rhs.type == "function_definition" else "value"
                sig_line = src[node.start_byte : node.end_byte].split(b"\n")[0]
                if len(sig_line) > 120:
                    sig_line = sig_line[:117] + b"..."
                symbols[name] = TopLevelSymbol(
                    name=name,
                    kind=kind,
                    signature=sig_line.decode("utf-8", "replace"),
                    file=path,
                    range=_node_range(lhs),
                )
    return symbols


class DocumentIndexer:
    """Pass 2: walk one file's AST, emitting SCIP occurrences."""

    def __init__(
        self,
        src: bytes,
        relative_path: str,
        package: str,
        version: str,
        top_level: Dict[str, TopLevelSymbol],
    ):
        self.src = src
        self.relative_path = relative_path
        self.package = package
        self.version = version
        self.top_level = top_level
        self.document = scip.Document(relative_path=relative_path, language="R")
        self.external_refs: Dict[Tuple[str, str], ExternalRef] = {}
        self.symbols_emitted: set = set()
        self.guessed_positions: List[dict] = []

    def text(self, node: Node) -> str:
        return self.src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    def top_level_symbol_string(self, sym: TopLevelSymbol) -> str:
        desc = f"{sym.name}()." if sym.kind == "function" else f"{sym.name}."
        return symbol_string("scip-r", self.package, self.version, desc)

    def external_symbol_string(self, ref: ExternalRef) -> str:
        desc = f"{ref.name}()." if ref.is_call else f"{ref.name}."
        return symbol_string("scip-r", ref.package, ".", desc)

    def emit_occurrence(
        self, node: Node, symbol: str, roles: int = 0
    ) -> None:
        occ = self.document.occurrences.add()
        occ.range.extend(_node_range(node))
        occ.symbol = symbol
        occ.symbol_roles = roles

    def emit_top_level_definition(self, sym: TopLevelSymbol, name_node: Node) -> None:
        symbol = self.top_level_symbol_string(sym)
        self.emit_occurrence(name_node, symbol, scip.SymbolRole.Definition)
        if symbol not in self.symbols_emitted:
            self.symbols_emitted.add(symbol)
            info = self.document.symbols.add()
            info.symbol = symbol
            info.documentation.append(sym.signature)
            info.kind = (
                scip.SymbolInformation.Kind.Function
                if sym.kind == "function"
                else scip.SymbolInformation.Kind.Variable
            )

    def note_external(self, package: str, name: str, is_call: bool, guessed: bool):
        key = (package, name)
        if key not in self.external_refs:
            self.external_refs[key] = ExternalRef(package, name, is_call, guessed)

    def walk(self, node: Node, scope: Optional[Scope], counter: LocalCounter):
        t = node.type

        if t == "function_definition":
            inner = Scope(counter)
            params = node.child_by_field_name("parameters")
            if params is not None:
                for p in params.children:
                    if p.type != "parameter":
                        continue
                    name_node = p.child_by_field_name("name")
                    if name_node is not None and name_node.type == "identifier":
                        pname = self.text(name_node)
                        local_sym = inner.define(pname)
                        self.emit_occurrence(
                            name_node, local_sym, scip.SymbolRole.Definition
                        )
                    default = p.child_by_field_name("default")
                    if default is not None:
                        self.walk(default, scope, counter)  # defaults use outer scope
            body = node.child_by_field_name("body")
            if body is not None:
                self.walk(body, inner, counter)
            return  # already recursed into children we care about

        if t == "binary_operator":
            op = node.child_by_field_name("operator")
            lhs = node.child_by_field_name("lhs")
            rhs = node.child_by_field_name("rhs")
            if (
                op is not None
                and lhs is not None
                and rhs is not None
                and op.type in ASSIGN_OPS_ALL
                and lhs.type == "identifier"
            ):
                name = self.text(lhs)
                if scope is None and name in self.top_level:
                    # Top-level definition site.
                    self.emit_top_level_definition(self.top_level[name], lhs)
                elif scope is not None:
                    existing = scope.get(name)
                    if existing is None:
                        local_sym = scope.define(name)
                        self.emit_occurrence(lhs, local_sym, scip.SymbolRole.Definition)
                    else:
                        self.emit_occurrence(lhs, existing, 0)
                self.walk(rhs, scope, counter)
                return
            # fall through to generic recursion for non-assignment binary ops

        if t == "call":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn is not None:
                if fn.type == "identifier":
                    self.handle_name_use(fn, scope, is_call=True)
                elif fn.type == "namespace_operator":
                    pkg_node = fn.child_by_field_name("lhs")
                    fn_node = fn.child_by_field_name("rhs")
                    if pkg_node is not None and fn_node is not None:
                        pkg_name = self.text(pkg_node)
                        fn_name = self.text(fn_node)
                        self.note_external(
                            pkg_name, fn_name, is_call=True, guessed=False
                        )
                        self.emit_occurrence(
                            fn_node,
                            self.external_symbol_string(
                                ExternalRef(pkg_name, fn_name, True, False)
                            ),
                        )
                else:
                    self.walk(fn, scope, counter)
            if args is not None:
                self.walk(args, scope, counter)
            return

        if t == "identifier":
            self.handle_name_use(node, scope, is_call=False)
            return

        for child in node.children:
            self.walk(child, scope, counter)

    def handle_name_use(self, node: Node, scope: Optional[Scope], is_call: bool):
        name = self.text(node)
        if scope is not None:
            local_sym = scope.get(name)
            if local_sym is not None:
                self.emit_occurrence(node, local_sym, 0)
                return
        if name in self.top_level:
            self.emit_occurrence(node, self.top_level_symbol_string(self.top_level[name]))
            return
        if is_call:
            # Unresolved call target: guess it's base/stats R. Flagged as a
            # guess in the emitted documentation, not asserted as fact. Also
            # recorded by (line, character) so a languageserver-based pass
            # (see .github-action/ls_index.R) can replace the guess with a
            # real answer without re-resolving every token in the file.
            line, char = node.start_point
            self.guessed_positions.append(
                {"file": self.relative_path, "line": line, "character": char}
            )
            self.note_external("base", name, is_call=True, guessed=True)
            self.emit_occurrence(
                node, self.external_symbol_string(ExternalRef("base", name, True, True))
            )
            return
        # Bare unresolved identifier read (not a call, not local, not
        # top-level): most often a base-R constant or a name from a
        # `library()`-attached package we can't see from source alone.
        # Skip rather than guess -- low value, high noise.


def build_index(pkg_dir: str, positions_out: Optional[List[dict]] = None) -> scip.Index:
    """positions_out, if a list is passed in, is extended in place with
    every guessed-external call site across the package, in the shape
    ls_index.R expects: {"file", "line", "character"}."""
    package, version = read_description(pkg_dir)
    files = find_r_files(pkg_dir)
    top_level = collect_top_level_symbols(files, package, version)

    parser = Parser(R_LANGUAGE)
    index = scip.Index()
    index.metadata.tool_info.name = "scip-r"
    index.metadata.tool_info.version = "0.1.0"
    index.metadata.project_root = "file://" + os.path.abspath(pkg_dir)
    index.metadata.text_document_encoding = scip.TextEncoding.UTF8

    all_external: Dict[Tuple[str, str], ExternalRef] = {}

    for path in files:
        rel = os.path.relpath(path, pkg_dir)
        src = open(path, "rb").read()
        tree = parser.parse(src)
        di = DocumentIndexer(src, rel, package, version, top_level)
        counter = LocalCounter()
        for stmt in tree.root_node.children:
            di.walk(stmt, None, counter)
        index.documents.append(di.document)
        all_external.update(di.external_refs)
        if positions_out is not None:
            positions_out.extend(di.guessed_positions)

    for (pkg, name), ref in sorted(all_external.items()):
        info = index.external_symbols.add()
        info.symbol = symbol_string(
            "scip-r", pkg, ".", f"{name}()." if ref.is_call else f"{name}."
        )
        doc = f"{pkg}::{name}"
        if ref.guessed:
            doc += "  (unresolved call target; guessed to be base/attached R, not confirmed)"
        info.documentation.append(doc)
        info.kind = scip.SymbolInformation.Kind.Function

    return index
