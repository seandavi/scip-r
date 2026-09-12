from __future__ import annotations

import pytest

from scipr.symbols import descriptor_for, method_descriptor, parse_symbol, symbol_string


def test_symbol_string_shape() -> None:
    assert symbol_string("pkg", "1.2.3", "fn().") == "scip-r cran pkg 1.2.3 fn()."


@pytest.mark.parametrize(
    ("name", "is_function", "expected"),
    [("f", True, "f()."), ("obj", False, "obj."), ("na.rm", False, "na.rm.")],
)
def test_descriptor_for(name: str, is_function: bool, expected: str) -> None:
    assert descriptor_for(name, is_function=is_function) == expected


def test_parse_package_function() -> None:
    p = parse_symbol("scip-r cran testpkg 0.1.0 zscore().")
    assert not p.is_local
    assert (p.scheme, p.manager, p.package, p.version) == ("scip-r", "cran", "testpkg", "0.1.0")
    assert p.descriptor == "zscore()."
    assert p.name == "zscore"
    assert p.is_function
    assert not p.is_external


def test_parse_package_value() -> None:
    p = parse_symbol("scip-r cran testpkg 0.1.0 CONST.")
    assert p.name == "CONST"
    assert not p.is_function


def test_parse_external_has_unknown_version() -> None:
    p = parse_symbol("scip-r cran stats . sd().")
    assert p.is_external
    assert p.package == "stats"
    assert p.name == "sd"


def test_parse_local() -> None:
    p = parse_symbol("local 7")
    assert p.is_local
    assert p.package is None
    assert p.name is None
    assert not p.is_function
    assert not p.is_external


def test_parse_unknown_shape_does_not_raise() -> None:
    p = parse_symbol("garbage")
    assert p.raw == "garbage"
    assert not p.is_local
    assert p.scheme is None


def test_parse_descriptor_with_spaces_keeps_tail() -> None:
    # Descriptors are the 5th field onwards; anything after is kept intact.
    p = parse_symbol("scip-r cran pkg 1.0 weird name().")
    assert p.descriptor == "weird name()."
    assert p.name == "weird name"


def test_parse_s4_method_descriptor() -> None:
    p = parse_symbol("scip-r cran pkg 1.0 width(Interval).")
    assert p.name == "width"
    assert p.disambiguator == "Interval"
    assert p.is_function and p.is_method and not p.is_class


def test_parse_multi_signature_method() -> None:
    p = parse_symbol("scip-r cran pkg 1.0 combine(A,B).")
    assert (p.name, p.disambiguator) == ("combine", "A,B")


def test_parse_class_descriptor() -> None:
    p = parse_symbol("scip-r cran pkg 1.0 Interval#")
    assert p.name == "Interval"
    assert p.is_class and not p.is_function and not p.is_method
    assert p.disambiguator is None


def test_plain_function_has_no_disambiguator() -> None:
    p = parse_symbol("scip-r cran pkg 1.0 f().")
    assert p.disambiguator is None and not p.is_method and not p.is_class


def test_method_and_class_descriptor_helpers() -> None:
    from scipr.symbols import class_descriptor, method_descriptor

    assert method_descriptor("show", ["Foo"]) == "show(Foo)."
    assert method_descriptor("m", ("A", "B")) == "m(A,B)."
    assert class_descriptor("Foo") == "Foo#"


def test_symbol_string_manager_keyword() -> None:
    assert (
        symbol_string("p", "1", "f().", manager="bioconductor") == "scip-r bioconductor p 1 f()."
    )
    assert symbol_string("p", ".", "f().", manager=".") == "scip-r . p . f()."


def test_parse_member_descriptors() -> None:
    m = parse_symbol("scip-r cran pkg 1.0 Counter#add().")
    assert (m.owner, m.name, m.qualified_name) == ("Counter", "add", "Counter#add")
    assert m.is_member and m.is_function and not m.is_method and not m.is_class
    f = parse_symbol("scip-r cran pkg 1.0 Counter#n.")
    assert (f.owner, f.name, f.is_function, f.is_member) == ("Counter", "n", False, True)
    assert parse_symbol("scip-r cran pkg 1.0 Foo#").owner is None


def test_method_descriptor_drops_trailing_any() -> None:
    from scipr.symbols import member_descriptor, normalize_signature

    assert method_descriptor("merge", ["A", "ANY"]) == "merge(A)."
    assert method_descriptor("merge", ["ANY", "B"]) == "merge(ANY,B)."
    assert method_descriptor("show", ["ANY"]) == "show(ANY)."
    assert normalize_signature(["A", "ANY", "ANY"]) == ("A",)
    assert member_descriptor("C", "m", is_function=True) == "C#m()."
    assert member_descriptor("C", "f", is_function=False) == "C#f."


def test_unknown_manager_is_external_marker() -> None:
    p = parse_symbol("scip-r . stats . sd().")
    assert p.manager == "." and p.is_external
