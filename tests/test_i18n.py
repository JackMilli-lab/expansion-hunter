"""Central Dutch labels cover actual contracts and cannot imply unknown bias."""

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


def test_packaged_catalog_is_dutch_json_with_plain_readable_strings():
    translations = i18n.catalog("nl")
    assert set(translations) == {"language", "labels", "states"}
    assert translations["language"] == "nl"
    assert len(translations["labels"]) > 100
    assert len(translations["states"]) > 250
    for mapping in (translations["labels"], translations["states"]):
        assert all(isinstance(key, str) and isinstance(value, str) and value.strip() for key, value in mapping.items())
        assert all("<" not in value and ">" not in value for value in mapping.values())
    assert json.loads(json.dumps(translations, ensure_ascii=False)) == translations


@pytest.mark.parametrize("enum", [FastStage, DiscoveryStage, Freshness, FeedConnection, FactorQuality,
                                 Regime, Stage, Status, CyclePhase, MacroMode, MacroQuality, ReleaseKind])
def test_every_actual_enum_value_has_an_explicit_dutch_translation(enum):
    mapping = i18n.catalog("nl")["states"]
    for member in enum:
        assert member.value in mapping, f"missing {enum.__name__}.{member.name}"
        assert i18n.translate(member, locale="nl") == mapping[member.value]
        assert i18n.translate(member, locale="nl") != "Onbekend"


def test_every_actual_pattern_and_setup_has_a_translation():
    mapping = i18n.catalog("nl")["states"]
    for name in tuple(PATTERN_NAMES) + tuple(SETUP_DEFINITIONS):
        assert name in mapping, f"missing pattern/setup: {name}"
        assert i18n.translate(name, locale="nl") != "Onbekend"


@pytest.mark.parametrize("key", ["HH", "HL", "LH", "LL", "EQH", "EQL", "BOS", "CHoCH", "MSS",
                                 "BREAKDOWN", "FAILED_BREAKDOWN", "SELL_SIDE_SWEEP", "BUY_SIDE_SWEEP",
                                 "RECLAIM", "REJECTION", "SUPPORT_RESISTANCE_FLIP", "RESTING", "APPROACHING",
                                 "TESTING", "SWEPT", "RECLAIMED", "CONSUMED", "INVALIDATED",
                                 "BULLISH", "BEARISH", "LONG", "SHORT", "POOR_EXECUTION", "WAITING_FOR_TRIGGER",
                                 "DERIVED_NATIVE_CANDLE_DELTA", "SAME_DIRECTION", "INVERSE",
                                 "AFTER_CONFIRMED_HIGH", "AFTER_CONFIRMED_LOW", "COMPRESSING_AFTER_CONFIRMED_HIGH",
                                 "COMPRESSING_AFTER_CONFIRMED_LOW", "BOUNDARY_TEST", "CLOSED_BREAK_REVIEW",
                                 "ACCEPTANCE_OR_FAILURE_REVIEW", "RETEST_REVIEW", "ACCEPTANCE_REVIEW",
                                 "CONTINUATION_OR_FAILURE_REVIEW"])
def test_actual_structure_bias_liquidity_and_execution_codes_are_known(key):
    assert i18n.translate(key, locale="nl") != "Onbekend"


@pytest.mark.parametrize("key", ["expected_path", "conditional_expected_path", "primary_path", "alternative_path",
                                 "failure_path", "forward_three", "forward_three_notice", "setup_quality",
                                 "entry_quality", "invalid_below", "invalid_above", "target", "market_phase",
                                 "liquidity", "whale_flow", "whale_flow_notice", "session", "session_notice",
                                 "paper_journal", "trade_calculator", "risk_calculator", "target_profit_calculator",
                                 "no_signal_wait"])
def test_war_room_labels_exist(key):
    assert i18n.translate(key, "labels", locale="nl") != "Onbekend"


@pytest.mark.parametrize("key", [None, "", "MADE_UP_BULLISH_CERTAINTY", "SYNTHETIC_PROBABILITY_90", 42])
def test_unknown_values_remain_unknown_never_optimistic(key):
    assert i18n.translate(key, locale="nl") == "Onbekend"
    assert i18n.translate(key, "labels", locale="nl") == "Onbekend"


