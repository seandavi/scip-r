"""Second-level resolution with a real R session (``scip-r resolve``).

The tree-sitter pass guesses that an unqualified call such as ``mean(x)``
lands in base R. This module runs the bundled ``resolve.R`` script, which
loads the package (``pkgload::load_all`` on a checkout, or
``loadNamespace`` on an installed package) and asks R's own namespace
machinery where each free name actually resolves, then merges the answers
into the SCIP index:

- guessed ``scip-r . base . name().`` references are rewritten to the
  package, manager and installed version R found, e.g.
  ``scip-r r stats 4.6.0 sd().``;
- explicit ``pkg::name`` references gain the installed version and manager
  of ``pkg``;
- S3 methods registered in ``NAMESPACE`` and S4 methods get an
  ``is_implementation`` relationship to their generic; classes get one to
  each superclass;
- the index's ``tool_info.arguments`` is stamped with a ``resolve_run_id``
  that also appears in a sidecar metadata record describing the R
  environment (R version, platform, Bioconductor release, every loaded
  namespace with version and manager), so index rows and environment facts
  can be joined later.

Requirements: ``Rscript`` on ``PATH`` (or passed explicitly), the R
packages ``pkgload``, ``codetools`` and ``jsonlite``, and the target
package's own dependencies installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__
from . import scip_pb2 as scip
from .parser import GUESSED_PACKAGE, index_arguments
from .symbols import (
    UNKNOWN_MANAGER,
    UNKNOWN_VERSION,
    ParsedSymbol,
    class_descriptor,
    descriptor_for,
    method_descriptor,
    parse_symbol,
    symbol_string,
)

RESOLVED_NOTE = "(resolved by R namespace lookup via {via})"
VERSIONED_NOTE = "(version from the installed package)"


class RscriptNotFoundError(RuntimeError):
    """``Rscript`` could not be located."""


class ResolverError(RuntimeError):
    """``resolve.R`` exited with an error."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"resolve.R failed with exit code {returncode}:\n{stderr.strip()}")


def resolver_script() -> Path:
    """Path of the bundled ``resolve.R``."""
    with resources.as_file(resources.files("scipr") / "r" / "resolve.R") as p:
        return Path(p)


def find_rscript(explicit: str | Path | None = None) -> Path:
    """Locate ``Rscript``; raise :class:`RscriptNotFoundError` if absent."""
    if explicit is not None:
        p = Path(explicit)
        if p.is_file():
            return p
        raise RscriptNotFoundError(f"Rscript not found at {p}")
    found = shutil.which("Rscript")
    if found is None:
        raise RscriptNotFoundError(
            "Rscript is not on PATH. Install R (https://cran.r-project.org/) or pass "
            "--rscript /path/to/Rscript."
        )
    return Path(found)


def _is_guessed(info: scip.SymbolInformation) -> bool:
    return any("guessed" in d for d in info.documentation)


def guessed_names(index: scip.Index) -> list[str]:
    """Names of every guessed external symbol in ``index``."""
    out: set[str] = set()
    for info in index.external_symbols:
        if _is_guessed(info):
            name = parse_symbol(info.symbol).name
            if name:
                out.add(name)
    return sorted(out)


