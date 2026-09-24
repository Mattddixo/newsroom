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
    ],
)
def test_outlet_validation(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "outlets.yaml"
    path.write_text(body)
    with pytest.raises(ConfigError, match=message):
        load_outlets(path)


def test_tag_matching() -> None:
    path = Path(__file__).parent / "fixtures" / "tags.yaml"
    tagger = Tagger(load_tags(path))
    assert tagger.match("Housing crisis deepens") == {"housing": "housing"}
    assert tagger.match("Crise du LOGEMENT à Québec") == {"housing": "logement"}
    assert tagger.match("Warehousing jobs") == {}  # whole words only
    assert tagger.match("Bank of  Canada holds interest   rate") == {"economy": "interest rate"}
    assert tagger.match("Élection partielle") == {"elections": "election"}
    assert set(tagger.match("Election brings housing pledge")) == {"elections", "housing"}


def test_normalize_text() -> None:
    assert normalize_text("  Élection   PARTIELLE ") == "election partielle"
