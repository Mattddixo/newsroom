from __future__ import annotations

from pathlib import Path

import pytest

from newsroom.config import ConfigError, load_outlets, load_tags, normalize_text
from newsroom.services.tagging import Tagger

ROOT = Path(__file__).resolve().parent.parent


def test_shipped_config_is_valid() -> None:
    outlets = load_outlets(ROOT / "config" / "outlets.yaml")
    tags = load_tags(ROOT / "config" / "tags.yaml")
    assert len(outlets) > 10
    assert len({o.domain for o in outlets}) == len(outlets)
    assert tags


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("outlets: {}", "outlets:"),
        ("outlets:\n  - {domain: 'not a domain', name: X, country: CA, language: en}", "domain"),
        ("outlets:\n  - {domain: a.ca, name: X, country: CAN, language: en}", "country"),
        ("outlets:\n  - {domain: a.ca, name: '', country: CA, language: en}", "name"),
        (
            "outlets:\n  - {domain: a.ca, name: X, country: CA, language: en}\n"
            "  - {domain: a.ca, name: Y, country: CA, language: en}",
            "duplicate",
        ),
        ("outlets: [", "invalid YAML"),
        ("outlets:\n  - {domain: a.ca, name: X, country: CA, language: en, also: b.ca}", "list"),
        ("outlets:\n  - {domain: a.ca, name: X, country: CA, language: en, feeds: x}", "list"),
        (
            "outlets:\n  - {domain: a.ca, name: X, country: CA, language: en,"
            " feeds: [javascript:alert(1)]}",
            "feed URL",
        ),
        (
            "outlets:\n  - {domain: a.ca, name: X, country: CA, language: en, note: '"
            + "x" * 301
            + "'}",
            "300",
        ),
        ("outlets:\n  - {domain: a.ca, name: X, country: CA, language: en, also: [bad]}", "also"),
        ("outlets:\n  - {domain: a.ca, name: X, country: CA, language: en, also: [a.ca]}", "twice"),
        (
            "outlets:\n  - {domain: a.ca, name: X, country: CA, language: en, also: [b.ca]}\n"
            "  - {domain: c.ca, name: Y, country: CA, language: en, also: [b.ca]}",
            "twice",
        ),
        (
            "outlets:\n  - {domain: a.ca, name: X, country: CA, language: en, also: [b.ca]}\n"
            "  - {domain: b.ca, name: Y, country: CA, language: en}",
            "duplicate",
        ),
    ],
)
def test_outlet_validation(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "outlets.yaml"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_outlets(path)


def test_tags_from_gdelt_themes_and_outlet_sections() -> None:
    path = Path(__file__).parent / "fixtures" / "tags.yaml"
    tagger = Tagger(load_tags(path))
    # GDELT: the tag's themes must come up often enough in the text (3; elections 2)
    found = tagger.from_themes({"ECON_HOUSING_PRICES": 2, "WB_2187_RENTAL_HOUSING": 1})
    assert found == {"housing": "GDELT themes: ECON_HOUSING_PRICES (2), WB_2187_RENTAL_HOUSING (1)"}
    assert tagger.from_themes({"ECON_INFLATION": 2}) == {}  # a passing mention
    assert tagger.from_themes({"ELECTION": 2}) == {"elections": "GDELT themes: ELECTION (2)"}
    assert tagger.from_themes({"TAX_FNCACT_MAYOR": 9}) == {}  # not a tag's theme
    # the outlet's own labels: whole labels, parts of a path, any case or accent
    assert tagger.from_sections(["News > Housing"]) == {"housing": "Outlet's section: Housing"}
    assert tagger.from_sections(["ECONOMIE"]) == {"economy": "Outlet's section: ECONOMIE"}
    assert tagger.from_sections(["Housing market outlook"]) == {}  # not a whole label
    # GDELT first; the outlet's sections only when GDELT's themes give no tag
    assert set(tagger.match({"ELECTION": 5}, ["Housing"])) == {"elections"}
    assert set(tagger.match({"ELECTION": 1}, ["Housing"])) == {"housing"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("tags:\n  x:\n    label: X\n    keywords: [a]\n", "no longer used"),
        ("tags:\n  x:\n    label: X\n", "needs gdelt themes or sections"),
        ("tags:\n  x:\n    label: X\n    gdelt: [climate change]\n", "not a GDELT theme"),
        ("tags:\n  x:\n    label: X\n    gdelt: [ELECTION]\n    gdelt_min_mentions: 0\n", ">= 1"),
    ],
)
def test_tag_validation(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "tags.yaml"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_tags(path)


def test_normalize_text() -> None:
    assert normalize_text("  Élection   PARTIELLE ") == "election partielle"


def test_other_domains(tmp_path: Path) -> None:
    path = tmp_path / "outlets.yaml"
    path.write_text(
        "outlets:\n  - {domain: ms.now, name: MS NOW, country: US, language: en,"
        " also: [MSNBC.com]}\n  - {domain: b.ca, name: B, country: CA, language: en}\n"
    )
    first, second = load_outlets(path)
    assert first.also == ("msnbc.com",) and second.also == ()