def run_resolver(
    pkg_dir: Path | str | None = None,
    *,
    installed: str | None = None,
    names: list[str] | None = None,
    rscript: str | Path | None = None,
    timeout: float | None = 600,
) -> dict[str, Any]:
    """Run ``resolve.R`` and return its parsed JSON output.

    Give either ``pkg_dir`` (a source checkout, loaded with pkgload) or
    ``installed`` (the name of an installed package, loaded with
    ``loadNamespace``).
    """
    if (pkg_dir is None) == (installed is None):
        raise ValueError("give exactly one of pkg_dir or installed")
    exe = find_rscript(rscript)
    script = resolver_script()
    with tempfile.TemporaryDirectory(prefix="scipr-resolve-") as tmp:
        out = Path(tmp) / "resolved.json"
        source = (
            ["--pkg", str(Path(pkg_dir))]
            if pkg_dir is not None
            else ["--installed", str(installed)]
        )
        cmd = [str(exe), str(script), *source, "--out", str(out)]
        if names:
            names_file = Path(tmp) / "names.json"
            names_file.write_text(json.dumps(names), encoding="utf-8")
            cmd += ["--names", str(names_file)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0 or not out.is_file():
            raise ResolverError(proc.returncode, proc.stderr or proc.stdout)
        data: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return data


class _Packages:
    """Installed-package facts from the resolution's environment block.

    Accepts schema 1 (``name -> version``) and schema 2
    (``name -> {version, manager}``).
    """

    def __init__(self, resolution: dict[str, Any]) -> None:
        raw = resolution.get("environment", {}).get("packages", {}) or {}
        self._info: dict[str, dict[str, Any]] = {}
        for name, v in raw.items():
            self._info[name] = v if isinstance(v, dict) else {"version": v, "manager": None}

    def version(self, pkg: str | None) -> str | None:
        return self._info.get(pkg or "", {}).get("version") or None

    def manager(self, pkg: str | None) -> str | None:
        return self._info.get(pkg or "", {}).get("manager") or None

    def __contains__(self, pkg: object) -> bool:
        return pkg in self._info


@dataclass
class MergeStats:
    guessed_before: int = 0
    resolved: int = 0
    resolved_to_self: int = 0
    still_guessed: int = 0
    versions_filled: int = 0
    managers_filled: int = 0
    s3_relationships: int = 0
    s4_relationships: int = 0
    class_relationships: int = 0
    unmatched_methods: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rewrite_symbol(
    parsed: ParsedSymbol, package: str, version: str | None, manager: str | None
) -> str:
    assert parsed.descriptor is not None
    return symbol_string(
        package,
        version or UNKNOWN_VERSION,
        parsed.descriptor,
        manager=manager or parsed.manager or UNKNOWN_MANAGER,
    )


def merge_resolution(index: scip.Index, resolution: dict[str, Any]) -> MergeStats:
    """Apply an R resolution record to ``index`` in place. Idempotent."""
    stats = MergeStats()
    self_pkg = resolution["package"]["name"]
    self_version = resolution["package"]["version"]
    stamps = index_arguments(index)
    self_manager = stamps.get("manager") or resolution["package"].get("manager") or UNKNOWN_MANAGER
    packages = _Packages(resolution)
    lookup: dict[str, dict[str, Any]] = {
        r["name"]: r for r in resolution.get("resolutions", []) if r.get("package")
    }
    guessed: set[str] = {info.symbol for info in index.external_symbols if _is_guessed(info)}
    stats.guessed_before = len(guessed)

    def self_symbol(descriptor: str) -> str:
        return symbol_string(self_pkg, self_version, descriptor, manager=self_manager)

    # 1. rewrite occurrences ---------------------------------------------------
    rewritten: dict[str, str] = {}  # old symbol -> new symbol
    via: dict[str, str] = {}  # new symbol -> how R found it

    def rewrite(symbol: str) -> str:
        if symbol in rewritten:
            return rewritten[symbol]
        parsed = parse_symbol(symbol)
        new = symbol
        if not parsed.is_local and parsed.version == UNKNOWN_VERSION and parsed.name:
            if symbol in guessed and parsed.package == GUESSED_PACKAGE:
                r = lookup.get(parsed.name)
                if r is not None:
                    if r["package"] == self_pkg:
                        manager: str | None = self_manager
                    else:
                        manager = r.get("manager") or packages.manager(r["package"])
                    new = _rewrite_symbol(parsed, r["package"], r.get("version"), manager)
                    via[new] = r.get("via", "?")
            elif parsed.package and parsed.package in packages:
                new = _rewrite_symbol(
                    parsed,
                    parsed.package,
                    packages.version(parsed.package),
                    packages.manager(parsed.package),
                )
                via[new] = "installed"
        rewritten[symbol] = new
        return new

    for doc in index.documents:
        for occ in doc.occurrences:
            occ.symbol = rewrite(occ.symbol)
        for info in doc.symbols:
            for rel in info.relationships:
                rel.symbol = rewrite(rel.symbol)

    for old in guessed:
        new = rewritten.get(old, old)
        if new == old:
            stats.still_guessed += 1
        elif parse_symbol(new).package == self_pkg:
            stats.resolved_to_self += 1
        else:
            stats.resolved += 1
    stats.versions_filled = sum(
        1 for old, new in rewritten.items() if old != new and old not in guessed
    )
    stats.managers_filled = sum(
        1
        for old, new in rewritten.items()
        if old != new and parse_symbol(new).manager not in (None, UNKNOWN_MANAGER)
    )

    # 2. relationships: methods -> generics, classes -> superclasses ------------
    by_symbol: dict[str, scip.SymbolInformation] = {
        info.symbol: info for doc in index.documents for info in doc.symbols
    }
    needed_external: dict[str, str] = {}  # symbol -> documentation

    def external_symbol(
        pkg: str, descriptor: str, version: str | None, manager: str | None
    ) -> str:
        sym = symbol_string(
            pkg,
            version or packages.version(pkg) or UNKNOWN_VERSION,
            descriptor,
            manager=manager or packages.manager(pkg) or UNKNOWN_MANAGER,
        )
        needed_external.setdefault(sym, f"{pkg}::{parse_symbol(sym).name}")
        return sym

    def generic_symbol(m: dict[str, Any]) -> str:
        gp = m.get("generic_package") or GUESSED_PACKAGE
        desc = descriptor_for(m["generic"], is_function=True)
        if gp == self_pkg:
            return self_symbol(desc)
        return external_symbol(gp, desc, m.get("generic_version"), m.get("generic_manager"))

    def add_impl(info: scip.SymbolInformation, target: str) -> bool:
        if any(r.symbol == target and r.is_implementation for r in info.relationships):
            return False
        rel = info.relationships.add()
        rel.symbol = target
        rel.is_implementation = True
        return True

    for m in resolution.get("methods", []):
        if m.get("system") == "S3":
            s3_info = by_symbol.get(self_symbol(descriptor_for(m["method"], is_function=True)))
            if s3_info is None:
                stats.unmatched_methods.append(f"S3 {m['method']}")
                continue
            if add_impl(s3_info, generic_symbol(m)):
                stats.s3_relationships += 1
        elif m.get("system") == "S4":
            desc = method_descriptor(m["generic"], list(m["signature"]))
            s4_info = by_symbol.get(self_symbol(desc))
            if s4_info is None:
                stats.unmatched_methods.append(f"S4 {desc}")
                continue
            if add_impl(s4_info, generic_symbol(m)):
                stats.s4_relationships += 1

    for c in resolution.get("classes", []):
        sub = by_symbol.get(self_symbol(class_descriptor(c["name"])))
        if sub is None:
            continue
        for parent in c.get("contains", []) or []:
            if isinstance(parent, str):
                parent = {"name": parent}
            pkg = parent.get("package")
            desc = class_descriptor(parent["name"])
            if pkg in (None, self_pkg):
                target = self_symbol(desc)
                if target not in by_symbol:
                    continue  # unknown superclass package: nothing to point at
            else:
                target = external_symbol(pkg, desc, parent.get("version"), parent.get("manager"))
            if add_impl(sub, target):
                stats.class_relationships += 1

    # 3. rebuild external_symbols ------------------------------------------------
    old_docs: dict[str, list[str]] = {
        info.symbol: list(info.documentation) for info in index.external_symbols
    }
    referenced: set[str] = set()
    for doc in index.documents:
        for occ in doc.occurrences:
            referenced.add(occ.symbol)
        for info in doc.symbols:
            for rel in info.relationships:
                referenced.add(rel.symbol)
    referenced.update(needed_external)

    del index.external_symbols[:]
    for sym in sorted(referenced):
        parsed = parse_symbol(sym)
        if parsed.is_local or parsed.package is None or parsed.package == self_pkg:
            continue
        info = index.external_symbols.add()
        info.symbol = sym
        base_doc = f"{parsed.package}::{parsed.name}"
        if via.get(sym) == "installed":
            doc_str = f"{base_doc}  {VERSIONED_NOTE}"
        elif sym in via:
            doc_str = f"{base_doc}  {RESOLVED_NOTE.format(via=via[sym])}"
        elif sym in old_docs:
            doc_str = old_docs[sym][0] if old_docs[sym] else base_doc
        else:
            doc_str = needed_external.get(sym, base_doc)
        info.documentation.append(doc_str)
        kinds = scip.SymbolInformation.Kind
        info.kind = (
            kinds.Class
            if parsed.is_class
            else kinds.Function
            if parsed.is_function
            else kinds.UnspecifiedKind
        )

    # 4. stamp the run id into the index ------------------------------------------
    args = index.metadata.tool_info.arguments
    for prefix in ("resolve_run_id=", "resolver=", "r_version=", "bioc_version="):
        for i in range(len(args) - 1, -1, -1):
            if args[i].startswith(prefix):
                del args[i]
    resolver = resolution.get("resolver", {})
    env = resolution.get("environment", {})
    args.extend(
        [
            f"resolve_run_id={resolution['run_id']}",
            f"resolver={resolver.get('name', 'scip-r-resolve')}/{resolver.get('version', '?')}",
            f"r_version={env.get('r_version', '?')}",
        ]
    )
    if env.get("bioc_version"):
        args.append(f"bioc_version={env['bioc_version']}")
    return stats


def resolve_run_id(index: scip.Index) -> str | None:
    """The ``resolve_run_id`` stamped on an index, if any."""
    return index_arguments(index).get("resolve_run_id")


def metadata_record(
    resolution: dict[str, Any],
    stats: MergeStats,
    *,
    index_path: Path | str | None = None,
    index_bytes: bytes | None = None,
    source: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The sidecar record written next to a resolved index.

    ``run_id`` matches ``resolve_run_id`` in the index's ``tool_info.arguments``
    (and the ``resolve_run_id`` column of every exported table);
    ``index.sha256`` is the digest of the resolved index file, so either key
    joins the record to the data it describes. ``source`` is the static
    pass's provenance (package, version, manager, git commit, tarball
    digest, Bioconductor build fields); pass ``index_arguments(index)``.
    """
    return {
        "schema_version": resolution.get("schema_version", 1),
        "run_id": resolution["run_id"],
        "scip_r_version": __version__,
        "resolver": resolution.get("resolver"),
        "package": resolution.get("package"),
        "source": {
            k: v
            for k, v in (source or {}).items()
            if not k.startswith(("resolve_run_id", "resolver", "r_version", "bioc_version"))
        },
        "environment": resolution.get("environment"),
        "summary": {**resolution.get("summary", {}), "merge": stats.to_dict()},
        "index": {
            "path": Path(index_path).name if index_path is not None else None,
            "sha256": hashlib.sha256(index_bytes).hexdigest() if index_bytes is not None else None,
        },
    }


@dataclass
class ResolveResult:
    index: scip.Index
    resolution: dict[str, Any]
    stats: MergeStats


def resolve_index(
    index: scip.Index,
    pkg_dir: Path | str | None = None,
    *,
    installed: str | None = None,
    rscript: str | Path | None = None,
    timeout: float | None = 600,
) -> ResolveResult:
    """Run the R resolver (source checkout or installed package) and merge
    it into ``index`` in place."""
    resolution = run_resolver(
        pkg_dir, installed=installed, names=guessed_names(index), rscript=rscript, timeout=timeout
    )
    stats = merge_resolution(index, resolution)
    return ResolveResult(index=index, resolution=resolution, stats=stats)
