"""English is the product default; Dutch remains an explicit safe locale."""

import json
from unittest.mock import Mock

import pytest

from expansion_hunter import i18n
from expansion_hunter.analog import FactorQuality
from expansion_hunter.cycle import CyclePhase
from expansion_hunter.discovery_models import DiscoveryStage, Freshness
from expansion_hunter.fast_models import FastStage
from expansion_hunter.live_models import FeedConnection
from expansion_hunter.macro_models import MacroMode, MacroQuality, ReleaseKind
from expansion_hunter.models import Regime, Stage, Status
from expansion_hunter.pattern_engine import PATTERN_NAMES
from expansion_hunter.setup_library import SETUP_DEFINITIONS


@pytest.fixture(autouse=True)
def clean_catalog_cache():
    i18n._load_catalog.cache_clear()
    yield
    i18n._load_catalog.cache_clear()


def test_default_english_catalog_covers_every_existing_dutch_key():
    english = i18n.catalog()
    dutch = i18n.catalog("nl")
    assert i18n.DEFAULT_LOCALE == "en"
    assert english["language"] == "en"
    assert set(english) == {"language", "labels", "states"}
    for namespace in ("labels", "states"):
        assert dutch[namespace].keys() <= english[namespace].keys()
        for key, text in english[namespace].items():
            assert isinstance(text, str) and text.strip()
            assert "<" not in text and ">" not in text
            assert "_" not in text
            assert text != key, f"raw internal code exposed: {key}"
    assert json.loads(json.dumps(english, ensure_ascii=False)) == english


@pytest.mark.parametrize("enum", [FastStage, DiscoveryStage, Freshness, FeedConnection, FactorQuality,
                                 Regime, Stage, Status, CyclePhase, MacroMode, MacroQuality, ReleaseKind])
def test_every_actual_enum_has_a_readable_explicit_english_translation(enum):
    mapping = i18n.catalog()["states"]
    for member in enum:
        assert member.value in mapping, f"missing {enum.__name__}.{member.name}"
        assert i18n.translate(member) == mapping[member.value]
        assert i18n.translate(member) != "Unknown"


def test_actual_pattern_and_setup_names_are_explicitly_translated():
    states = i18n.catalog()["states"]
    for name in tuple(PATTERN_NAMES) + tuple(SETUP_DEFINITIONS):
        assert name in states
        assert i18n.translate(name) != "Unknown"


@pytest.mark.parametrize("key", ["best_long", "best_short", "no_edge", "rookie", "pro", "war_room",
                                 "chart", "layers", "current_timeframe_fib", "higher_timeframe_confluence",
                                 "all_fibonacci", "entry_zone", "first_target", "second_target",
                                 "three_conditional_steps", "status", "expected_path", "setup_quality",
                                 "entry_quality", "session", "paper_journal", "invalid_below", "invalid_above"])
def test_compact_product_labels_exist_in_english(key):
    assert i18n.translate(key, "labels") != "Unknown"


@pytest.mark.parametrize("key", ["BREAKDOWN_PRESSURE", "BREAKDOWN_ATTEMPT", "1M_BREAKDOWN_CONFIRMED",
                                 "5M_ACCEPTED_DOWN", "15M_EXPANSION_CONFIRMED_DOWN", "FAILED_BREAKDOWN",
                                 "ACTIVE_RETRACEMENT", "IMPULSE_EXTENSION", "AT_SWING_EXTREME", "ORIGIN_LOST",
                                 "UNVALIDATED_GEOMETRY", "FALLING", "BOTTOMING", "TURNING_UP", "EARLY_UPSWING",
                                 "UPSWING", "LOSING_MOMENTUM", "TOPPING", "TURNING_DOWN", "EARLY_DOWNSWING",
                                 "DOWNSWING", "LONG_SETUP", "SHORT_SETUP", "WAIT", "NO_TRADE",
                                 "RANGE_REVIEW", "INVALIDATION_REVIEW", "OPPOSITE_BOUNDARY_RETEST",
                                 "OPPOSITE_CONTINUATION_REVIEW"])
