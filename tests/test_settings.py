"""Configuration validation must reject ambiguity before opening a connection."""

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = "helldivers_settings_test_plugin"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
settings = importlib.import_module(f"{PACKAGE}.settings")


def test_defaults_are_complete_independent_and_exclude_unknown_fields():
    first = settings.validate_settings({"legacy_password": "never-export"})
    assert first == settings.DEFAULTS
    first["enabled"] = False
    assert settings.validate_settings({})["enabled"] is True
    assert "legacy_password" not in first


@pytest.mark.parametrize(
    "key",
    [
        "enabled",
        "attach_guidance",
        "wiki_enabled",
        "news_enabled",
        "war_enabled",
        "history_enabled",
    ],
)
@pytest.mark.parametrize("value", ["false", 0, None])
def test_switches_never_coerce_strings_numbers_or_null(key, value):
    with pytest.raises(ValueError, match=key):
        settings.validate_settings({key: value})


@pytest.mark.parametrize(
    "key,low,high",
    [
        ("timeout_seconds", 5, 120),
        ("max_concurrency", 1, 3),
        ("cache_max_entries", 1, 128),
    ],
)
def test_bounded_integer_fields_reject_booleans_floats_and_out_of_range(key, low, high):
    for value in [True, "5", float(low), low - 1, high + 1, float("inf")]:
        with pytest.raises(ValueError, match=key):
            settings.validate_settings({key: value})
    assert settings.validate_settings({key: low})[key] == low
    assert settings.validate_settings({key: high})[key] == high


@pytest.mark.parametrize("key", ["proxy_mode", "proxy_url"])
def test_text_settings_reject_wrong_types(key):
    with pytest.raises(TypeError, match=key):
        settings.validate_settings({key: None})


@pytest.mark.parametrize("value", ["ftp", "", "SYSTEM"])
def test_proxy_modes_are_explicit(value):
    with pytest.raises(ValueError, match="连接方式"):
        settings.validate_settings({"proxy_mode": value})


@pytest.mark.parametrize(
    "url",
    [
        "",
        "socks5://127.0.0.1:1080",
        "http://user:private-secret@127.0.0.1:7890",
        "http://127.0.0.1:7890/path",
        "http://127.0.0.1:7890?token=private-secret",
        "http://127.0.0.1:7890#fragment",
    ],
)
def test_custom_proxy_rejects_unsafe_components_without_echoing_secrets(url):
    with pytest.raises(ValueError) as failure:
        settings.validate_settings({"proxy_mode": "custom", "proxy_url": url})
    assert "private-secret" not in str(failure.value)
    assert "user:" not in str(failure.value)


def test_custom_proxy_is_normalized_and_existing_config_is_not_mutated():
    config = {"proxy_mode": "custom", "proxy_url": " http://127.0.0.1:7890/ "}
    validated = settings.validate_settings(config)
    assert validated["proxy_url"] == "http://127.0.0.1:7890"
    assert config["proxy_url"].startswith(" ")
