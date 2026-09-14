from __future__ import annotations

from pathlib import Path

import pytest

from scipr import build_index
from scipr import scip_pb2 as scip
from scipr.parser import (
    EXPORTED_DOC,
    GUESSED_NOTE,
    IMPORTED_NOTE,
    S3_GENERIC_NOTE,
    collect_top_level_symbols,
    find_r_files,
    index_arguments,
    read_description,
)
from tests.conftest import MakePackage, occurrences, symbols_at

DEF = scip.SymbolRole.Definition


# --- DESCRIPTION / file discovery -------------------------------------------


def test_read_description(make_package: MakePackage) -> None:
    root = make_package({}, name="mypkg", version="2.3.4")
    info = read_description(root)
    assert (info.name, info.version, info.manager) == ("mypkg", "2.3.4", "cran")


def test_read_description_missing_file_falls_back_to_dirname(make_package: MakePackage) -> None:
    root = make_package({}, name="loose", description=False)
    info = read_description(root)
    assert (info.name, info.version) == ("loose", "0.0.0")


def test_read_description_missing_fields(make_package: MakePackage) -> None:
    root = make_package({}, name="onlyname", version=None)
    info = read_description(root)
    assert (info.name, info.version) == ("onlyname", "0.0.0")


def test_find_r_files_only_directly_under_R_dir_and_sorted(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/zeta.R": "",
            "R/alpha.r": "",
            "R/sub/nested.R": "",  # R CMD INSTALL ignores R/ subdirectories
            "R/notes.txt": "",
            "tests/testthat/test-x.R": "",
            "vignettes/v.R": "",
        }
    )
    rel = [p.relative_to(root).as_posix() for p in find_r_files(root)]
    assert rel == ["R/alpha.r", "R/zeta.R"]


def test_find_r_files_without_R_dir_searches_whole_tree(make_package: MakePackage) -> None:
    root = make_package({"a.R": "", "deep/b.R": ""}, description=False)
    rel = [p.relative_to(root).as_posix() for p in find_r_files(root)]
    assert rel == ["a.R", "deep/b.R"]  # loose scripts: recursive


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
        "R/classes.R",
        "R/pipeline.R",
        "R/stats_helpers.R",
    ]
    assert all(d.language == "R" for d in testpkg_index.documents)


def test_tool_version_override(testpkg_dir: Path) -> None:
    idx = build_index(testpkg_dir, tool_version="9.9.9")
    assert idx.metadata.tool_info.version == "9.9.9"


def test_relative_paths_are_posix(make_package: MakePackage) -> None:
    root = make_package({"sub/dir/f.R": "f <- function() 1\n"}, description=False)
    idx = build_index(root)
    assert idx.documents[0].relative_path == "sub/dir/f.R"


# --- resolution on the fixture package --------------------------------------


def test_cross_file_calls_resolve_to_package_symbols(testpkg_index: scip.Index) -> None:
    occ = symbols_at(testpkg_index, "R/pipeline.R")
    assert occ["scip-r cran testpkg 0.1.0 zscore()."] == [[2, 7, 13]]
    assert occ["scip-r cran testpkg 0.1.0 winsorize()."] == [[4, 9, 18]]


def test_definition_role_and_symbol_information(testpkg_index: scip.Index) -> None:
    doc = testpkg_index.documents[2]
    assert doc.relative_path == "R/stats_helpers.R"
    infos = {s.symbol: s for s in doc.symbols}
    assert set(infos) == {
        "scip-r cran testpkg 0.1.0 zscore().",
        "scip-r cran testpkg 0.1.0 winsorize().",
    }
    z = infos["scip-r cran testpkg 0.1.0 zscore()."]
    assert z.kind == scip.SymbolInformation.Kind.Function
    assert z.documentation == ["zscore <- function(x, na.rm = TRUE) {", EXPORTED_DOC]
    defs = [
        o for o in doc.occurrences if o.symbol_roles & DEF and not o.symbol.startswith("local")
    ]
    assert [(list(o.range), o.symbol) for o in defs] == [
        ([1, 0, 6], "scip-r cran testpkg 0.1.0 zscore()."),
        ([8, 0, 9], "scip-r cran testpkg 0.1.0 winsorize()."),
    ]


