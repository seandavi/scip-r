"""Static symbol extraction for R source, emitted as a SCIP index.

Design notes (see README for the full write-up):

- Parsing is done with tree-sitter-r. No R interpreter is invoked, so this
  works on any R source tree without installing the package or its
  dependencies.
- Symbol resolution is heuristic, not semantic. It knows about:
    * top-level assignments (``<-``, ``=``, ``<<-``, ``->``, ``->>``,
      chained, string-literal targets) -- package-level SCIP symbols
    * ``setClass``/``setRefClass``/``R6Class`` (classes), ``setGeneric``
      (generics), ``setMethod`` (S4 methods) and the members of R6 /
      reference classes (``Class#name``)
    * NAMESPACE: exported symbols are marked, ``importFrom`` names resolve
      to their package, ``S3method`` registrations link methods to generics
    * function parameters, ``for``-loop variables and in-body assignments
      -- SCIP "local" symbols, scoped to one function
    * ``pkg::fun`` / ``pkg:::fun`` -- references to external symbols; the
      ``:::`` form is recorded on the occurrence
    * bare calls to names that resolve to nothing above are treated as
      calls into "base" R and flagged as guesses; ``scip-r resolve`` can
      replace them with real answers.
- What it does NOT do: S3/S4/R6 dispatch at call sites, ``library()``-driven
  scope changes, NSE-aware argument matching, or version pinning of
  external symbols. Treat this as "go to a plausible definition".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict

from tree_sitter import Node, Parser

from . import scip_pb2 as scip
from .package import (
    R_LANGUAGE,
    NamespaceInfo,
    PackageInfo,
    S3Method,
    find_r_files,
    provenance,
    read_description,
    read_namespace,
)
from .symbols import (
    UNKNOWN_MANAGER,
    UNKNOWN_VERSION,
    class_descriptor,
    descriptor_for,
    member_descriptor,
    method_descriptor,
    symbol_string,
)

__all__ = [
    "GUESSED_NOTE",
    "GUESSED_PACKAGE",
    "DocumentIndexer",
    "GuessedPosition",
    "PackageInfo",
    "TopLevelSymbol",
    "build_index",
    "collect_top_level_symbols",
    "find_r_files",
    "read_description",
]

ASSIGN_OPS = frozenset({"<-", "="})
# ``<<-`` writes to an enclosing/global scope; we still treat the LHS name as
# a definition site the first time we see it.
ASSIGN_OPS_ALL = ASSIGN_OPS | {"<<-"}
RIGHT_ASSIGN_OPS = frozenset({"->", "->>"})

GUESSED_PACKAGE = "base"
GUESSED_NOTE = "(unresolved call target; guessed to be base/attached R, not confirmed)"
IMPORTED_NOTE = "(imported via NAMESPACE importFrom)"
S3_GENERIC_NOTE = "(generic of an S3 method registered in NAMESPACE; package guessed)"
EXPORTED_DOC = "@export"

# Calls that define classes, generics and methods. Recognised by name only
# (``methods::setClass`` works too); a package that shadows these names will
# get odd results, which is acceptable.
S4_CLASS_DEFINERS = frozenset({"setClass"})
RC_CLASS_DEFINERS = frozenset({"setRefClass"})
R6_CLASS_DEFINERS = frozenset({"R6Class"})
CLASS_DEFINERS = S4_CLASS_DEFINERS | RC_CLASS_DEFINERS | R6_CLASS_DEFINERS
GENERIC_DEFINERS = frozenset({"setGeneric"})
METHOD_DEFINERS = frozenset({"setMethod"})
R6_SECTIONS = ("public", "private", "active")
RC_SECTIONS = ("methods", "fields")
SELF_NAMES = frozenset({"self", "private", ".self"})

Range = tuple[int, ...]


class GuessedPosition(TypedDict):
    """A call site whose target scip-r could only guess.

    ``enclosing`` is the name of the top-level function the call sits in,
    or ``None`` for top-level code.
    """

    file: str
    line: int
    character: int
    name: str
    enclosing: str | None


def _node_range(node: Node) -> Range:
    """SCIP ranges are ``[start_line, start_col, end_line, end_col]``,
    0-based, with ``end_line`` omitted when it equals ``start_line``."""
    sr, sc = node.start_point
    er, ec = node.end_point
    if sr == er:
        return (sr, sc, ec)
    return (sr, sc, er, ec)


def _decode(src: bytes, node: Node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


# ---- symbol table ----------------------------------------------------------------


@dataclass
class TopLevelSymbol:
    name: str
    kind: str  # function, value, class, method, member_function, member_field
    signature: str
    file: Path
    range: Range
    disambiguator: str | None = None  # S4 method signature classes, joined by ","
    owner: str | None = None  # class name for member_* kinds
    generator: str | None = None  # class kind: variable holding an R6/RC generator
    contains: tuple[str, ...] = ()  # class kind: superclasses (S4 contains / R6 inherit)
    exported: bool | None = None  # None when the package has no NAMESPACE

    @property
    def is_function(self) -> bool:
        return self.kind in ("function", "method", "member_function")

    @property
    def descriptor(self) -> str:
        if self.kind == "class":
            return class_descriptor(self.name)
        if self.kind == "method":
            return method_descriptor(self.name, (self.disambiguator or "").split(","))
        if self.kind in ("member_function", "member_field"):
            return member_descriptor(
                self.owner or "", self.name, is_function=self.kind == "member_function"
            )
        return descriptor_for(self.name, is_function=self.is_function)

    @property
    def scip_kind(self) -> Any:
        k = scip.SymbolInformation.Kind
        return {
            "function": k.Function,
            "method": k.Method,
            "member_function": k.Method,
            "member_field": k.Field,
            "class": k.Class,
        }.get(self.kind, k.Variable)


def _symbol_key(
    kind: str, name: str, disambiguator: str | None = None, owner: str | None = None
) -> str:
    """Key into the top-level table. Plain names (functions and values) are
    keyed by name alone, since that is what call sites use; classes, S4
    methods and members live in separate R namespaces and get a prefix."""
    if kind == "class":
        return f"class:{name}"
    if kind == "method":
        return f"method:{name}({disambiguator or ''})"
    if kind in ("member_function", "member_field"):
        return f"member:{owner}#{name}"
    return name


@dataclass(frozen=True)
class ExternalRef:
    package: str
    name: str
    is_call: bool
    guessed: bool  # True if resolved by base-R fallback rather than pkg::fn
    via: str = "namespace_operator"  # namespace_operator | importFrom | guess | S3method
    descriptor_override: str | None = None  # for class references

    @property
    def symbol(self) -> str:
        desc = self.descriptor_override or descriptor_for(self.name, is_function=self.is_call)
        return symbol_string(self.package, UNKNOWN_VERSION, desc, manager=UNKNOWN_MANAGER)

    @property
    def documentation(self) -> str:
        doc = f"{self.package}::{self.name}"
        if self.via == "guess":
            return f"{doc}  {GUESSED_NOTE}"
        if self.via == "importFrom":
            return f"{doc}  {IMPORTED_NOTE}"
        if self.via == "S3method" and self.guessed:
            return f"{doc}  {S3_GENERIC_NOTE}"
        return doc


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


# ---- syntax helpers ---------------------------------------------------------------


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
        return _string_value(src, node)
    return None


def _assign_call_parts(src: bytes, node: Node) -> Assignment | None:
    """``assign("name", value)`` with a literal name (common for S3 methods
    on operators, e.g. ``assign("[.RGList", function(...) ...)``)."""
    if node.type != "call" or _call_name(src, node) != "assign":
        return None
    args = _call_args(src, node)
    named = {n: v for n, v in args if n is not None}
    positional = [v for n, v in args if n is None]
    target = named.get("x", positional[0] if positional else None)
    value = named.get("value", positional[1] if len(positional) > 1 else None)
    if target is None or value is None or target.type != "string":
        return None
    if any(n in ("envir", "pos") for n in named):
        return None  # assigning somewhere other than the namespace
    name = _string_value(src, target)
    if name is None:
        return None
    return Assignment(target=target, name=name, value=value)


def _assignment_parts(src: bytes, node: Node) -> Assignment | None:
    """If ``node`` is ``name <op> value`` (or ``value -> name``) with a
    simple name on the target side, or ``assign("name", value)``, describe
    it; else ``None``.

    Not handled, by design: destructuring targets such as ``obj$field <- v``,
    ``x[i] <- v`` or ``names(x) <- v``; those are walked as ordinary
    expressions so the reads inside them are still recorded.
    """
    if node.type == "call":
        return _assign_call_parts(src, node)
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
        out: list[str] = []
        for _name, value in _call_args(src, node):
            sv = _string_value(src, value)
            if sv is None:
                return None
            out.append(sv)
        return out
    return None


def _call_args(src: bytes, node: Node) -> list[tuple[str | None, Node]]:
    """``[(argument name or None, value node)]`` for a call."""
    args = node.child_by_field_name("arguments")
    out: list[tuple[str | None, Node]] = []
    if args is None:
        return out
    for a in args.children:
        if a.type != "argument":
            continue
        value = a.child_by_field_name("value")
        if value is None:
            continue
        name_node = a.child_by_field_name("name")
        out.append((_decode(src, name_node) if name_node is not None else None, value))
    return out


@dataclass(frozen=True)
class Member:
    """One entry of an R6 ``public``/``private``/``active`` list or an RC
    ``methods``/``fields`` list."""

    name: str
    name_node: Node
    value: Node
    is_function: bool
    argument: Node  # the ``name = value`` argument node (enclosing range)


@dataclass(frozen=True)
class DefinerCall:
    """A ``setClass("Foo", ...)`` / ``setGeneric`` / ``setMethod`` /
    ``setRefClass`` / ``R6Class`` call."""

    kind: str  # "class", "function" (generic) or "method"
    name: str
    name_node: Node
    signature: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    inherit: str | None = None  # R6: generator variable name
    members: tuple[Member, ...] = ()
    system: str = ""  # S4 | RC | R6 for classes

    @property
    def disambiguator(self) -> str | None:
        return ",".join(self.signature) if self.kind == "method" else None


def _members_of(src: bytes, section_value: Node, section: str) -> list[Member]:
    if section_value.type != "call" or _call_name(src, section_value) != "list":
        return []
    out: list[Member] = []
    args = section_value.child_by_field_name("arguments")
    if args is None:
        return out
    for a in args.children:
        if a.type != "argument":
            continue
        name_node = a.child_by_field_name("name")
        value = a.child_by_field_name("value")
        if name_node is None or value is None:
            continue
        name = _target_name(src, name_node)
        if name is None:
            continue
        out.append(
            Member(
                name=name,
                name_node=name_node,
                value=value,
                is_function=value.type == "function_definition",
                argument=a,
            )
        )
    return out


def _definer_call(src: bytes, node: Node) -> DefinerCall | None:
    if node.type != "call":
        return None
    callee = _call_name(src, node)
    if callee is None:
        return None
    args = _call_args(src, node)
    named = {n: v for n, v in args if n is not None}
    positional = [v for n, v in args if n is None]

    def arg(name: str, position: int) -> Node | None:
        if name in named:
            return named[name]
        return positional[position] if len(positional) > position else None

    if callee in CLASS_DEFINERS:
        system = (
            "S4" if callee in S4_CLASS_DEFINERS else "RC" if callee in RC_CLASS_DEFINERS else "R6"
        )
        name_node = arg("classname" if system == "R6" else "Class", 0)
        name = _string_value(src, name_node) if name_node is not None else None
        if name is None or name_node is None:
            return None
        contains: tuple[str, ...] = ()
        inherit: str | None = None
        if system == "S4" or system == "RC":
            c = named.get("contains")
            contains = tuple(_string_list(src, c) or ()) if c is not None else ()
        else:
            inh = named.get("inherit")
            if inh is not None and inh.type == "identifier":
                inherit = _decode(src, inh)
        members: list[Member] = []
        for section in R6_SECTIONS if system == "R6" else RC_SECTIONS if system == "RC" else ():
            if section in named:
                members.extend(_members_of(src, named[section], section))
        return DefinerCall(
            kind="class",
            name=name,
            name_node=name_node,
            contains=contains,
            inherit=inherit,
            members=tuple(members),
            system=system,
        )
    if callee in GENERIC_DEFINERS:
        name_node = arg("name", 0)
        name = _string_value(src, name_node) if name_node is not None else None
        if name is None or name_node is None:
            return None
        return DefinerCall(kind="function", name=name, name_node=name_node)
    if callee in METHOD_DEFINERS:
        name_node = arg("f", 0)
        name = _string_value(src, name_node) if name_node is not None else None
        if name is None or name_node is None:
            return None
        sig_node = arg("signature", 1)
        sig = _string_list(src, sig_node) if sig_node is not None else None
        if sig is None:
            return None
        return DefinerCall(kind="method", name=name, name_node=name_node, signature=tuple(sig))
    return None


# ---- pass 1 ---------------------------------------------------------------------------


def collect_top_level_symbols(
    files: list[Path], namespace: NamespaceInfo | None = None
) -> dict[str, TopLevelSymbol]:
    """Pass 1: find every definition at module top level, across all files,
    so cross-file references within the package resolve correctly.

    Covers assignments (chained ones define every name), ``setClass`` /
    ``setRefClass`` / ``R6Class`` classes and their members, ``setGeneric``
    and ``setMethod``. When a name is defined in more than one file the
    last file wins, mirroring R's "last source wins" collation behaviour.
    ``namespace`` (parsed NAMESPACE) marks exported symbols.
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
            signature = sig_line.decode("utf-8", "replace")
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
                    signature=signature,
                    file=path,
                    range=_node_range(definer.name_node),
                    disambiguator=definer.disambiguator,
                    generator=chain[0].name if chain and definer.kind == "class" else None,
                    contains=definer.contains,
                )
                if definer.kind == "class" and definer.inherit:
                    symbols[key].contains = (definer.inherit,)
                for m in definer.members:
                    mkind = "member_function" if m.is_function else "member_field"
                    mkey = _symbol_key(mkind, m.name, owner=definer.name)
                    symbols[mkey] = TopLevelSymbol(
                        name=m.name,
                        kind=mkind,
                        signature=_decode(src, m.argument).split("\n")[0][:120],
                        file=path,
                        range=_node_range(m.name_node),
                        owner=definer.name,
                    )
            if not chain:
                continue
            kind = "function" if node.type == "function_definition" else "value"
            for a in chain:
                symbols[a.name] = TopLevelSymbol(
                    name=a.name,
                    kind=kind,
                    signature=signature,
                    file=path,
                    range=_node_range(a.target),
                )
    if namespace is not None:
        for s3 in namespace.s3methods:
            alias = symbols.get(s3.method)
            if alias is not None and alias.kind == "value":
                alias.kind = "function"  # e.g. "dimnames<-.MAList" <- .setdimnames
        _mark_exports(symbols, namespace)
    return symbols


