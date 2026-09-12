"""Helpers for scip-r's symbol-string convention.

SCIP treats the symbol string as opaque. scip-r uses this shape for
package-level and external symbols::

    scip-r cran <package> <version> <descriptor>

where ``<descriptor>`` is one of

- ``name().`` for functions,
- ``name(Sig).`` for S4 methods, with the signature classes joined by ``,``
  as the disambiguator (``width(Interval).``, ``show(A,B).``),
- ``name#`` for classes (S4, reference and R6),
- ``name.`` for other top-level objects,

and ``<version>`` is ``.`` when unknown (external references resolved from
source alone). Locals use SCIP's own ``local N`` convention and are only
meaningful within one document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SCHEME = "scip-r"
MANAGER = "cran"
UNKNOWN_VERSION = "."


@dataclass(frozen=True)
class ParsedSymbol:
    """Structured view of a scip-r symbol string."""

    raw: str
    is_local: bool
    scheme: str | None = None
    manager: str | None = None
    package: str | None = None
    version: str | None = None
    descriptor: str | None = None

    def _split(self) -> tuple[str, str | None, str] | None:
        """``(name, disambiguator, suffix)`` or None for locals/unknown."""
        if self.descriptor is None:
            return None
        m = _DESCRIPTOR_RE.match(self.descriptor)
        if m is None:
            return self.descriptor, None, ""
        if m.group("suffix") is not None:  # name(...). form
            return m.group("name"), m.group("disamb") or None, ")."
        return m.group("name"), None, m.group("suffix2")

    @property
    def name(self) -> str | None:
        """Bare name with the descriptor suffix and disambiguator stripped."""
        parts = self._split()
        return None if parts is None else parts[0]

    @property
    def disambiguator(self) -> str | None:
        """S4 method signature (``Interval`` in ``width(Interval).``), else None."""
        parts = self._split()
        return None if parts is None else parts[1]

    @property
    def is_function(self) -> bool:
        parts = self._split()
        return parts is not None and parts[2] == ")."

    @property
    def is_method(self) -> bool:
        return self.is_function and self.disambiguator is not None

    @property
    def is_class(self) -> bool:
        parts = self._split()
        return parts is not None and parts[2] == "#"

    @property
    def is_external(self) -> bool:
        """True for references whose defining package was not indexed."""
        return not self.is_local and self.version == UNKNOWN_VERSION


_DESCRIPTOR_RE = re.compile(
    r"^(?P<name>.*?)(?:\((?P<disamb>[^()]*)\)(?P<suffix>\.)|(?P<suffix2>[.#]))$"
)


def symbol_string(package: str, version: str, descriptor: str, *, scheme: str = SCHEME) -> str:
    """Build a package-level symbol string in scip-r's convention."""
    return f"{scheme} {MANAGER} {package} {version} {descriptor}"


def descriptor_for(name: str, *, is_function: bool) -> str:
    return f"{name}()." if is_function else f"{name}."


def method_descriptor(generic: str, signature: list[str] | tuple[str, ...]) -> str:
    """Descriptor for an S4 method: ``generic(Class1,Class2).``"""
    return f"{generic}({','.join(signature)})."


def class_descriptor(name: str) -> str:
    return f"{name}#"


def parse_symbol(symbol: str) -> ParsedSymbol:
    """Parse a symbol string. Never raises; unknown shapes come back with
    only ``raw`` and ``is_local`` populated."""
    if symbol.startswith("local "):
        return ParsedSymbol(raw=symbol, is_local=True)
    parts = symbol.split(" ", 4)
    if len(parts) != 5:
        return ParsedSymbol(raw=symbol, is_local=False)
    scheme, manager, package, version, descriptor = parts
    return ParsedSymbol(
        raw=symbol,
        is_local=False,
        scheme=scheme,
        manager=manager,
        package=package,
        version=version,
        descriptor=descriptor,
    )