def test_namespaced_calls_become_external_refs(testpkg_index: scip.Index) -> None:
    occ = symbols_at(testpkg_index, "R/stats_helpers.R")
    assert occ["scip-r . stats . quantile()."] == [[9, 15, 23]]


def test_unresolved_calls_are_guessed_as_base(testpkg_index: scip.Index) -> None:
    occ = symbols_at(testpkg_index, "R/stats_helpers.R")
    assert occ["scip-r . base . mean()."] == [[2, 8, 12]]
    assert occ["scip-r . stats . sd()."] == [[3, 7, 9]]  # importFrom(stats, sd) in NAMESPACE
    assert occ["scip-r . base . c()."] == [[8, 33, 34]]


def test_external_symbols_sorted_and_documented(testpkg_index: scip.Index) -> None:
    ext = {s.symbol: s for s in testpkg_index.external_symbols}
    assert list(ext) == sorted(ext)
    assert {
        "scip-r . base . mean().",
        "scip-r . stats . sd().",
        "scip-r . stats . quantile().",
        "scip-r . base . print().",
    } <= set(ext)
    assert ext["scip-r . base . mean()."].documentation == [f"base::mean  {GUESSED_NOTE}"]
    assert ext["scip-r . stats . quantile()."].documentation == ["stats::quantile"]
    assert ext["scip-r . stats . sd()."].documentation == [f"stats::sd  {IMPORTED_NOTE}"]
    assert ext["scip-r . base . print()."].documentation == [f"base::print  {S3_GENERIC_NOTE}"]
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
    sh = [
        (p["line"], p["character"], p["name"], p["enclosing"])
        for p in positions
        if p["file"] == "R/stats_helpers.R"
    ]
    assert sh == [
        (2, 8, "mean", "zscore"),
        (4, 2, "structure", "zscore"),
        (8, 33, "c", "winsorize"),
    ]
    top = {p["name"] for p in positions if p["file"] == "R/classes.R" and p["enclosing"] is None}
    assert {"representation", "standardGeneric", "list"} <= top  # setClass etc. are imports
    assert "cat" not in top  # inside print.zresult, so enclosing is set
    assert {p["enclosing"] for p in positions if p["name"] == "cat"} == {"print.zresult"}
    assert {p["enclosing"] for p in positions if p["name"] == "invisible"} == {
        "print.zresult",
        "Counter$add",
    }


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
    assert ext == ["scip-r . base . print().", "scip-r . base . seq_len()."]


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
    assert occ["scip-r . pkg . hidden()."] == [[1, 8, 14]]
    # x$method(1): the callee is an extract expression, so `x` is a local
    # read and nothing is guessed.
    assert [s.symbol for s in idx.external_symbols] == ["scip-r . pkg . hidden()."]
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
    assert occ["scip-r . stats . median."] == [[0, 34, 40]]
    assert occ["scip-r . utils . head."] == [[1, 12, 16]]
    ext = {s.symbol: s for s in idx.external_symbols}
    assert ext["scip-r . stats . median."].kind == scip.SymbolInformation.Kind.UnspecifiedKind
    assert ext["scip-r . stats . median."].documentation == ["stats::median"]


