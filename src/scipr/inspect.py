"""Read back and summarise SCIP indexes without any optional dependencies."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import scip_pb2 as scip
from .symbols import parse_symbol


def load_index(path: Path | str) -> scip.Index:
    """Parse an ``index.scip`` file from disk."""
    index = scip.Index()
    index.ParseFromString(Path(path).read_bytes())
    return index


def write_index(index: scip.Index, path: Path | str) -> Path:
    """Serialise ``index`` to ``path`` and return the path written."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(index.SerializeToString())
    return out


@dataclass
class DocumentSummary:
    relative_path: str
    symbols: int
    occurrences: int
    definitions: int


@dataclass
class IndexSummary:
    tool_name: str
    tool_version: str
    project_root: str
    documents: int
    symbols: int
    occurrences: int
    definitions: int
    references: int
    local_occurrences: int
    external_symbols: int
    guessed_external_symbols: int
    external_packages: dict[str, int] = field(default_factory=dict)
    per_document: list[DocumentSummary] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def one_line(self) -> str:
        return (
            f"{self.documents} documents, {self.symbols} defined symbols, "
            f"{self.occurrences} occurrences ({self.definitions} definitions), "
            f"{self.external_symbols} external symbols referenced "
            f"({self.guessed_external_symbols} guessed)"
        )


def summarize(index: scip.Index) -> IndexSummary:
    """Compute counts over an index. Cheap enough to run on every index."""
    definitions = 0
    references = 0
    local_occ = 0
    per_doc: list[DocumentSummary] = []
    for doc in index.documents:
        doc_defs = 0
        for occ in doc.occurrences:
            is_def = bool(occ.symbol_roles & scip.SymbolRole.Definition)
            doc_defs += is_def
            if occ.symbol.startswith("local "):
                local_occ += 1
        definitions += doc_defs
        references += len(doc.occurrences) - doc_defs
        per_doc.append(
            DocumentSummary(
                relative_path=doc.relative_path,
                symbols=len(doc.symbols),
                occurrences=len(doc.occurrences),
                definitions=doc_defs,
            )
        )

    guessed = 0
    packages: Counter[str] = Counter()
    for ext in index.external_symbols:
        parsed = parse_symbol(ext.symbol)
        if parsed.package is not None:
            packages[parsed.package] += 1
        if any("guessed" in d for d in ext.documentation):
            guessed += 1

    return IndexSummary(
        tool_name=index.metadata.tool_info.name,
        tool_version=index.metadata.tool_info.version,
        project_root=index.metadata.project_root,
        documents=len(index.documents),
        symbols=sum(len(d.symbols) for d in index.documents),
        occurrences=sum(len(d.occurrences) for d in index.documents),
        definitions=definitions,
        references=references,
        local_occurrences=local_occ,
        external_symbols=len(index.external_symbols),
        guessed_external_symbols=guessed,
        external_packages=dict(sorted(packages.items())),
        per_document=per_doc,
    )


def format_range(rng: Any) -> str:
    """Render a SCIP range as ``line:col-line:col`` (1-based, like editors)."""
    r = list(rng)
    if len(r) == 3:
        sl, sc, ec = r
        el = sl
    else:
        sl, sc, el, ec = r
    return f"{sl + 1}:{sc + 1}-{el + 1}:{ec + 1}"


def render_text(index: scip.Index, *, show_locals: bool = True) -> str:
    """Human-readable dump of an index, in the spirit of ``scip print``."""
    lines: list[str] = []
    md = index.metadata
    lines.append(f"# {md.tool_info.name} {md.tool_info.version}  root={md.project_root}")
    for doc in index.documents:
        lines.append("")
        lines.append(f"== {doc.relative_path} ({doc.language})")
        for info in doc.symbols:
            kind = scip.SymbolInformation.Kind.Name(info.kind)
            doc_str = f"  # {info.documentation[0]}" if info.documentation else ""
            lines.append(f"  symbol {kind:<10} {info.symbol}{doc_str}")
        for occ in doc.occurrences:
            if not show_locals and occ.symbol.startswith("local "):
                continue
            role = "def" if occ.symbol_roles & scip.SymbolRole.Definition else "ref"
            lines.append(f"  {format_range(occ.range):<14} {role}  {occ.symbol}")
    if index.external_symbols:
        lines.append("")
        lines.append("== external symbols")
        for info in index.external_symbols:
            doc_str = f"  # {info.documentation[0]}" if info.documentation else ""
            lines.append(f"  {info.symbol}{doc_str}")
    return "\n".join(lines) + "\n"
