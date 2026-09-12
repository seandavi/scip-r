from __future__ import annotations

from pathlib import Path

import pytest

from scipr import build_index
from scipr import scip_pb2 as scip
from scipr.parser import (
    GUESSED_NOTE,
    PackageInfo,
    collect_top_level_symbols,
    find_r_files,
    read_description,
)
from tests.conftest import MakePackage, occurrences, symbols_at

DEF = scip.SymbolRole.Definition


# --- DESCRIPTION / file discovery -------------------------------------------


def test_read_description(make_package: MakePackage) -> None:
    root = make_package({}, name="mypkg", version="2.3.4")
    assert read_description(root) == PackageInfo("mypkg", "2.3.4")


def test_read_description_missing_file_falls_back_to_dirname(make_package: MakePackage) -> None:
    root = make_package({}, name="loose", description=False)
    assert read_description(root) == PackageInfo("loose", "0.0.0")


def test_read_description_missing_fields(make_package: MakePackage) -> None:
    root = make_package({}, name="onlyname", version=None)
    assert read_description(root) == PackageInfo("onlyname", "0.0.0")


def test_find_r_files_only_under_R_dir_recursively_and_sorted(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/zeta.R": "",
            "R/alpha.r": "",
            "R/sub/nested.R": "",
            "R/notes.txt": "",
            "tests/testthat/test-x.R": "",
            "vignettes/v.R": "",
        }
    )
    rel = [p.relative_to(root).as_posix() for p in find_r_files(root)]
    assert rel == ["R/alpha.r", "R/sub/nested.R", "R/zeta.R"]


def test_find_r_files_without_R_dir_searches_whole_tree(make_package: MakePackage) -> None:
    root = make_package({"a.R": "", "deep/b.R": ""}, description=False)
    rel = [p.relative_to(root).as_posix() for p in find_r_files(root)]
    assert rel == ["a.R", "deep/b.R"]


def test_build_index_rejects_non_directory(tmp_path: Path) -> None:
    with pytest.raises(NotADirectoryError):
        build_index(tmp_path / "nope")


# --- top-level symbol collection --------------------------------------------


def test_collect_top_level_symbols_kinds_and_signatures(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": "f <- function(x) x\nCONST = 42\ng <<- function() NULL\n",
            "R/b.R": "obj$field <- 1\n" + "long <- function(" + "a, " * 60 + ") NULL\n",
        }
    )
    syms = collect_top_level_symbols(find_r_files(root))
    assert set(syms) == {"f", "CONST", "g", "long"}  # obj$field is not an identifier LHS
    assert syms["f"].kind == "function"
    assert syms["CONST"].kind == "value"
    assert syms["g"].kind == "function"
    assert syms["f"].signature == "f <- function(x) x"
    assert syms["long"].signature.endswith("...")
    assert len(syms["long"].signature) == 120
    assert syms["f"].range == (0, 0, 1)