def test_namespace_call_and_value_forms_are_distinct_symbols(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x) { stats::sd(x); stats::sd }\n"})
    idx = build_index(root)
    assert [s.symbol for s in idx.external_symbols] == [
        "scip-r . stats . sd().",
        "scip-r . stats . sd.",
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
    assert [s.symbol for s in idx.external_symbols] == ["scip-r . base . names()."]


# --- S4 / R6 definitions ------------------------------------------------------


def test_s4_class_generic_and_method_symbols(testpkg_index: scip.Index) -> None:
    doc = testpkg_index.documents[0]
    assert doc.relative_path == "R/classes.R"
    infos = {s.symbol: s for s in doc.symbols}
    K = scip.SymbolInformation.Kind
    assert infos["scip-r cran testpkg 0.1.0 Interval#"].kind == K.Class
    assert infos["scip-r cran testpkg 0.1.0 width()."].kind == K.Function
    assert infos["scip-r cran testpkg 0.1.0 width(Interval)."].kind == K.Method
    assert infos["scip-r cran testpkg 0.1.0 Counter#"].kind == K.Class
    assert infos["scip-r cran testpkg 0.1.0 Counter."].kind == K.Variable
    occ = symbols_at(testpkg_index, "R/classes.R")
    assert occ["scip-r cran testpkg 0.1.0 Interval#"] == [[1, 9, 19]]  # the string literal
    assert occ["scip-r cran testpkg 0.1.0 width()."] == [[3, 11, 18]]
    assert occ["scip-r cran testpkg 0.1.0 width(Interval)."] == [[5, 10, 17]]
    assert occ["scip-r cran testpkg 0.1.0 Counter#"] == [[14, 19, 28]]


def test_s4_method_implements_local_generic(testpkg_index: scip.Index) -> None:
    doc = testpkg_index.documents[0]
    method = next(s for s in doc.symbols if s.symbol.endswith("width(Interval)."))
    assert [(r.symbol, r.is_implementation) for r in method.relationships] == [
        ("scip-r cran testpkg 0.1.0 width().", True)
    ]
    generic = next(s for s in doc.symbols if s.symbol.endswith(" width()."))
    assert list(generic.relationships) == []


def test_generic_calls_resolve_to_setgeneric_definition(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'setGeneric("area", function(x) standardGeneric("area"))\n'
                "f <- function(s) area(s)\n"
            )
        }
    )
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 area()."] == [[0, 11, 17], [1, 17, 21]]


def test_setmethod_signature_forms(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'setMethod("show", "Foo", function(object) NULL)\n'
                'setMethod("combine", c("A", "B"), function(x, y) NULL)\n'
                'setMethod("merge", signature(x = "A", y = "ANY"), function(x, y) NULL)\n'
                'methods::setMethod("length", "Foo", function(x) 0L)\n'
                'setMethod(dynamic_name, "Foo", function(x) NULL)\n'
            )
        }
    )
    idx = build_index(root)
    syms = sorted(s.symbol for s in idx.documents[0].symbols)
    assert syms == [
        "scip-r cran pkg 1.0.0 combine(A,B).",
        "scip-r cran pkg 1.0.0 length(Foo).",
        "scip-r cran pkg 1.0.0 merge(A).",  # trailing ANY dropped
        "scip-r cran pkg 1.0.0 show(Foo).",
    ]
    # external generic: no static relationship (scip-r resolve adds it)
    assert all(list(s.relationships) == [] for s in idx.documents[0].symbols)


def test_setrefclass_and_r6class_define_classes(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'Acc <- setRefClass("Account", fields = list(b = "numeric"))\n'
                'P <- R6::R6Class("Person")\n'
            )
        }
    )
    idx = build_index(root)
    infos = {s.symbol: s.kind for s in idx.documents[0].symbols}
    K = scip.SymbolInformation.Kind
    assert infos["scip-r cran pkg 1.0.0 Account#"] == K.Class
    assert infos["scip-r cran pkg 1.0.0 Person#"] == K.Class
    assert infos["scip-r cran pkg 1.0.0 Acc."] == K.Variable


