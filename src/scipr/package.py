"""Package-level metadata read without R: DESCRIPTION, NAMESPACE, file
order, provenance and tarball handling."""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_r as tsr
from tree_sitter import Language, Node, Parser

R_LANGUAGE = Language(tsr.language())
R_SOURCE_SUFFIXES = frozenset({".R", ".r"})

MANAGER_CRAN = "cran"
MANAGER_BIOCONDUCTOR = "bioconductor"
MANAGER_R = "r"  # packages shipped with R itself (Priority: base)
UNKNOWN_MANAGER = "."


# ---- DESCRIPTION ---------------------------------------------------------------


def parse_dcf(text: str) -> dict[str, str]:
    """Parse one Debian-control-format record (a DESCRIPTION file).

    Continuation lines start with whitespace; values are joined with a
    newline so ``Collate`` and ``Description`` keep their line breaks.
    """
    fields: dict[str, str] = {}
    key: str | None = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if raw[0] in " \t" and key is not None:
            fields[key] = fields[key] + "\n" + raw.strip()
            continue
        m = re.match(r"^([A-Za-z0-9.@/_-]+)\s*:\s*(.*)$", raw)
        if m is None:
            continue
        key = m.group(1)
        fields[key] = m.group(2).strip()
    return fields


def infer_manager(fields: dict[str, str]) -> str:
    """Best-effort package manager from DESCRIPTION fields.

    ``biocViews`` marks a Bioconductor package. ``Priority: base`` marks a
    package that ships with R. Everything else defaults to ``cran``, which
    is wrong for GitHub-only packages but is the most common case; pass
    ``--manager`` to override.
    """
    if "biocViews" in fields:
        return MANAGER_BIOCONDUCTOR
    if fields.get("Priority", "").strip().lower() == "base":
        return MANAGER_R
    return MANAGER_CRAN


def parse_collate(value: str) -> list[str]:
    """``Collate`` is whitespace separated with optional quoting."""
    try:
        return shlex.split(value.replace("\n", " "))
    except ValueError:
        return value.split()


@dataclass(frozen=True)
class PackageInfo:
    name: str
    version: str
    manager: str = MANAGER_CRAN
    collate: tuple[str, ...] = ()
    fields: dict[str, str] = field(default_factory=dict, compare=False, hash=False)


def read_description(pkg_dir: Path | str, *, manager: str | None = None) -> PackageInfo:
    """Package name/version/manager from DESCRIPTION.

    Falls back to the directory name and ``0.0.0`` when the file is absent
    or the fields are missing.
    """
    pkg_dir = Path(pkg_dir)
    name, version = pkg_dir.resolve().name, "0.0.0"
    fields: dict[str, str] = {}
    desc_path = pkg_dir / "DESCRIPTION"
    if desc_path.is_file():
        fields = parse_dcf(desc_path.read_text(encoding="utf-8", errors="replace"))
        name = fields.get("Package", name).split()[0] if fields.get("Package") else name
        version = fields.get("Version", version).split()[0] if fields.get("Version") else version
    collate = tuple(parse_collate(fields["Collate"])) if fields.get("Collate") else ()
    return PackageInfo(
        name=name,
        version=version,
        manager=manager or infer_manager(fields),
        collate=collate,
        fields=fields,
    )


# ---- source files --------------------------------------------------------------


def find_r_files(pkg_dir: Path | str, collate: tuple[str, ...] | list[str] = ()) -> list[Path]:
    """All ``.R``/``.r`` files directly under ``<pkg>/R/``.

    Files named in ``collate`` come first, in that order (R sources them in
    ``Collate`` order, so a later file's definition wins); the rest follow
    sorted. Subdirectories of ``R/`` and dotfiles are skipped, as R does. If
    there is no ``R/`` directory the whole tree is searched recursively, so
    a loose directory of scripts can still be indexed.
    """
    pkg_dir = Path(pkg_dir)
    r_dir = pkg_dir / "R"
    if r_dir.is_dir():
        # R CMD INSTALL sources only the files directly in R/; subdirectories
        # (R/TODO, R/examples, ...) are ignored, and so are dotfiles (which
        # also skips macOS "._foo.R" AppleDouble files).
        root = r_dir
        candidates = r_dir.iterdir()
    else:
        root = pkg_dir
        candidates = pkg_dir.rglob("*")
    found = sorted(
        p
        for p in candidates
        if p.is_file()
        and p.suffix in R_SOURCE_SUFFIXES
        and not p.name.startswith(".")
        and not any(part.startswith(".") for part in p.relative_to(root).parts[:-1])
    )
    if not collate:
        return found
    by_name = {p.relative_to(root).as_posix(): p for p in found}
    ordered = [by_name[c] for c in collate if c in by_name]
    rest = [p for p in found if p not in ordered]
    return ordered + rest


# ---- NAMESPACE -------------------------------------------------------------------


@dataclass(frozen=True)
class S3Method:
    generic: str
    cls: str
    method: str
    generic_package: str | None = None  # from ``S3method(pkg::generic, class)``


@dataclass
class NamespaceInfo:
    exports: set[str] = field(default_factory=set)
    export_patterns: list[str] = field(default_factory=list)
    export_classes: set[str] = field(default_factory=set)
    export_methods: set[str] = field(default_factory=set)
    imports: dict[str, str] = field(default_factory=dict)  # name -> package (importFrom)
    import_all: list[str] = field(default_factory=list)  # import(pkg)
    s3methods: list[S3Method] = field(default_factory=list)

    def is_exported(self, name: str) -> bool:
        if name in self.exports:
            return True
        return any(_r_regex_match(pat, name) for pat in self.export_patterns)


def _r_regex_match(pattern: str, name: str) -> bool:
    try:
        return re.search(pattern, name) is not None
    except re.error:
        return False