def test_symmetric_short_fibonacci_and_swing_codes_are_known_in_both_languages(key):
    assert i18n.translate(key) != "Unknown"
    assert i18n.translate(key, locale="nl") != "Onbekend"


@pytest.mark.parametrize("key", [None, "", "MADE_UP_BULLISH_CERTAINTY", "SYNTHETIC_PROBABILITY_90", 42])
def test_missing_unknown_codes_remain_locale_specific_unknown(key):
    assert i18n.translate(key) == "Unknown"
    assert i18n.translate(key, "labels") == "Unknown"
    assert i18n.translate(key, locale="nl") == "Onbekend"
    assert i18n.translate(key, "labels", locale="nl") == "Onbekend"


def test_default_language_is_english_and_dutch_selection_still_works():
    assert i18n.translate(" bullish ") == "Bullish scenario"
    assert i18n.translate(" CHoCH ") == "Change of character"
    assert i18n.translate("Setup_Quality", "labels") == "Setup quality"
    assert i18n.translate("BULLISH", "unsupported_namespace") == "Unknown"
    assert i18n.translate("BULLISH", locale="nl") == "Stijgend scenario"


def test_both_languages_remain_cached_without_cross_language_mutation(monkeypatch):
    resources = {}
    for locale, unknown in (("en", "Unknown"), ("nl", "Onbekend")):
        resource = Mock()
        resource.read_text.return_value = json.dumps({"language": locale, "labels": {"unknown": unknown},
                                                     "states": {"UNKNOWN": unknown}})
        resources[f"i18n.{locale}.json"] = resource
    package = Mock()
    package.joinpath.side_effect = lambda directory, filename: resources[filename]
    loader = Mock(return_value=package)
    monkeypatch.setattr(i18n, "files", loader)
    for _ in range(3):
        assert i18n.translate("UNKNOWN") == "Unknown"
        assert i18n.translate("UNKNOWN", locale="nl") == "Onbekend"
    altered = i18n.catalog()
    altered["states"]["UNKNOWN"] = "Guaranteed winner"
    altered["labels"].clear()
    assert i18n.catalog()["states"]["UNKNOWN"] == "Unknown"
    assert i18n.catalog("nl")["states"]["UNKNOWN"] == "Onbekend"
    assert loader.call_count == 2
    assert package.joinpath.call_count == 2
    for resource in resources.values():
        resource.read_text.assert_called_once_with(encoding="utf-8")


@pytest.mark.parametrize("locale", ["de", "en-GB", "nl-NL", "../settings", "/etc/passwd", "", None])
def test_unsupported_locale_never_loads_an_arbitrary_resource(monkeypatch, locale):
    loader = Mock()
    monkeypatch.setattr(i18n, "files", loader)
    with pytest.raises(ValueError, match="supported locales"):
        i18n.catalog(locale)
    loader.assert_not_called()


def test_semantic_research_warnings_preserve_hypothesis_and_data_honesty():
    states = i18n.catalog()["states"]
    for phase in CyclePhase:
        if phase != CyclePhase.NEUTRAL_UNCERTAIN:
            assert "hypothesis" in states[phase.value].lower()
    assert "not probabilities" in i18n.translate("uncalibrated_notice", "labels")
    assert "not forecasts" in i18n.translate("forward_three_notice", "labels")
    assert "not a forecast" in i18n.translate("historical_research", "labels").lower()
    assert "do not identify" in i18n.translate("whale_flow_notice", "labels")
    assert "on-chain" in i18n.translate("whale_flow_notice", "labels").lower()
    assert "does not prove" in i18n.translate("session_notice", "labels")
    assert all("%" not in text for text in states.values())
    assert "support" in states["BREAKDOWN_PRESSURE"].lower()
    assert "breakdown" in states["1M_BREAKDOWN_CONFIRMED"].lower()
    assert "downside" in states["5M_ACCEPTED_DOWN"].lower()
    assert "open" in states["BREAKDOWN_ATTEMPT"].lower()