def test_definer_calls_inside_functions_are_not_definitions(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": 'f <- function() setClass("Late")\n'})
    idx = build_index(root)
    assert [s.symbol for s in idx.documents[0].symbols] == ["scip-r cran pkg 1.0.0 f()."]


def test_enclosing_function_in_positions(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                "top()\nf <- function() inner()\ng = function() { h <- function() deep(); h() }\n"
            )
        }
    )
    positions: list = []
    build_index(root, positions_out=positions)
    assert [(p["name"], p["enclosing"]) for p in positions] == [
        ("top", None),
        ("inner", "f"),
        ("deep", "g"),
    ]


# --- review-gap features ---------------------------------------------------------


def test_documents_declare_utf8_byte_offsets(testpkg_index: scip.Index) -> None:
    assert all(
        d.position_encoding == scip.PositionEncoding.UTF8CodeUnitOffsetFromLineStart
        for d in testpkg_index.documents
    )


def test_columns_are_utf8_bytes(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": 'f <- function(x) { "café"; g(x) }\n'})
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    # "café" is 6 bytes with quotes; g sits at byte 28, one past its character column
    assert occ["scip-r . base . g()."] == [[0, 28, 29]]


def test_triple_colon_is_marked_on_the_occurrence(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function() { pkg:::secret(); pkg::open() }\n"})
    idx = build_index(root)
    by_name = {o.symbol: o for o in idx.documents[0].occurrences if "pkg" in o.symbol}
    assert list(by_name["scip-r . pkg . secret()."].override_documentation) == ["pkg:::secret"]
    assert list(by_name["scip-r . pkg . open()."].override_documentation) == []


def test_tool_info_stamps(testpkg_index: scip.Index) -> None:
    stamps = index_arguments(testpkg_index)
    assert stamps["package"] == "testpkg" and stamps["version"] == "0.1.0"
    assert stamps["manager"] == "cran"
    assert "git_commit" in stamps  # the fixture lives inside this repository


def test_manager_inferred_from_biocviews_and_overridable(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function() 1\n"}, name="bpkg")
    (root / "DESCRIPTION").write_text("Package: bpkg\nVersion: 1.0\nbiocViews: Software\n")
    idx = build_index(root)
    assert [s.symbol for s in idx.documents[0].symbols] == ["scip-r bioconductor bpkg 1.0 f()."]
    assert index_arguments(idx)["manager"] == "bioconductor"
    idx = build_index(root, manager="github")
    assert [s.symbol for s in idx.documents[0].symbols] == ["scip-r github bpkg 1.0 f()."]


def test_extra_arguments_are_stamped(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function() 1\n"})
    idx = build_index(root, extra_arguments={"source_sha256": "abc"})
    assert index_arguments(idx)["source_sha256"] == "abc"


def test_exported_flag_from_namespace(testpkg_index: scip.Index) -> None:
    docs = {s.symbol: list(s.documentation) for d in testpkg_index.documents for s in d.symbols}
    assert EXPORTED_DOC in docs["scip-r cran testpkg 0.1.0 zscore()."]
    assert EXPORTED_DOC not in docs["scip-r cran testpkg 0.1.0 print.zresult()."]
    assert EXPORTED_DOC in docs["scip-r cran testpkg 0.1.0 Interval#"]  # exportClasses
    assert EXPORTED_DOC in docs["scip-r cran testpkg 0.1.0 width(Interval)."]  # exportMethods
    assert EXPORTED_DOC not in docs["scip-r cran testpkg 0.1.0 width()."]
    assert EXPORTED_DOC in docs["scip-r cran testpkg 0.1.0 Counter#"]  # via export(Counter)
    assert EXPORTED_DOC in docs["scip-r cran testpkg 0.1.0 Counter#add()."]


def test_export_pattern(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "pub <- function() 1\n.hidden <- function() 2\n"})
    (root / "NAMESPACE").write_text('exportPattern("^[^\\\\.]")\n')
    idx = build_index(root)
    docs = {s.symbol: list(s.documentation) for s in idx.documents[0].symbols}
    assert EXPORTED_DOC in docs["scip-r cran pkg 1.0.0 pub()."]
    assert EXPORTED_DOC not in docs["scip-r cran pkg 1.0.0 .hidden()."]


def test_no_namespace_means_no_export_marks(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function() 1\n"})
    idx = build_index(root)
    assert idx.documents[0].symbols[0].documentation == ["f <- function() 1"]


def test_importfrom_resolves_without_r(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function(x) filter(x) + mutate\n"})
    (root / "NAMESPACE").write_text("importFrom(dplyr, filter, mutate)\n")
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r . dplyr . filter()."] == [[0, 17, 23]]
    assert occ["scip-r . dplyr . mutate."] == [[0, 29, 35]]
    ext = {s.symbol: s.documentation[0] for s in idx.external_symbols}
    assert ext["scip-r . dplyr . filter()."] == f"dplyr::filter  {IMPORTED_NOTE}"
    assert not any("guessed" in d for d in ext.values())


def test_s3method_links_to_local_and_qualified_generics(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                "area <- function(x, ...) UseMethod('area')\n"
                "area.square <- function(x, ...) x$s^2\n"
                "format.square <- function(x, ...) 'sq'\n"
                "summarise_it <- function(x) NULL\n"
            )
        }
    )
    (root / "NAMESPACE").write_text(
        "S3method(area, square)\nS3method(base::format, square)\n"
        "S3method(dplyr::summarise, square, summarise_it)\n"
    )
    idx = build_index(root)
    rels = {
        s.symbol: [r.symbol for r in s.relationships if r.is_implementation]
        for s in idx.documents[0].symbols
    }
    assert rels["scip-r cran pkg 1.0.0 area.square()."] == ["scip-r cran pkg 1.0.0 area()."]
    assert rels["scip-r cran pkg 1.0.0 format.square()."] == ["scip-r . base . format()."]
    assert rels["scip-r cran pkg 1.0.0 summarise_it()."] == ["scip-r . dplyr . summarise()."]
    ext = {s.symbol: s.documentation[0] for s in idx.external_symbols}
    assert ext["scip-r . base . format()."] == "base::format"  # package given, not guessed


def test_r6_members_and_self_resolution(testpkg_index: scip.Index) -> None:
    doc = testpkg_index.documents[0]
    kinds = {s.symbol: s.kind for s in doc.symbols}
    K = scip.SymbolInformation.Kind
    assert kinds["scip-r cran testpkg 0.1.0 Counter#n."] == K.Field
    assert kinds["scip-r cran testpkg 0.1.0 Counter#add()."] == K.Method
    occ = symbols_at(testpkg_index, "R/classes.R")
    assert occ["scip-r cran testpkg 0.1.0 Counter#n."] == [[16, 4, 5], [18, 11, 12], [18, 21, 22]]
    assert occ["scip-r cran testpkg 0.1.0 Counter#add()."] == [[17, 4, 7]]
    defs = {o.symbol: list(o.enclosing_range) for o in doc.occurrences if o.symbol_roles & DEF}
    assert defs["scip-r cran testpkg 0.1.0 Counter#add()."] == [17, 4, 20, 5]
    assert defs["scip-r cran testpkg 0.1.0 Counter#"] == [14, 0, 22, 1]


def test_r6_private_and_active_and_inherit(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'Base <- R6Class("Base", public = list(hello = function() 1))\n'
                'Kid <- R6Class("Kid", inherit = Base,\n'
                "  private = list(secret = 1),\n"
                "  active = list(twice = function() private$secret * 2),\n"
                "  public = list(go = function() { private$secret; self$twice; self$nothere })\n"
                ")\n"
            )
        }
    )
    idx = build_index(root)
    doc = idx.documents[0]
    syms = {s.symbol: s for s in doc.symbols}
    assert "scip-r cran pkg 1.0.0 Kid#secret." in syms
    assert "scip-r cran pkg 1.0.0 Kid#twice()." in syms
    kid = syms["scip-r cran pkg 1.0.0 Kid#"]
    assert [r.symbol for r in kid.relationships] == ["scip-r cran pkg 1.0.0 Base#"]
    occ = symbols_at(idx, "R/a.R")
    assert len(occ["scip-r cran pkg 1.0.0 Kid#secret."]) == 3  # def + 2 reads
    assert len(occ["scip-r cran pkg 1.0.0 Kid#twice()."]) == 2
    assert not any("nothere" in s for s in occ)


def test_reference_class_methods(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'Acc <- setRefClass("Account", fields = list(bal = "numeric"),\n'
                "  methods = list(dep = function(x) { bal <<- bal + x; .self$bal }))\n"
            )
        }
    )
    idx = build_index(root)
    syms = {s.symbol: s.kind for s in idx.documents[0].symbols}
    K = scip.SymbolInformation.Kind
    assert syms["scip-r cran pkg 1.0.0 Account#bal."] == K.Field
    assert syms["scip-r cran pkg 1.0.0 Account#dep()."] == K.Method
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 Account#bal."] == [
        [0, 44, 47],
        [1, 60, 63],
    ]  # def, .self$bal