def _mark_exports(symbols: dict[str, TopLevelSymbol], ns: NamespaceInfo) -> None:
    generator_exported: dict[str, bool] = {}
    for sym in symbols.values():
        if sym.kind in ("function", "value"):
            sym.exported = ns.is_exported(sym.name)
            generator_exported[sym.name] = sym.exported
    for sym in symbols.values():
        if sym.kind == "class":
            sym.exported = (
                sym.name in ns.export_classes
                or ns.is_exported(sym.name)
                or bool(sym.generator and generator_exported.get(sym.generator))
            )
        elif sym.kind == "method":
            sym.exported = sym.name in ns.export_methods or ns.is_exported(sym.name)
    for sym in symbols.values():
        if sym.kind in ("member_function", "member_field"):
            owner = symbols.get(_symbol_key("class", sym.owner or ""))
            sym.exported = owner.exported if owner is not None else False


# ---- pass 2 ---------------------------------------------------------------------------


class DocumentIndexer:
    """Pass 2: walk one file's AST, emitting SCIP occurrences."""

    def __init__(
        self,
        src: bytes,
        relative_path: str,
        package: PackageInfo,
        top_level: dict[str, TopLevelSymbol],
        namespace: NamespaceInfo | None = None,
    ) -> None:
        self.src = src
        self.relative_path = relative_path
        self.package = package
        self.top_level = top_level
        self.namespace = namespace
        self.s3_methods: dict[str, S3Method] = (
            {m.method: m for m in namespace.s3methods} if namespace else {}
        )
        self.document = scip.Document(
            relative_path=relative_path,
            language="R",
            position_encoding=scip.PositionEncoding.UTF8CodeUnitOffsetFromLineStart,
        )
        self.external_refs: dict[str, ExternalRef] = {}
        self.symbols_emitted: set[str] = set()
        self.guessed_positions: list[GuessedPosition] = []
        self._counter = LocalCounter()
        self._enclosing: str | None = None
        self._stmt: Node | None = None
        self._class_ctx: str | None = None  # R6/RC class whose method is being walked

    # -- helpers -------------------------------------------------------------

    def text(self, node: Node) -> str:
        return _decode(self.src, node)

    def top_level_symbol_string(self, sym: TopLevelSymbol) -> str:
        return symbol_string(
            self.package.name, self.package.version, sym.descriptor, manager=self.package.manager
        )

    def emit_occurrence(
        self,
        node: Node,
        symbol: str,
        roles: int = 0,
        *,
        enclosing: Node | None = None,
        override_documentation: str | None = None,
    ) -> None:
        occ = self.document.occurrences.add()
        occ.range.extend(_node_range(node))
        occ.symbol = symbol
        occ.symbol_roles = roles
        if enclosing is not None:
            occ.enclosing_range.extend(_node_range(enclosing))
        if override_documentation:
            occ.override_documentation.append(override_documentation)

    def note_external(self, ref: ExternalRef) -> None:
        self.external_refs.setdefault(ref.symbol, ref)

    def _class_symbol(self, name: str) -> str | None:
        """Symbol of a class defined in this package, by class name or by
        the variable holding its generator."""
        sym = self.top_level.get(_symbol_key("class", name))
        if sym is None:
            sym = next(
                (s for s in self.top_level.values() if s.kind == "class" and s.generator == name),
                None,
            )
        return self.top_level_symbol_string(sym) if sym is not None else None

    def _add_relationship(self, info: scip.SymbolInformation, target: str) -> None:
        if any(r.symbol == target and r.is_implementation for r in info.relationships):
            return
        rel = info.relationships.add()
        rel.symbol = target
        rel.is_implementation = True

    def emit_top_level_definition(
        self, sym: TopLevelSymbol, name_node: Node, enclosing: Node | None = None
    ) -> None:
        symbol = self.top_level_symbol_string(sym)
        self.emit_occurrence(
            name_node, symbol, scip.SymbolRole.Definition, enclosing=enclosing or self._stmt
        )
        if symbol in self.symbols_emitted:
            return
        self.symbols_emitted.add(symbol)
        info = self.document.symbols.add()
        info.symbol = symbol
        info.documentation.append(sym.signature)
        if sym.exported:
            info.documentation.append(EXPORTED_DOC)
        info.kind = sym.scip_kind
        if sym.kind == "method":
            # An S4 method implements its generic. When the generic is
            # defined in this package we can say so statically; for
            # external generics `scip-r resolve` fills the relationship in.
            generic = self.top_level.get(sym.name)
            if generic is not None and generic.is_function:
                self._add_relationship(info, self.top_level_symbol_string(generic))
        elif sym.kind == "class":
            for parent in sym.contains:
                target = self._class_symbol(parent)
                if target is not None:
                    self._add_relationship(info, target)
        elif sym.kind == "function" and sym.name in self.s3_methods:
            m = self.s3_methods[sym.name]
            generic = self.top_level.get(m.generic)
            if generic is not None and generic.is_function:
                self._add_relationship(info, self.top_level_symbol_string(generic))
            else:
                ref = ExternalRef(
                    m.generic_package or GUESSED_PACKAGE,
                    m.generic,
                    is_call=True,
                    guessed=m.generic_package is None,
                    via="S3method",
                )
                self.note_external(ref)
                self._add_relationship(info, ref.symbol)

    def index(self, root: Node) -> None:
        """Walk every top-level statement of a parsed file."""
        for stmt in root.children:
            self._stmt = stmt
            self.walk(stmt, None)
        self._stmt = None

    # -- walking -------------------------------------------------------------

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
            parts = _assign_call_parts(self.src, node)
            if parts is not None:
                # assign("name", value): record the definition, then walk the
                # call normally so `assign` itself is still a reference.
                self._handle_assignment(parts, scope)
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
            # ``sapply(x, stats::median)``.
            self._handle_namespace_ref(node, is_call=False)
            return

        if t == "extract_operator":
            self._walk_extract(node, scope)
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
        op = node.child_by_field_name("operator")
        if pkg_node is None or name_node is None:
            return
        ref = ExternalRef(
            self.text(pkg_node), self.text(name_node), is_call=is_call, guessed=False
        )
        self.note_external(ref)
        internal = op is not None and op.type == ":::"
        self.emit_occurrence(
            name_node,
            ref.symbol,
            override_documentation=f"{ref.package}:::{ref.name}" if internal else None,
        )

    def _walk_extract(self, node: Node, scope: Scope | None) -> None:
        """``x$name`` / ``x@slot``: ``x`` is a read; ``name`` is a field label,
        not a variable, except ``self$name`` / ``private$name`` inside an R6
        or reference-class method, which resolves to the class member."""
        lhs = node.child_by_field_name("lhs")
        rhs = node.child_by_field_name("rhs")
        if lhs is not None:
            if (
                lhs.type == "identifier"
                and self._class_ctx is not None
                and self.text(lhs) in SELF_NAMES
                and rhs is not None
                and rhs.type == "identifier"
            ):
                name = self.text(rhs)
                for kind in ("member_function", "member_field"):
                    sym = self.top_level.get(_symbol_key(kind, name, owner=self._class_ctx))
                    if sym is not None:
                        self.emit_occurrence(rhs, self.top_level_symbol_string(sym))
                        break
                return
            self.walk(lhs, scope)

    def _walk_call(self, node: Node, scope: Scope | None) -> None:
        if scope is None:
            definer = _definer_call(self.src, node)
            if definer is not None:
                self._walk_definer(node, definer, scope)
                return
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

    def _walk_definer(self, node: Node, definer: DefinerCall, scope: Scope | None) -> None:
        key = _symbol_key(definer.kind, definer.name, definer.disambiguator)
        sym = self.top_level.get(key)
        if sym is not None:
            self.emit_top_level_definition(sym, definer.name_node)
        fn = node.child_by_field_name("function")
        if fn is not None:
            if fn.type == "identifier":
                self.handle_name_use(fn, scope, is_call=True)
            elif fn.type == "namespace_operator":
                self._handle_namespace_ref(fn, is_call=True)
        member_args = {m.argument.id: m for m in definer.members}
        args = node.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.children:
            if a.type != "argument":
                continue
            value = a.child_by_field_name("value")
            if value is None:
                continue
            if definer.members and any(
                m.argument.parent is not None
                and m.argument.parent.parent is not None
                and m.argument.parent.parent.id == value.id
                for m in definer.members
            ):
                # a public/private/active/methods/fields list: emit members
                self._walk_member_list(value, definer, member_args, scope)
                continue
            self.walk(value, scope)

    def _walk_member_list(
        self,
        list_call: Node,
        definer: DefinerCall,
        members: dict[int, Member],
        scope: Scope | None,
    ) -> None:
        fn = list_call.child_by_field_name("function")
        if fn is not None and fn.type == "identifier":
            self.handle_name_use(fn, scope, is_call=True)
        args = list_call.child_by_field_name("arguments")
        if args is None:
            return
        for a in args.children:
            if a.type != "argument":
                continue
            m = members.get(a.id)
            value = a.child_by_field_name("value")
            if m is None:
                if value is not None:
                    self.walk(value, scope)
                continue
            kind = "member_function" if m.is_function else "member_field"
            sym = self.top_level.get(_symbol_key(kind, m.name, owner=definer.name))
            if sym is not None:
                self.emit_top_level_definition(sym, m.name_node, enclosing=a)
            saved_ctx, saved_enclosing = self._class_ctx, self._enclosing
            self._class_ctx = definer.name
            if m.is_function:
                self._enclosing = f"{definer.name}${m.name}"
            self.walk(m.value, scope)
            self._class_ctx, self._enclosing = saved_ctx, saved_enclosing

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
        if self.namespace is not None and name in self.namespace.imports:
            ref = ExternalRef(
                self.namespace.imports[name],
                name,
                is_call=is_call,
                guessed=False,
                via="importFrom",
            )
            self.note_external(ref)
            self.emit_occurrence(node, ref.symbol)
            return
        if is_call:
            # Unresolved call target: guess it's base/attached R. Flagged as a
            # guess in the emitted documentation, not asserted as fact, and
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
            ref = ExternalRef(GUESSED_PACKAGE, name, is_call=True, guessed=True, via="guess")
            self.note_external(ref)
            self.emit_occurrence(node, ref.symbol)
            return
        # Bare unresolved identifier read (not a call, not local, not
        # top-level): most often a base-R constant or a name from a
        # ``library()``-attached package we can't see from source alone.
        # Skip rather than guess -- low value, high noise.