def _arg_values(src: bytes, call: Node) -> list[tuple[str | None, Node]]:
    args = call.child_by_field_name("arguments")
    out: list[tuple[str | None, Node]] = []
    if args is None:
        return out
    for a in args.children:
        if a.type != "argument":
            continue
        name_node = a.child_by_field_name("name")
        value = a.child_by_field_name("value")
        if value is None:
            continue
        name = src[name_node.start_byte : name_node.end_byte].decode() if name_node else None
        out.append((name, value))
    return out


def _name_of(src: bytes, node: Node) -> str | None:
    """Identifier or string literal text."""
    if node.type == "identifier":
        return src[node.start_byte : node.end_byte].decode("utf-8", "replace")
    if node.type == "string":
        content = next((c for c in node.children if c.type == "string_content"), None)
        return (
            src[content.start_byte : content.end_byte].decode("utf-8", "replace")
            if content
            else ""
        )
    return None


def parse_namespace(src: bytes) -> NamespaceInfo:
    """Parse NAMESPACE directives (they are R syntax) with tree-sitter."""
    ns = NamespaceInfo()
    tree = Parser(R_LANGUAGE).parse(src)
    for stmt in tree.root_node.children:
        if stmt.type != "call":
            continue
        fn = stmt.child_by_field_name("function")
        if fn is None or fn.type != "identifier":
            continue
        directive = src[fn.start_byte : fn.end_byte].decode()
        args = _arg_values(src, stmt)
        positional = [v for n, v in args if n is None]
        names = [n for n in (_name_of(src, v) for v in positional) if n is not None]
        if directive == "export":
            ns.exports.update(names)
        elif directive == "exportPattern":
            ns.export_patterns.extend(names)
        elif directive == "exportClasses" or directive == "exportClass":
            ns.export_classes.update(names)
        elif directive == "exportMethods":
            ns.export_methods.update(names)
        elif directive == "importFrom" and names:
            pkg, *imported = names
            for n in imported:
                ns.imports.setdefault(n, pkg)
        elif directive == "import":
            ns.import_all.extend(names)
        elif directive == "S3method" and positional:
            gnode = positional[0]
            generic_pkg: str | None = None
            if gnode.type == "namespace_operator":
                lhs, rhs = gnode.child_by_field_name("lhs"), gnode.child_by_field_name("rhs")
                if lhs is None or rhs is None:
                    continue
                generic_pkg = _name_of(src, lhs)
                generic = _name_of(src, rhs)
            else:
                generic = _name_of(src, gnode)
            if generic is None or len(positional) < 2:
                continue
            cls = _name_of(src, positional[1])
            if cls is None:
                continue
            method = _name_of(src, positional[2]) if len(positional) > 2 else None
            ns.s3methods.append(S3Method(generic, cls, method or f"{generic}.{cls}", generic_pkg))
    return ns


def read_namespace(pkg_dir: Path | str) -> NamespaceInfo | None:
    p = Path(pkg_dir) / "NAMESPACE"
    if not p.is_file():
        return None
    return parse_namespace(p.read_bytes())


# ---- provenance ------------------------------------------------------------------

_DESCRIPTION_PROVENANCE = {
    "Repository": "repository",
    "git_url": "git_url",
    "git_branch": "git_branch",
    "git_last_commit": "git_last_commit",
    "git_last_commit_date": "git_last_commit_date",
    "Date/Publication": "date_publication",
    "Packaged": "packaged",
    "RemoteSha": "remote_sha",
    "RemoteUrl": "remote_url",
}


def git_provenance(pkg_dir: Path | str) -> dict[str, str]:
    """``git_commit`` and ``git_dirty`` when ``pkg_dir`` is inside a git
    checkout; empty otherwise. Never raises."""
    out: dict[str, str] = {}
    try:
        head = subprocess.run(
            ["git", "-C", str(pkg_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if head.returncode != 0:
            return out
        out["git_commit"] = head.stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(pkg_dir), "status", "--porcelain", "--", "."],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if status.returncode == 0:
            out["git_dirty"] = "true" if status.stdout.strip() else "false"
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def provenance(pkg_dir: Path | str, fields: dict[str, str] | None = None) -> dict[str, str]:
    """Everything the static pass can say about where the source came from.

    Keys are stable identifiers suitable for ``key=value`` stamps in
    ``tool_info.arguments``: ``git_commit``, ``git_dirty``, plus the
    Bioconductor/CRAN build fields from DESCRIPTION (``git_url``,
    ``git_branch``, ``git_last_commit``, ``date_publication``, ...).
    """
    out: dict[str, str] = {}
    fields = fields if fields is not None else read_description(pkg_dir).fields
    for src_key, key in _DESCRIPTION_PROVENANCE.items():
        if fields.get(src_key):
            out[key] = " ".join(fields[src_key].split())
    out.update(git_provenance(pkg_dir))
    return out


# ---- tarballs --------------------------------------------------------------------

TARBALL_SUFFIXES = (".tar.gz", ".tgz", ".tar")


def is_tarball(path: Path | str) -> bool:
    return str(path).endswith(TARBALL_SUFFIXES) and Path(path).is_file()


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def unpack_tarball(path: Path | str, dest: Path | str) -> Path:
    """Extract a source tarball and return the package root inside it.

    The root is the directory containing DESCRIPTION (CRAN/Bioconductor
    tarballs have exactly one top-level directory); falls back to ``dest``.
    """
    dest = Path(dest)
    with tarfile.open(path) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:  # Python < 3.12 without the filter argument
            tf.extractall(dest)
    candidates = sorted(p.parent for p in dest.rglob("DESCRIPTION") if p.is_file())
    if candidates:
        return min(candidates, key=lambda p: len(p.parts))
    return dest