def test_later_file_wins_on_duplicate_names(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- 1\n", "R/b.R": "f <- function() 2\n"})
    syms = collect_top_level_symbols(find_r_files(root))
    assert syms["f"].kind == "function"
    assert syms["f"].file.name == "b.R"


# --- metadata ----------------------------------------------------------------


def test_metadata(testpkg_index: scip.Index, testpkg_dir: Path) -> None:
    from scipr import __version__

    md = testpkg_index.metadata
    assert md.tool_info.name == "scip-r"
    assert md.tool_info.version == __version__
    assert md.project_root == testpkg_dir.resolve().as_uri()
    assert md.text_document_encoding == scip.TextEncoding.UTF8
    assert [d.relative_path for d in testpkg_index.documents] == [
        "R/pipeline.R",
        "R/stats_helpers.R",
    ]
    assert all(d.language == "R" for d in testpkg_index.documents)


def test_tool_version_override(testpkg_dir: Path) -> None:
    idx = build_index(testpkg_dir, tool_version="9.9.9")
    assert idx.metadata.tool_info.version == "9.9.9"


def test_relative_paths_are_posix(make_package: MakePackage) -> None:
    root = make_package({"R/sub/dir/f.R": "f <- function() 1\n"})
    idx = build_index(root)
    assert idx.documents[0].relative_path == "R/sub/dir/f.R"


# --- resolution on the fixture package --------------------------------------


def test_cross_file_calls_resolve_to_package_symbols(testpkg_index: scip.Index) -> None:
    occ = symbols_at(testpkg_index, "R/pipeline.R")
    assert occ["scip-r cran testpkg 0.1.0 zscore()."] == [[2, 7, 13]]
    assert occ["scip-r cran testpkg 0.1.0 winsorize()."] == [[4, 9, 18]]


def test_definition_role_and_symbol_information(testpkg_index: scip.Index) -> None:
    doc = testpkg_index.documents[1]
    assert doc.relative_path == "R/stats_helpers.R"
    infos = {s.symbol: s for s in doc.symbols}
    assert set(infos) == {
        "scip-r cran testpkg 0.1.0 zscore().",
        "scip-r cran testpkg 0.1.0 winsorize().",
    }
    z = infos["scip-r cran testpkg 0.1.0 zscore()."]
    assert z.kind == scip.SymbolInformation.Kind.Function
    assert z.documentation == ["zscore <- function(x, na.rm = TRUE) {"]
    defs = [
        o for o in doc.occurrences if o.symbol_roles & DEF and not o.symbol.startswith("local")
    ]
    assert [(list(o.range), o.symbol) for o in defs] == [
        ([1, 0, 6], "scip-r cran testpkg 0.1.0 zscore()."),
        ([8, 0, 9], "scip-r cran testpkg 0.1.0 winsorize()."),
    ]


def test_namespaced_calls_become_external_refs(testpkg_index: scip.Index) -> None:
    occ = symbols_at(testpkg_index, "R/stats_helpers.R")
    assert occ["scip-r cran stats . sd()."] == [[3, 14, 16]]
    assert occ["scip-r cran stats . quantile()."] == [[9, 15, 23]]


def test_unresolved_calls_are_guessed_as_base(testpkg_index: scip.Index) -> None:
    occ = symbols_at(testpkg_index, "R/stats_helpers.R")
    assert occ["scip-r cran base . mean()."] == [[2, 8, 12]]
    assert occ["scip-r cran base . c()."] == [[8, 33, 34]]


def test_external_symbols_sorted_and_documented(testpkg_index: scip.Index) -> None:
    ext = {s.symbol: s for s in testpkg_index.external_symbols}
    assert list(ext) == [
        "scip-r cran base . c().",
        "scip-r cran base . mean().",
        "scip-r cran stats . quantile().",
        "scip-r cran stats . sd().",
    ]
    assert ext["scip-r cran base . mean()."].documentation == [f"base::mean  {GUESSED_NOTE}"]
    assert ext["scip-r cran stats . sd()."].documentation == ["stats::sd"]
    assert all(s.kind == scip.SymbolInformation.Kind.Function for s in ext.values())


def test_locals_parameters_and_body_assignments(testpkg_index: scip.Index) -> None:
    occ = occurrences(testpkg_index, "R/pipeline.R")
    # run_pipeline <- function(x, clip = TRUE) {
    #   y <- zscore(x); if (clip) { y <- winsorize(y) }; y
    # }
    assert occ[1] == ([1, 25, 26], "local 0", DEF)  # x
    assert occ[2] == ([1, 28, 32], "local 1", DEF)  # clip
    assert occ[3] == ([2, 2, 3], "local 2", DEF)  # y <-
    assert ([2, 14, 15], "local 0", 0) in occ  # zscore(x)
    assert ([3, 6, 10], "local 1", 0) in occ  # if (clip)
    assert ([4, 4, 5], "local 2", 0) in occ  # y <- winsorize(y): re-assignment is not a def
    assert ([6, 2, 3], "local 2", 0) in occ  # trailing y


def test_local_ids_are_unique_per_document(testpkg_index: scip.Index) -> None:
    occ = occurrences(testpkg_index, "R/stats_helpers.R")
    defs = [(sym, rng) for rng, sym, roles in occ if sym.startswith("local") and roles & DEF]
    ids = [sym for sym, _ in defs]
    assert ids == [f"local {i}" for i in range(len(ids))]
    assert len(ids) == 7  # x, na.rm, mu, s | x, probs, qs


def test_guessed_positions(testpkg_dir: Path) -> None:
    positions: list = []
    build_index(testpkg_dir, positions_out=positions)
    assert positions == [
        {"file": "R/stats_helpers.R", "line": 2, "character": 8},
        {"file": "R/stats_helpers.R", "line": 8, "character": 33},
    ]


# --- targeted syntax cases ---------------------------------------------------


def test_multiline_range_uses_four_elements(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x) {\n  `multi\nline` <- 1\n}\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    ranges = [rng for rng, _, _ in occ]
    assert any(len(r) == 4 for r in ranges)


def test_equals_and_superassignment_define_top_level(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "a = function() b()\nb <<- function() 1\n"})
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 a()."] == [[0, 0, 1]]
    assert occ["scip-r cran pkg 1.0.0 b()."] == [[0, 15, 16], [1, 0, 1]]
    assert idx.external_symbols == []


