"""Small, validated configuration surface for the read-only research plugin."""

from .source_http import SourceError, validate_proxy

DEFAULTS = {
    "enabled": True,
    "attach_guidance": True,
    "wiki_enabled": True,
    "news_enabled": True,
    "war_enabled": True,
    "history_enabled": True,
    "proxy_mode": "system",
    "proxy_url": "",
    "timeout_seconds": 30,
    "max_concurrency": 3,
    "cache_max_entries": 128,
}


def validate_settings(values: dict) -> dict:
    """Validate plugin settings before creating any network clients.

    Args:
        values: Values managed by AstrBot's plugin configuration page.

    Returns:
        A complete snapshot containing supported settings only.

    Raises:
        ValueError: If a range or connection mode is invalid.
        TypeError: If a text field has an invalid type.
    """
    result = {
        **DEFAULTS,
        **{key: value for key, value in values.items() if key in DEFAULTS},
    }
    for key, default in DEFAULTS.items():
        value = result[key]
        if isinstance(default, bool) and type(value) is not bool:
            raise ValueError(f"{key} 必须是开关值")
        if isinstance(default, str) and not isinstance(value, str):
            raise TypeError(f"{key} 必须是文本")
    for key, low, high in (
        ("timeout_seconds", 5, 120),
        ("max_concurrency", 1, 3),
        ("cache_max_entries", 1, 128),
    ):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key} 必须是 {low}–{high} 的整数")
    if result["proxy_mode"] not in {"system", "direct", "custom"}:
        raise ValueError("请选择 system、direct 或 custom 连接方式")
    result["proxy_url"] = result["proxy_url"].strip()
    if result["proxy_url"] or result["proxy_mode"] == "custom":
        try:
            result["proxy_url"] = validate_proxy(result["proxy_url"])
        except SourceError as exc:
            raise ValueError(str(exc)) from exc
    return result
