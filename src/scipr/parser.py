"""Static symbol extraction for R source, emitted as a SCIP index.

Design notes (see README for the full write-up):

- Parsing is done with tree-sitter-r. No R interpreter is invoked, so this
  works on any R source tree without installing the package or its
  dependencies.
- Symbol resolution is heuristic, not semantic. It knows about:
    * top-level ``name <- value`` / ``name = value`` assignments (functions
      and plain objects) -- these become package-level SCIP symbols
    * function parameters, ``for``-loop variables and in-body assignments
      -- these become SCIP "local" symbols, scoped to one function
    * ``pkg::fun(...)`` / ``pkg:::fun(...)`` calls -- these become
      references to external symbols (one synthetic SymbolInformation per
      external name, so consumers get *something* to show even if the
      target package was never indexed)
    * bare calls to names that resolve to neither a local nor a package
      symbol are treated as calls into "base" R and recorded the same way
      external calls are -- this is a guess, not a fact (R's actual lookup
      also depends on the search path and can be shadowed), and is flagged
      as such in the emitted documentation string.
- What it does NOT do: S3/S4/R6 method dispatch, ``library()``-driven scope
  changes, NSE-aware argument matching, or cross-package version pinning.
  A tree-sitter pass fundamentally can't resolve dispatch -- that needs
  running R. Treat this as "go to a plausible definition", not a compiler.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

import tree_sitter_r as tsr
from tree_sitter import Language, Node, Parser

from . import scip_pb2 as scip
from .symbols import (
    UNKNOWN_VERSION,
    class_descriptor,
    descriptor_for,
    method_descriptor,
    symbol_string,
)

R_LANGUAGE = Language(tsr.language())

ASSIGN_OPS = frozenset({"<-", "="})
# ``<<-`` writes to an enclosing/global scope; we still treat the LHS name as
# a definition site the first time we see it, since tracking the *actual*
# enclosing scope would require full lexical analysis.
ASSIGN_OPS_ALL = ASSIGN_OPS | {"<<-"}
# ``value -> name`` / ``value ->> name``: same thing with the sides swapped.
RIGHT_ASSIGN_OPS = frozenset({"->", "->>"})

R_SOURCE_SUFFIXES = frozenset({".R", ".r"})

GUESSED_PACKAGE = "base"

# Calls that define S4 / reference / R6 classes, generics and methods. They
# are recognised by name only (``methods::setClass`` works too); a package
# that shadows these names will get odd results, which is acceptable.
CLASS_DEFINERS = frozenset({"setClass", "setRefClass", "R6Class"})
GENERIC_DEFINERS = frozenset({"setGeneric"})
METHOD_DEFINERS = frozenset({"setMethod"})
GUESSED_NOTE = "(unresolved call target; guessed to be base/attached R, not confirmed)"

Range = tuple[int, ...]


class GuessedPosition(TypedDict):
    """A call site whose target scip-r could only guess.

    ``enclosing`` is the name of the top-level function the call sits in,
    or ``None`` for top-level code; ``scip-r resolve`` uses ``name`` and
    ``enclosing`` to join R-side namespace lookups back onto occurrences.
    """

    file: str
    line: int
    character: int
    name: str
    enclosing: str | None


@dataclass(frozen=True)
class PackageInfo:
    name: str
    version: str


def read_description(pkg_dir: Path | str) -> PackageInfo:
    """Best-effort package name/version from a DESCRIPTION file.

    Falls back to the directory name and ``0.0.0`` when the file is absent
    or the fields are missing.
    """
    pkg_dir = Path(pkg_dir)
    name, version = pkg_dir.resolve().name, "0.0.0"
    desc_path = pkg_dir / "DESCRIPTION"
    if desc_path.is_file():
        text = desc_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^Package:\s*(\S+)", text, re.MULTILINE)
        if m:
            name = m.group(1)
        m = re.search(r"^Version:\s*(\S+)", text, re.MULTILINE)
        if m:
            version = m.group(1)
    return PackageInfo(name=name, version=version)


def find_r_files(pkg_dir: Path | str) -> list[Path]:
    """All ``.R``/``.r`` files under ``<pkg>/R/`` (recursively), sorted.

    If there is no ``R/`` directory the whole tree is searched instead, so
    a loose directory of scripts can still be indexed.
    """
    pkg_dir = Path(pkg_dir)
    r_dir = pkg_dir / "R"
    root = r_dir if r_dir.is_dir() else pkg_dir
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in R_SOURCE_SUFFIXES)


def _node_range(node: Node) -> Range:
    """SCIP ranges are ``[start_line, start_col, end_line, end_col]``,
    0-based, with ``end_line`` omitted when it equals ``start_line``
    (3-element form)."""
    sr, sc = node.start_point
    er, ec = node.end_point
    if sr == er:
        return (sr, sc, ec)
    return (sr, sc, er, ec)


@dataclass
class TopLevelSymbol:
    name: str
    kind: str  # "function", "value", "class" or "method"
    signature: str
    file: Path
    range: Range
    disambiguator: str | None = None  # S4 method signature classes, joined by ","

    @property
    def is_function(self) -> bool:
        return self.kind in ("function", "method")

    @property
    def descriptor(self) -> str:
        if self.kind == "class":
            return class_descriptor(self.name)
        if self.kind == "method":
            return method_descriptor(self.name, (self.disambiguator or "").split(","))
        return descriptor_for(self.name, is_function=self.is_function)

    @property
    def scip_kind(self) -> Any:
        k = scip.SymbolInformation.Kind
        return {
            "function": k.Function,
            "method": k.Method,
            "class": k.Class,
        }.get(self.kind, k.Variable)


@dataclass(frozen=True)
class ExternalRef:
    package: str
    name: str
    is_call: bool
    guessed: bool  # True if resolved by base-R fallback rather than pkg::fn

    @property
    def symbol(self) -> str:
        return symbol_string(
            self.package, UNKNOWN_VERSION, descriptor_for(self.name, is_function=self.is_call)
        )


class LocalCounter:
    """Per-document counter backing SCIP ``local N`` symbol ids."""

    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        n = self._n
        self._n += 1
        return n


@dataclass
class Scope:
    """One function's local names -> SCIP local symbol id."""

    counter: LocalCounter
    names: dict[str, str] = field(default_factory=dict)

    def define(self, name: str) -> str:
        if name in self.names:
            return self.names[name]
        sym = f"local {self.counter.next()}"
        self.names[name] = sym
        return sym

    def get(self, name: str) -> str | None:
        return self.names.get(name)