def test_value_symbol_kind_is_variable(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "LIMIT <- 10\nf <- function() LIMIT\n"})
    idx = build_index(root)
    infos = {s.symbol: s.kind for s in idx.documents[0].symbols}
    assert infos["scip-r cran pkg 1.0.0 LIMIT."] == scip.SymbolInformation.Kind.Variable
    assert symbols_at(idx, "R/a.R")["scip-r cran pkg 1.0.0 LIMIT."] == [[0, 0, 5], [1, 16, 21]]


def test_lambda_shorthand_and_dots(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- \\(x, ...) x + 1\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    assert ([0, 0, 1], "scip-r cran pkg 1.0.0 f().", DEF) in occ
    assert ([0, 7, 8], "local 0", DEF) in occ
    assert ([0, 15, 16], "local 0", 0) in occ
    assert not any(sym == "local 1" for _, sym, _ in occ)  # `...` is not a local


def test_locals_shadow_package_symbols(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "mean <- function(x) 1\nf <- function(mean) mean(2)\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    # inside f, `mean` is the parameter (local 1; local 0 is the first
    # function's x), not the package-level function
    assert ([1, 14, 18], "local 1", DEF) in occ
    assert ([1, 20, 24], "local 1", 0) in occ
    assert idx.external_symbols == []


def test_unresolved_bare_identifier_reads_are_skipped(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function() pi + letters\n"})
    idx = build_index(root)
    assert [sym for _, sym, _ in occurrences(idx, "R/a.R")] == ["scip-r cran pkg 1.0.0 f()."]
    assert idx.external_symbols == []


def test_for_loop_variable_is_local(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(n) {\n  for (i in seq_len(n)) print(i)\n}\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    assert ([1, 7, 8], "local 1", DEF) in occ  # i
    assert ([1, 30, 31], "local 1", 0) in occ  # print(i)
    assert ([1, 20, 21], "local 0", 0) in occ  # seq_len(n)
    ext = sorted(s.symbol for s in idx.external_symbols)
    assert ext == ["scip-r cran base . print().", "scip-r cran base . seq_len()."]


def test_parameter_defaults_see_earlier_parameters(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x, n = length(x)) n\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    assert ([0, 28, 29], "local 0", 0) in occ  # length(x) -> x is the parameter


def test_nested_functions_get_their_own_scope(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x) {\n  g <- function(x) x\n  g(x)\n}\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    # outer x = local 0, g = local 1, inner x = local 2
    assert ([1, 16, 17], "local 2", DEF) in occ
    assert ([1, 19, 20], "local 2", 0) in occ
    assert ([2, 2, 3], "local 1", 0) in occ
    assert ([2, 4, 5], "local 0", 0) in occ


def test_triple_colon_and_method_call_targets(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x) {\n  pkg:::hidden(x)\n  x$method(1)\n}\n"})
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg . hidden()."] == [[1, 8, 14]]
    # x$method(1): the callee is an extract expression, so `x` is a local
    # read and nothing is guessed.
    assert [s.symbol for s in idx.external_symbols] == ["scip-r cran pkg . hidden()."]
    assert [2, 2, 3] in occ["local 0"]


def test_non_utf8_source_does_not_crash(make_package: MakePackage) -> None:
    root = make_package({})
    (root / "R").mkdir()
    (root / "R" / "latin1.R").write_bytes(b"f <- function() 'caf\xe9'\n")
    idx = build_index(root)
    assert [s.symbol for s in idx.documents[0].symbols] == ["scip-r cran pkg 1.0.0 f()."]


def test_empty_package(make_package: MakePackage) -> None:
    root = make_package({})
    idx = build_index(root)
    assert list(idx.documents) == []
    assert list(idx.external_symbols) == []


# --- regressions from review -------------------------------------------------


def test_named_argument_keys_are_not_reads(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x, na.rm) mean(x, na.rm = na.rm)\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    # `na.rm =` (cols 32-37) is a label; only the value `na.rm` (cols 40-45) is a read
    assert ([0, 40, 45], "local 1", 0) in occ
    assert not any(rng == [0, 32, 37] for rng, _, _ in occ)
    assert sum(1 for _, sym, _ in occ if sym == "local 1") == 2  # def + one read


def test_named_argument_key_matching_top_level_symbol(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "data <- 1\nf <- function() g(data = 2)\n"})
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 data."] == [[0, 0, 4]]  # definition only


def test_namespace_reference_without_call(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x) sapply(x, stats::median)\ng <- utils::head\n"})
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran stats . median."] == [[0, 34, 40]]
    assert occ["scip-r cran utils . head."] == [[1, 12, 16]]
    ext = {s.symbol: s for s in idx.external_symbols}
    assert ext["scip-r cran stats . median."].kind == scip.SymbolInformation.Kind.UnspecifiedKind
    assert ext["scip-r cran stats . median."].documentation == ["stats::median"]


def test_namespace_call_and_value_forms_are_distinct_symbols(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x) { stats::sd(x); stats::sd }\n"})
    idx = build_index(root)
    assert [s.symbol for s in idx.external_symbols] == [
        "scip-r cran stats . sd().",
        "scip-r cran stats . sd.",
    ]


def test_chained_top_level_assignment_defines_every_name(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "a <- b <- function() 1\nc <- d <- 2\n"})
    idx = build_index(root)
    infos = {s.symbol: s.kind for s in idx.documents[0].symbols}
    assert infos == {
        "scip-r cran pkg 1.0.0 a().": scip.SymbolInformation.Kind.Function,
        "scip-r cran pkg 1.0.0 b().": scip.SymbolInformation.Kind.Function,
        "scip-r cran pkg 1.0.0 c.": scip.SymbolInformation.Kind.Variable,
        "scip-r cran pkg 1.0.0 d.": scip.SymbolInformation.Kind.Variable,
    }
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 b()."] == [[0, 5, 6]]
    assert occ["scip-r cran pkg 1.0.0 d."] == [[1, 5, 6]]


def test_chained_assignment_inside_function(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function() { a <- b <- 1; a + b }\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    assert ([0, 18, 19], "local 0", DEF) in occ
    assert ([0, 23, 24], "local 1", DEF) in occ
    assert ([0, 35, 36], "local 1", 0) in occ


def test_right_assignment(make_package: MakePackage) -> None:
    root = make_package(
        {"R/a.R": "1 -> LIMIT\n(function(x) x) ->> g\nf <- function() { 2 -> y; y }\n"}
    )
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 LIMIT."] == [[0, 5, 10]]
    assert occ["scip-r cran pkg 1.0.0 g."] == [[1, 20, 21]]  # rhs is a paren expr, so "value"
    assert occ["local 0"] == [[1, 10, 11], [1, 13, 14]]  # the lambda's x
    assert occ["local 1"] == [[2, 23, 24], [2, 26, 27]]  # y inside f


def test_string_literal_lhs_defines_operator_and_replacement_functions(
    make_package: MakePackage,
) -> None:
    root = make_package(
        {"R/a.R": "\"%+%\" <- function(a, b) paste(a, b)\n'foo<-' <- function(x, value) x\n"}
    )
    idx = build_index(root)
    infos = {s.symbol: s.kind for s in idx.documents[0].symbols}
    assert infos == {
        "scip-r cran pkg 1.0.0 %+%().": scip.SymbolInformation.Kind.Function,
        "scip-r cran pkg 1.0.0 foo<-().": scip.SymbolInformation.Kind.Function,
    }
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 %+%()."] == [[0, 0, 5]]  # whole string literal incl. quotes


def test_destructuring_targets_are_walked_not_defined(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x, i) { x[i] <- 1; names(x) <- 'a'; x }\n"})
    idx = build_index(root)
    occ = occurrences(idx, "R/a.R")
    # x and i are read inside the targets; no new locals are created
    assert ([0, 22, 23], "local 0", 0) in occ
    assert ([0, 24, 25], "local 1", 0) in occ
    assert not any(sym == "local 2" for _, sym, _ in occ)
    assert [s.symbol for s in idx.external_symbols] == ["scip-r cran base . names()."]
