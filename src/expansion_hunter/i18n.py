"""Packaged English/Dutch labels; unknown codes never imply a trade direction."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

DEFAULT_LOCALE = "en"
UNKNOWN = "Unknown"
_CATALOG_FILES = {"en": "i18n.en.json", "nl": "i18n.nl.json"}
_UNKNOWN = {"en": UNKNOWN, "nl": "Onbekend"}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate translation key: {key}")
        result[key] = value
    return result


@lru_cache(maxsize=2)
def _load_catalog(locale: str = DEFAULT_LOCALE) -> dict:
    if not isinstance(locale, str) or locale not in _CATALOG_FILES:
        raise ValueError("supported locales are 'en' and 'nl'")
    resource = files("expansion_hunter").joinpath("static", _CATALOG_FILES[locale])
    content = json.loads(resource.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(content, dict) or set(content) != {"language", "labels", "states"} or content["language"] != locale:
        raise ValueError("invalid packaged translation catalog")
    for namespace in ("labels", "states"):
        mapping = content[namespace]
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError("translation namespace must be a nonempty mapping")
        if any(not isinstance(key, str) or not key or not isinstance(value, str) or not value.strip()
               or "<" in value or ">" in value for key, value in mapping.items()):
            raise ValueError("translations must be nonempty plain strings")
    return content


def catalog(locale: str = DEFAULT_LOCALE) -> dict:
    """Return a JSON-safe independent copy for API/static-template consumers."""
    return copy.deepcopy(_load_catalog(locale))


def translate(key: str | None, namespace: str = "states", *, locale: str = DEFAULT_LOCALE) -> str:
    """Translate only known keys; unknown data is visibly unknown, never bullish."""
    mappings = _load_catalog(locale)
    unknown = _UNKNOWN[locale]
    if namespace not in {"states", "labels"} or not isinstance(key, str):
        return unknown
    mapping = mappings[namespace]
    normalized = key.strip()
    return mapping.get(normalized, mapping.get(normalized.upper() if namespace == "states" else normalized.lower(), unknown))