def _decode(src: bytes, node: Node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


@dataclass(frozen=True)
class Assignment:
    target: Node  # the node the definition occurrence is attached to
    name: str
    value: Node


def _target_name(src: bytes, node: Node) -> str | None:
    """Name bound by an assignment target: a bare identifier (backticked
    names parse as identifiers too), or a string literal, which R accepts
    for operator and replacement-function definitions such as
    ``"%+%" <- function(a, b) ...`` and ``"foo<-" <- function(x, value) ...``.
    """
    if node.type == "identifier":
        return _decode(src, node)
    if node.type == "string":
        content = next((c for c in node.children if c.type == "string_content"), None)
        return _decode(src, content) if content is not None else None
    return None


def _call_name(src: bytes, node: Node) -> str | None:
    """Callee name of a ``call`` node when it is ``f(...)`` or ``pkg::f(...)``."""
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "identifier":
        return _decode(src, fn)
    if fn.type == "namespace_operator":
        rhs = fn.child_by_field_name("rhs")
        return _decode(src, rhs) if rhs is not None else None
    return None


def _string_value(src: bytes, node: Node) -> str | None:
    if node.type != "string":
        return None
    content = next((c for c in node.children if c.type == "string_content"), None)
    return _decode(src, content) if content is not None else ""


def _string_list(src: bytes, node: Node) -> list[str] | None:
    """Strings in ``"A"``, ``c("A", "B")`` or ``signature("A", x = "B")``."""
    single = _string_value(src, node)
    if single is not None:
        return [single]
    if node.type == "call" and _call_name(src, node) in ("c", "signature"):
        args = node.child_by_field_name("arguments")
        if args is None:
            return None
        out: list[str] = []
        for a in args.children:
            if a.type != "argument":
                continue
            value = a.child_by_field_name("value")
            sv = _string_value(src, value) if value is not None else None
            if sv is None:
                return None
            out.append(sv)
        return out
    return None


@dataclass(frozen=True)
class DefinerCall:
    """A ``setClass("Foo", ...)`` / ``setGeneric`` / ``setMethod`` / ``R6Class`` call."""

    kind: str  # "class", "function" (generic) or "method"
    name: str
    name_node: Node
    signature: tuple[str, ...] = ()

    @property
    def disambiguator(self) -> str | None:
        return ",".join(self.signature) if self.kind == "method" else None


def _definer_call(src: bytes, node: Node) -> DefinerCall | None:
    if node.type != "call":
        return None
    callee = _call_name(src, node)
    if callee is None:
        return None
    if callee in CLASS_DEFINERS:
        kind = "class"
    elif callee in GENERIC_DEFINERS:
        kind = "function"
    elif callee in METHOD_DEFINERS:
        kind = "method"
    else:
        return None
    args = node.child_by_field_name("arguments")
    if args is None:
        return None
    positional = [a for a in args.children if a.type == "argument"]
    if not positional:
        return None
    first = positional[0].child_by_field_name("value")
    if first is None:
        return None
    name = _string_value(src, first)
    if name is None:
        return None
    signature: tuple[str, ...] = ()
    if kind == "method":
        sig_node = positional[1].child_by_field_name("value") if len(positional) > 1 else None
        sig = _string_list(src, sig_node) if sig_node is not None else None
        if sig is None:
            return None
        signature = tuple(sig)
    return DefinerCall(kind=kind, name=name, name_node=first, signature=signature)


def _assignment_parts(src: bytes, node: Node) -> Assignment | None:
    """If ``node`` is ``name <op> value`` (or ``value -> name``) with a
    simple name on the target side, describe it; else ``None``.

    Not handled, by design: destructuring targets such as ``obj$field <- v``,
    ``x[i] <- v`` or ``names(x) <- v``; those are walked as ordinary
    expressions so the reads inside them are still recorded.
    """
    if node.type != "binary_operator":
        return None
    op = node.child_by_field_name("operator")
    lhs = node.child_by_field_name("lhs")
    rhs = node.child_by_field_name("rhs")
    if op is None or lhs is None or rhs is None:
        return None
    if op.type in ASSIGN_OPS_ALL:
        target, value = lhs, rhs
    elif op.type in RIGHT_ASSIGN_OPS:
        target, value = rhs, lhs
    else:
        return None
    name = _target_name(src, target)
    if name is None:
        return None
    return Assignment(target=target, name=name, value=value)


def _symbol_key(kind: str, name: str, disambiguator: str | None = None) -> str:
    """Key into the top-level table. Plain names (functions and values) are
    keyed by name alone, since that is what call sites use; classes and S4
    methods live in separate namespaces in R and are keyed with a prefix."""
    if kind == "class":
        return f"class:{name}"
    if kind == "method":
        return f"method:{name}({disambiguator or ''})"
    return name


def collect_top_level_symbols(files: list[Path]) -> dict[str, TopLevelSymbol]:
    """Pass 1: find every ``name <- value`` at module top level, across all
    files, so cross-file calls within the package resolve correctly. Also
    registers ``setClass``/``setRefClass``/``R6Class`` (classes),
    ``setGeneric`` (functions) and ``setMethod`` (methods) calls.

    When a name is defined in more than one file the last file (in sorted
    order) wins, mirroring R's "last source wins" collation behaviour.
    Chained assignments (``a <- b <- value``) define every name in the chain.
    """
    parser = Parser(R_LANGUAGE)
    symbols: dict[str, TopLevelSymbol] = {}
    for path in files:
        src = path.read_bytes()
        tree = parser.parse(src)
        for stmt in tree.root_node.children:
            sig_line = src[stmt.start_byte : stmt.end_byte].split(b"\n")[0]
            if len(sig_line) > 120:
                sig_line = sig_line[:117] + b"..."
            chain: list[Assignment] = []
            node = stmt
            while (parts := _assignment_parts(src, node)) is not None:
                chain.append(parts)
                node = parts.value
            definer = _definer_call(src, node)
            if definer is not None:
                key = _symbol_key(definer.kind, definer.name, definer.disambiguator)
                symbols[key] = TopLevelSymbol(
                    name=definer.name,
                    kind=definer.kind,
                    signature=sig_line.decode("utf-8", "replace"),
                    file=path,
                    range=_node_range(definer.name_node),
                    disambiguator=definer.disambiguator,
                )
            if not chain:
                continue
            kind = "function" if node.type == "function_definition" else "value"
            for a in chain:
                symbols[a.name] = TopLevelSymbol(
                    name=a.name,
                    kind=kind,
                    signature=sig_line.decode("utf-8", "replace"),
                    file=path,
                    range=_node_range(a.target),
                )
    return symbols


class DocumentIndexer:
    """Pass 2: walk one file's AST, emitting SCIP occurrences."""

    def __init__(
        self,
        src: bytes,
        relative_path: str,
        package: PackageInfo,
        top_level: dict[str, TopLevelSymbol],
    ) -> None:
        self.src = src
        self.relative_path = relative_path
        self.package = package
        self.top_level = top_level
        self.document = scip.Document(relative_path=relative_path, language="R")
        self.external_refs: dict[str, ExternalRef] = {}
        self.symbols_emitted: set[str] = set()
        self.guessed_positions: list[GuessedPosition] = []
        self._counter = LocalCounter()
        self._enclosing: str | None = None

    def text(self, node: Node) -> str:
        return _decode(self.src, node)

    def top_level_symbol_string(self, sym: TopLevelSymbol) -> str:
        return symbol_string(self.package.name, self.package.version, sym.descriptor)

    def emit_occurrence(self, node: Node, symbol: str, roles: int = 0) -> None:
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
            info.kind = sym.scip_kind
            if sym.kind == "method":
                # An S4 method implements its generic. When the generic is
                # defined in this package we can say so statically; for
                # external generics `scip-r resolve` fills the relationship in.
                generic = self.top_level.get(sym.name)
                if generic is not None and generic.is_function:
                    rel = info.relationships.add()
                    rel.symbol = self.top_level_symbol_string(generic)
                    rel.is_implementation = True

    def note_external(self, ref: ExternalRef) -> None:
        self.external_refs.setdefault(ref.symbol, ref)

    def index(self, root: Node) -> None:
        """Walk every top-level statement of a parsed file."""
        for stmt in root.children:
            self.walk(stmt, None)

    def walk(self, node: Node, scope: Scope | None) -> None:
        t = node.type

        if t == "function_definition":
            self._walk_function(node, scope)
            return

        if t == "binary_operator":
            parts = _assignment_parts(self.src, node)
            if parts is not None:
                self._handle_assignment(parts, scope)
                if scope is None and parts.value.type == "function_definition":
                    self._enclosing = parts.name
                    self.walk(parts.value, scope)
                    self._enclosing = None
                else:
                    self.walk(parts.value, scope)
                return
            # fall through to generic recursion for non-assignment binary ops

        if t == "call":
            self._walk_call(node, scope)
            return

        if t == "argument":
            # ``f(name = value)``: the ``name`` is a formal-argument label,
            # not a variable read, so only the value is walked.
            value = node.child_by_field_name("value")
            if value is not None:
                self.walk(value, scope)
            return

        if t == "namespace_operator":
            # ``pkg::name`` used as a value rather than called, e.g.
            # ``sapply(x, stats::median)``. Recorded as a non-call external
            # reference (``name.`` descriptor).
            self._handle_namespace_ref(node, is_call=False)
            return

        if t == "for_statement":
            self._walk_for(node, scope)
            return

        if t == "identifier":
            self.handle_name_use(node, scope, is_call=False)
            return

        for child in node.children:
            self.walk(child, scope)

    def _walk_function(self, node: Node, scope: Scope | None) -> None:
        inner = Scope(self._counter)
        params = node.child_by_field_name("parameters")
        if params is not None:
            for p in params.children:
                if p.type != "parameter":
                    continue
                name_node = p.child_by_field_name("name")
                if name_node is not None and name_node.type == "identifier":
                    local_sym = inner.define(self.text(name_node))
                    self.emit_occurrence(name_node, local_sym, scip.SymbolRole.Definition)
                default = p.child_by_field_name("default")
                if default is not None:
                    # Defaults are evaluated lazily in the function's own
                    # frame in R, so earlier parameters are visible to them.
                    self.walk(default, inner)
        body = node.child_by_field_name("body")
        if body is not None:
            self.walk(body, inner)

    def _handle_assignment(self, a: Assignment, scope: Scope | None) -> None:
        if scope is None:
            if a.name in self.top_level:
                self.emit_top_level_definition(self.top_level[a.name], a.target)
            return
        existing = scope.get(a.name)
        if existing is None:
            self.emit_occurrence(a.target, scope.define(a.name), scip.SymbolRole.Definition)
        else:
            self.emit_occurrence(a.target, existing, 0)

    def _handle_namespace_ref(self, node: Node, *, is_call: bool) -> None:
        pkg_node = node.child_by_field_name("lhs")
        name_node = node.child_by_field_name("rhs")
        if pkg_node is None or name_node is None:
            return
        ref = ExternalRef(
            self.text(pkg_node), self.text(name_node), is_call=is_call, guessed=False
        )
        self.note_external(ref)
        self.emit_occurrence(name_node, ref.symbol)

    def _walk_call(self, node: Node, scope: Scope | None) -> None:
        if scope is None:
            definer = _definer_call(self.src, node)
            if definer is not None:
                key = _symbol_key(definer.kind, definer.name, definer.disambiguator)
                sym = self.top_level.get(key)
                if sym is not None:
                    self.emit_top_level_definition(sym, definer.name_node)
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None:
            if fn.type == "identifier":
                self.handle_name_use(fn, scope, is_call=True)
            elif fn.type == "namespace_operator":
                self._handle_namespace_ref(fn, is_call=True)
            else:
                self.walk(fn, scope)
        if args is not None:
            self.walk(args, scope)

    def _walk_for(self, node: Node, scope: Scope | None) -> None:
        # ``for (var in seq) body``: the grammar exposes ``body`` as a field
        # but not ``var``/``seq``, so pick them positionally.
        body = node.child_by_field_name("body")
        var_seen = False
        for child in node.children:
            if child is body:
                continue
            if child.type == "identifier" and not var_seen:
                var_seen = True
                if scope is not None:
                    existing = scope.get(self.text(child))
                    if existing is None:
                        self.emit_occurrence(
                            child, scope.define(self.text(child)), scip.SymbolRole.Definition
                        )
                    else:
                        self.emit_occurrence(child, existing, 0)
                continue
            self.walk(child, scope)
        if body is not None:
            self.walk(body, scope)

    def handle_name_use(self, node: Node, scope: Scope | None, *, is_call: bool) -> None:
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
            # recorded with its name and enclosing function so a second pass
            # (`scip-r resolve`, or any other consumer) can replace the guess.
            line, char = node.start_point
            self.guessed_positions.append(
                {
                    "file": self.relative_path,
                    "line": line,
                    "character": char,
                    "name": name,
                    "enclosing": self._enclosing,
                }
            )
            ref = ExternalRef(GUESSED_PACKAGE, name, is_call=True, guessed=True)
            self.note_external(ref)
            self.emit_occurrence(node, ref.symbol)
            return
        # Bare unresolved identifier read (not a call, not local, not
        # top-level): most often a base-R constant or a name from a
        # ``library()``-attached package we can't see from source alone.
        # Skip rather than guess -- low value, high noise.


def build_index(
    pkg_dir: Path | str,
    *,
    positions_out: list[GuessedPosition] | None = None,
    tool_version: str | None = None,
) -> scip.Index:
    """Index an R package directory and return a SCIP ``Index`` message.

    Args:
        pkg_dir: package root (contains ``DESCRIPTION`` and ``R/``). A bare
            directory of ``.R`` files also works; name/version then fall
            back to the directory name and ``0.0.0``.
        positions_out: if a list is passed, it is extended in place with
            every guessed-external call site across the package as
            ``{"file", "line", "character", "name", "enclosing"}``.
        tool_version: recorded in ``metadata.tool_info.version``; defaults
            to scip-r's own version.

    Raises:
        NotADirectoryError: if ``pkg_dir`` is not a directory.
    """
    from . import __version__

    pkg_dir = Path(pkg_dir)
    if not pkg_dir.is_dir():
        raise NotADirectoryError(f"not a directory: {pkg_dir}")

    package = read_description(pkg_dir)
    files = find_r_files(pkg_dir)
    top_level = collect_top_level_symbols(files)

    parser = Parser(R_LANGUAGE)
    index = scip.Index()
    index.metadata.version = scip.ProtocolVersion.UnspecifiedProtocolVersion
    index.metadata.tool_info.name = "scip-r"
    index.metadata.tool_info.version = tool_version or __version__
    index.metadata.project_root = pkg_dir.resolve().as_uri()
    index.metadata.text_document_encoding = scip.TextEncoding.UTF8

    all_external: dict[str, ExternalRef] = {}

    for path in files:
        rel = path.relative_to(pkg_dir).as_posix()
        src = path.read_bytes()
        tree = parser.parse(src)
        di = DocumentIndexer(src, rel, package, top_level)
        di.index(tree.root_node)
        index.documents.append(di.document)
        all_external.update(di.external_refs)
        if positions_out is not None:
            positions_out.extend(di.guessed_positions)

    for _symbol, ref in sorted(all_external.items()):
        info = index.external_symbols.add()
        info.symbol = ref.symbol
        doc = f"{ref.package}::{ref.name}"
        if ref.guessed:
            doc += f"  {GUESSED_NOTE}"
        info.documentation.append(doc)
        # A called name is a function. A ``pkg::name`` used as a value is
        # usually a function too, but source alone can't say.
        info.kind = (
            scip.SymbolInformation.Kind.Function
            if ref.is_call
            else scip.SymbolInformation.Kind.UnspecifiedKind
        )

    return index
