"""Regression test: Home Assistant does not resolve %key: cross-references for custom
integrations, so any such references render as raw garbage in the UI. This test asserts
that no such references exist in either strings.json or its en.json translation, catching
any regressions early."""

from __future__ import annotations

import json
import pathlib


def test_no_key_cross_references_in_strings_json() -> None:
    """strings.json must contain no %key: cross-references."""
    strings_path = pathlib.Path(__file__).parent.parent / "custom_components" / "kibble" / "strings.json"
    content = strings_path.read_text()
    assert "%key:" not in content, "strings.json contains %key: cross-references that will not resolve"


def test_no_key_cross_references_in_en_json() -> None:
    """translations/en.json must contain no %key: cross-references."""
    en_path = (
        pathlib.Path(__file__).parent.parent
        / "custom_components"
        / "kibble"
        / "translations"
        / "en.json"
    )
    content = en_path.read_text()
    assert "%key:" not in content, "translations/en.json contains %key: cross-references that will not resolve"


def test_strings_and_en_json_are_identical() -> None:
    """The en.json translation file must be identical to strings.json."""
    strings_path = pathlib.Path(__file__).parent.parent / "custom_components" / "kibble" / "strings.json"
    en_path = (
        pathlib.Path(__file__).parent.parent
        / "custom_components"
        / "kibble"
        / "translations"
        / "en.json"
    )
    strings = json.loads(strings_path.read_text())
    en = json.loads(en_path.read_text())
    assert strings == en, "strings.json and translations/en.json must be identical JSON"
