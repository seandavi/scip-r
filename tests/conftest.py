from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scipr import build_index
from scipr import scip_pb2 as scip

FIXTURES = Path(__file__).parent / "fixtures"
TESTPKG = FIXTURES / "testpkg"
TESTPKG_INDEX = FIXTURES / "testpkg-index.scip"


@pytest.fixture(scope="session")
def testpkg_dir() -> Path:
    return TESTPKG


@pytest.fixture(scope="session")
def testpkg_index_file() -> Path:
    return TESTPKG_INDEX


@pytest.fixture(scope="session")
def testpkg_index() -> scip.Index:
    return build_index(TESTPKG)


MakePackage = Callable[..., Path]


@pytest.fixture
def make_package(tmp_path: Path) -> MakePackage:
    """Build a throwaway R package on disk.

    ``make_package({"R/a.R": "x <- 1"}, name="pkg", version="1.0")``
    """

    def _make(
        files: dict[str, str],
        *,
        name: str | None = "pkg",
        version: str | None = "1.0.0",
        description: bool = True,
    ) -> Path:
        root = tmp_path / (name or "pkg")
        root.mkdir(exist_ok=True)
        if description:
            lines = []
            if name is not None:
                lines.append(f"Package: {name}")
            if version is not None:
                lines.append(f"Version: {version}")
            (root / "DESCRIPTION").write_text("\n".join(lines) + "\n")
        for rel, text in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return root

    return _make


def occurrences(index: scip.Index, relative_path: str) -> list[tuple[list[int], str, int]]:
    """``[(range, symbol, roles)]`` for one document, in emission order."""
    for doc in index.documents:
        if doc.relative_path == relative_path:
            return [(list(o.range), o.symbol, o.symbol_roles) for o in doc.occurrences]
    raise KeyError(relative_path)


def symbols_at(index: scip.Index, relative_path: str) -> dict[str, list[list[int]]]:
    """symbol -> list of ranges, for one document."""
    out: dict[str, list[list[int]]] = {}
    for rng, sym, _ in occurrences(index, relative_path):
        out.setdefault(sym, []).append(rng)
    return out
