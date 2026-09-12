from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

from scipr.package import (
    NamespaceInfo,
    S3Method,
    find_r_files,
    git_provenance,
    infer_manager,
    is_tarball,
    parse_collate,
    parse_dcf,
    parse_namespace,
    provenance,
    read_description,
    read_namespace,
    sha256_file,
    unpack_tarball,
)
from tests.conftest import MakePackage


def test_parse_dcf_continuations() -> None:
    fields = parse_dcf(
        "Package: x\nDescription: first line\n  second line\n\tthird\n"
        "Collate:\n    'a.R'\n    'b.R'\nEmpty:\n"
    )
    assert fields["Package"] == "x"
    assert fields["Description"] == "first line\nsecond line\nthird"
    assert fields["Collate"] == "\n'a.R'\n'b.R'"
    assert fields["Empty"] == ""


def test_parse_collate_quoting() -> None:
    assert parse_collate("'a.R' \"b c.R\"\n d.R") == ["a.R", "b c.R", "d.R"]


def test_infer_manager() -> None:
    assert infer_manager({}) == "cran"
    assert infer_manager({"biocViews": "Software"}) == "bioconductor"
    assert infer_manager({"Priority": "base"}) == "r"
    assert infer_manager({"Priority": "recommended"}) == "cran"
    assert infer_manager({"Repository": "CRAN", "biocViews": "x"}) == "bioconductor"


def test_read_description_fields_and_override(make_package: MakePackage) -> None:
    root = make_package({}, name="p")
    (root / "DESCRIPTION").write_text(
        "Package: p\nVersion: 1.2\nbiocViews: Software\nCollate: 'z.R' 'a.R'\ngit_url: https://git.bioconductor.org/packages/p\n"
    )
    info = read_description(root)
    assert (info.name, info.version, info.manager) == ("p", "1.2", "bioconductor")
    assert info.collate == ("z.R", "a.R")
    assert info.fields["git_url"].endswith("/p")
    assert read_description(root, manager="github").manager == "github"


def test_find_r_files_collate_and_dotfiles(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "", "R/b.R": "", "R/c.R": "", "R/._b.R": "", "R/.x/y.R": ""})
    rel = [p.name for p in find_r_files(root)]
    assert rel == ["a.R", "b.R", "c.R"]
    rel = [p.name for p in find_r_files(root, ("c.R", "missing.R", "a.R"))]
    assert rel == ["c.R", "a.R", "b.R"]


def test_parse_namespace_directives() -> None:
    ns = parse_namespace(
        b"""export(f, g)
exportPattern("^h")
exportClasses(Foo)
exportMethods(show, "width")
importFrom(stats, sd, "quantile")
importFrom(utils, head)
import(methods, R6)
S3method(print, zres)
S3method(base::format, zres)
S3method(dplyr::summarise, zres, summarise_zres)
useDynLib(pkg, .registration = TRUE)
if (FALSE) export(no)
"""
    )
    assert ns.exports == {"f", "g"}
    assert ns.export_patterns == ["^h"]
    assert ns.export_classes == {"Foo"}
    assert ns.export_methods == {"show", "width"}
    assert ns.imports == {"sd": "stats", "quantile": "stats", "head": "utils"}
    assert ns.import_all == ["methods", "R6"]
    assert ns.s3methods == [
        S3Method("print", "zres", "print.zres", None),
        S3Method("format", "zres", "format.zres", "base"),
        S3Method("summarise", "zres", "summarise_zres", "dplyr"),
    ]
    assert ns.is_exported("f") and ns.is_exported("hello") and not ns.is_exported("zzz")


def test_is_exported_bad_pattern_is_ignored() -> None:
    ns = NamespaceInfo(export_patterns=["("])
    assert not ns.is_exported("x")


def test_read_namespace_missing(make_package: MakePackage) -> None:
    assert read_namespace(make_package({})) is None


def test_provenance_from_description(make_package: MakePackage, tmp_path: Path) -> None:
    root = make_package({}, name="p")
    (root / "DESCRIPTION").write_text(
        "Package: p\nVersion: 1\nRepository: CRAN\n"
        "git_url: https://git.bioconductor.org/packages/p\n"
        "git_branch: RELEASE_3_22\ngit_last_commit: abc123\n"
        "Date/Publication: 2026-01-02 10:00:00 UTC\n"
    )
    prov = provenance(root)
    assert prov["repository"] == "CRAN"
    assert prov["git_branch"] == "RELEASE_3_22"
    assert prov["git_last_commit"] == "abc123"
    assert prov["date_publication"] == "2026-01-02 10:00:00 UTC"


def test_git_provenance(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "DESCRIPTION").write_text("Package: p\nVersion: 1\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(tmp_path),
    }
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "x"], check=True, env=env)
    prov = git_provenance(repo)
    assert len(prov["git_commit"]) == 40 and prov["git_dirty"] == "false"
    (repo / "new.txt").write_text("dirty")
    assert git_provenance(repo)["git_dirty"] == "true"
    assert git_provenance(tmp_path / "nowhere") == {}


def test_tarball_helpers(make_package: MakePackage, tmp_path: Path) -> None:
    root = make_package({"R/a.R": "f <- function() 1\n"}, name="tp")
    tb = tmp_path / "tp_1.0.0.tar.gz"
    with tarfile.open(tb, "w:gz") as tf:
        tf.add(root, arcname="tp")
    assert is_tarball(tb) and not is_tarball(root) and not is_tarball(tmp_path / "missing.tar.gz")
    assert len(sha256_file(tb)) == 64
    dest = tmp_path / "unpacked"
    inner = unpack_tarball(tb, dest)
    assert inner == dest / "tp" and (inner / "DESCRIPTION").is_file()
