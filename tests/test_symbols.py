from __future__ import annotations

import pytest

from scipr.symbols import descriptor_for, parse_symbol, symbol_string


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