def test_s4_contains_relationship(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'setClass("Shape", representation("VIRTUAL"))\n'
                'setClass("Square", contains = "Shape", representation(s = "numeric"))\n'
                'setClass("Weird", contains = c("Square", "Unknown"))\n'
            )
        }
    )
    idx = build_index(root)
    rels = {s.symbol: [r.symbol for r in s.relationships] for s in idx.documents[0].symbols}
    assert rels["scip-r cran pkg 1.0.0 Square#"] == ["scip-r cran pkg 1.0.0 Shape#"]
    assert rels["scip-r cran pkg 1.0.0 Weird#"] == ["scip-r cran pkg 1.0.0 Square#"]


def test_setmethod_named_arguments(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'setMethod(f = "show", signature = "Foo", definition = function(object) NULL)\n'
                'setClass(Class = "Foo")\n'
                'setGeneric(name = "go", def = function(x) standardGeneric("go"))\n'
            )
        }
    )
    idx = build_index(root)
    assert sorted(s.symbol for s in idx.documents[0].symbols) == [
        "scip-r cran pkg 1.0.0 Foo#",
        "scip-r cran pkg 1.0.0 go().",
        "scip-r cran pkg 1.0.0 show(Foo).",
    ]


def test_field_access_is_not_a_read_of_a_top_level_name(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "n <- 1\nf <- function(x) { x$n; x@n; x$n$n }\n"})
    idx = build_index(root)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 n."] == [[0, 0, 1]]  # definition only
    assert len(occ["local 0"]) == 4  # x, x, x, x


