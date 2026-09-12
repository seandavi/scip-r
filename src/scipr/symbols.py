"""Helpers for scip-r's symbol-string convention.

SCIP treats the symbol string as opaque. scip-r uses this shape for
package-level and external symbols::

    scip-r cran <package> <version> <descriptor>

where ``<descriptor>`` is ``name().`` for functions and ``name.`` for other
top-level objects, and ``<version>`` is ``.`` when unknown (external
references resolved from source alone). Locals use SCIP's own
``local N`` convention and are only meaningful within one document.
"""

from __future__ import annotations

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

    @property
    def name(self) -> str | None:
        """Bare name with the descriptor suffix (``().`` or ``.``) stripped."""
        if self.descriptor is None:
            return None
        d = self.descriptor
        if d.endswith("()."):
            return d[:-3]
        if d.endswith("."):
            return d[:-1]
        return d

    @property
    def is_function(self) -> bool:
        return self.descriptor is not None and self.descriptor.endswith("().")

    @property
    def is_external(self) -> bool:
        """True for references whose defining package was not indexed."""
        return not self.is_local and self.version == UNKNOWN_VERSION


def symbol_string(package: str, version: str, descriptor: str, *, scheme: str = SCHEME) -> str:
    """Build a package-level symbol string in scip-r's convention."""
    return f"{scheme} {MANAGER} {package} {version} {descriptor}"


def descriptor_for(name: str, *, is_function: bool) -> str:
    return f"{name}()." if is_function else f"{name}."


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