# ---- driver ---------------------------------------------------------------------------


def build_index(
    pkg_dir: Path | str,
    *,
    positions_out: list[GuessedPosition] | None = None,
    tool_version: str | None = None,
    manager: str | None = None,
    extra_arguments: dict[str, str] | None = None,
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
        manager: override the inferred package manager (``cran``,
            ``bioconductor``, ...), used in this package's symbols.
        extra_arguments: additional ``key=value`` provenance stamps for
            ``metadata.tool_info.arguments`` (e.g. a tarball digest).

    Raises:
        NotADirectoryError: if ``pkg_dir`` is not a directory.
    """
    from . import __version__

    pkg_dir = Path(pkg_dir)
    if not pkg_dir.is_dir():
        raise NotADirectoryError(f"not a directory: {pkg_dir}")

    package = read_description(pkg_dir, manager=manager)
    namespace = read_namespace(pkg_dir)
    files = find_r_files(pkg_dir, package.collate)
    top_level = collect_top_level_symbols(files, namespace)

    parser = Parser(R_LANGUAGE)
    index = scip.Index()
    index.metadata.version = scip.ProtocolVersion.UnspecifiedProtocolVersion
    index.metadata.tool_info.name = "scip-r"
    index.metadata.tool_info.version = tool_version or __version__
    index.metadata.project_root = pkg_dir.resolve().as_uri()
    index.metadata.text_document_encoding = scip.TextEncoding.UTF8
    stamps = {"package": package.name, "version": package.version, "manager": package.manager}
    stamps.update(provenance(pkg_dir, package.fields))
    stamps.update(extra_arguments or {})
    index.metadata.tool_info.arguments.extend(f"{k}={v}" for k, v in stamps.items())

    all_external: dict[str, ExternalRef] = {}

    for path in files:
        rel = path.relative_to(pkg_dir).as_posix()
        src = path.read_bytes()
        tree = parser.parse(src)
        di = DocumentIndexer(src, rel, package, top_level, namespace)
        di.index(tree.root_node)
        index.documents.append(di.document)
        all_external.update(di.external_refs)
        if positions_out is not None:
            positions_out.extend(di.guessed_positions)

    for _symbol, ref in sorted(all_external.items()):
        info = index.external_symbols.add()
        info.symbol = ref.symbol
        info.documentation.append(ref.documentation)
        # A called name is a function. A ``pkg::name`` used as a value is
        # usually a function too, but source alone can't say.
        info.kind = (
            scip.SymbolInformation.Kind.Function
            if ref.is_call
            else scip.SymbolInformation.Kind.UnspecifiedKind
        )

    return index


def index_arguments(index: scip.Index) -> dict[str, str]:
    """``key=value`` stamps from ``metadata.tool_info.arguments`` as a dict."""
    out: dict[str, str] = {}
    for a in index.metadata.tool_info.arguments:
        if "=" in a:
            k, v = a.split("=", 1)
            out[k] = v
    return out