def test_collate_order_decides_the_winner(make_package: MakePackage) -> None:
    root = make_package({"R/a.R": "f <- function() 'a'\n", "R/b.R": "f <- function() 'b'\n"})
    (root / "DESCRIPTION").write_text("Package: pkg\nVersion: 1.0.0\nCollate: 'b.R' 'a.R'\n")
    idx = build_index(root)
    assert [d.relative_path for d in idx.documents] == ["R/b.R", "R/a.R"]
    f = next(s for d in idx.documents for s in d.symbols if s.symbol.endswith(" f()."))
    assert f.documentation[0] == "f <- function() 'a'"  # a.R is sourced last


def test_dotfiles_are_skipped(make_package: MakePackage) -> None:
    root = make_package(
        {"R/a.R": "f <- function() 1\n", "R/._a.R": "garbage(\n", "R/.hidden/x.R": "g <- 1\n"}
    )
    idx = build_index(root)
    assert [d.relative_path for d in idx.documents] == ["R/a.R"]


def test_enclosing_range_on_top_level_definitions(testpkg_index: scip.Index) -> None:
    doc = testpkg_index.documents[2]  # stats_helpers.R
    z = next(
        o for o in doc.occurrences if o.symbol.endswith(" zscore().") and o.symbol_roles & DEF
    )
    assert list(z.enclosing_range) == [1, 0, 5, 1]
    refs = [o for o in doc.occurrences if not o.symbol_roles & DEF or o.symbol.startswith("local")]
    assert all(not o.enclosing_range for o in refs)