def test_known_keys_support_case_and_whitespace_without_exposing_raw_code():
    assert i18n.translate(" bullish ", locale="nl") == "Stijgend scenario"
    assert i18n.translate(" CHoCH ", locale="nl") == "Verandering van structuur"
    assert i18n.translate("Setup_Quality", "labels", locale="nl") == "Kwaliteit setup"
    assert i18n.translate("BULLISH", "unsafe_namespace", locale="nl") == "Onbekend"


@pytest.mark.parametrize("locale", ["de", "nl-NL", "../config/settings", "/etc/passwd", "", None])
def test_locale_is_explicitly_restricted_and_cannot_choose_arbitrary_resource(locale):
    with pytest.raises(ValueError, match="supported locales"):
        i18n.catalog(locale)
    with pytest.raises(ValueError, match="supported locales"):
        i18n.translate("BULLISH", locale=locale)


def test_returned_catalog_cannot_mutate_cached_translations():
    result = i18n.catalog("nl")
    original = result["states"]["UNAVAILABLE"]
    result["states"]["UNAVAILABLE"] = "Gegarandeerde koop"
    result["labels"].clear()
    assert i18n.translate("UNAVAILABLE", locale="nl") == original
    assert i18n.catalog("nl")["labels"]["setup_quality"] == "Kwaliteit setup"


def test_packaged_catalog_is_read_once_per_cache_lifetime(monkeypatch):
    text = json.dumps({"language": "nl", "labels": {"unknown": "Onbekend"}, "states": {"UNKNOWN": "Onbekend"}})
    resource = Mock()
    resource.read_text.return_value = text
    package = Mock()
    package.joinpath.return_value = resource
    loader = Mock(return_value=package)
    monkeypatch.setattr(i18n, "files", loader)
    assert i18n.translate("UNKNOWN", locale="nl") == "Onbekend"
    assert i18n.catalog("nl")["language"] == "nl"
    assert i18n.translate("UNKNOWN", locale="nl") == "Onbekend"
    loader.assert_called_once_with("expansion_hunter")
    package.joinpath.assert_called_once_with("static", "i18n.nl.json")
    resource.read_text.assert_called_once_with(encoding="utf-8")


@pytest.mark.parametrize("bad", [
    '{"language":"nl","labels":{"x":"a","x":"b"},"states":{"UNKNOWN":"Onbekend"}}',
    '{"language":"en","labels":{"x":"a"},"states":{"UNKNOWN":"Onbekend"}}',
    '{"language":"nl","labels":{},"states":{"UNKNOWN":"Onbekend"}}',
    '{"language":"nl","labels":{"x":"<script>"},"states":{"UNKNOWN":"Onbekend"}}',
    '{"language":"nl","labels":{"x":99},"states":{"UNKNOWN":"Onbekend"}}',
    '{"language":"nl","labels":{"x":""},"states":{"UNKNOWN":"Onbekend"}}',
    '[]',
])
def test_malformed_packaged_translation_fails_explicitly(monkeypatch, bad):
    resource = Mock()
    resource.read_text.return_value = bad
    package = Mock()
    package.joinpath.return_value = resource
    monkeypatch.setattr(i18n, "files", Mock(return_value=package))
    with pytest.raises(ValueError):
        i18n.catalog("nl")


def test_research_quality_does_not_turn_into_probability_or_unqualified_truth():
    states = i18n.catalog("nl")["states"]
    for phase in CyclePhase:
        if phase != CyclePhase.NEUTRAL_UNCERTAIN:
            assert "hypothese" in states[phase.value].lower()
    assert "geen kansen" in i18n.translate("uncalibrated_notice", "labels", locale="nl")
    assert "voorwaardelijk" in i18n.translate("conditional_expected_path", "labels", locale="nl").lower()
    assert "geen voorspelling" in i18n.translate("historical_research", "labels", locale="nl").lower()
    assert "identiteit" in i18n.translate("whale_flow_notice", "labels", locale="nl").lower()
    assert all("%" not in text for text in states.values())