# --- findings from indexing limma ---------------------------------------------------


def test_top_level_assign_defines_a_symbol(make_package: MakePackage) -> None:
    root = make_package(
        {
            "R/a.R": (
                'assign("[.RGList", function(object, i, j, ...) object)\n'
                'assign(x = "helper", value = 42)\n'
                'assign("elsewhere", 1, envir = globalenv())\n'
                'f <- function() { assign("local_only", 1); helper }\n'
            )
        }
    )
    idx = build_index(root)
    doc = idx.documents[0]
    kinds = {s.symbol: s.kind for s in doc.symbols}
    K = scip.SymbolInformation.Kind
    assert kinds["scip-r cran pkg 1.0.0 [.RGList()."] == K.Function
    assert kinds["scip-r cran pkg 1.0.0 helper."] == K.Variable
    assert not any("elsewhere" in s or "local_only" in s for s in kinds)
    occ = symbols_at(idx, "R/a.R")
    assert occ["scip-r cran pkg 1.0.0 [.RGList()."] == [[0, 7, 17]]  # the string literal
    assert occ["scip-r cran pkg 1.0.0 helper."] == [[1, 11, 19], [3, 43, 49]]
    assert len(occ["scip-r . base . assign()."]) == 4  # assign itself is still a call


def test_s3_registered_alias_is_a_function(make_package: MakePackage) -> None:
    root = make_package(
        {"R/a.R": ".setdimnames <- function(x, value) x\n'dimnames<-.MAList' <- .setdimnames\n"}
    )
    (root / "NAMESPACE").write_text('S3method("dimnames<-", MAList)\n')
    idx = build_index(root)
    syms = {s.symbol: s for s in idx.documents[0].symbols}
    alias = syms["scip-r cran pkg 1.0.0 dimnames<-.MAList()."]
    assert alias.kind == scip.SymbolInformation.Kind.Function
    assert [r.symbol for r in alias.relationships] == ["scip-r . base . dimnames<-()."]


# --- parse diagnostics ---------------------------------------------------------------


def test_parse_diagnostics_reported(make_package: MakePackage) -> None:
    from scipr.parser import collect_diagnostics

    root = make_package(
        {
            "R/ok.R": "f <- function(x) x\n",
            "R/bad.R": "h <- 1\ng <- function(x) {\n  x +\n}\nk <- function( { 2\n",
        }
    )
    diags: list = []
    idx = build_index(root, diagnostics_out=diags)
    stamps = index_arguments(idx)
    assert stamps["parse_errors"] == str(len(diags)) and len(diags) >= 2
    assert stamps["parse_error_documents"] == "1"
    assert {d["file"] for d in diags} == {"R/bad.R"}
    assert {d["kind"] for d in diags} <= {"error", "missing"}
    assert diags == sorted(diags, key=lambda d: (d["line"], d["character"]))
    # the parts of a broken file before the damage are still indexed; what
    # tree-sitter swallows into its recovery region is not
    assert "scip-r cran pkg 1.0.0 h." in symbols_at(idx, "R/bad.R")
    assert all(d["line"] >= 1 for d in diags)
    # clean trees short-circuit
    from tree_sitter import Parser

    from scipr.package import R_LANGUAGE

    tree = Parser(R_LANGUAGE).parse(b"f <- function(x) x\n")
    assert collect_diagnostics(tree.root_node, "x.R") == []


def test_clean_package_has_zero_parse_error_stamps(testpkg_index: scip.Index) -> None:
    stamps = index_arguments(testpkg_index)
    assert (stamps["parse_errors"], stamps["parse_error_documents"]) == ("0", "0")
