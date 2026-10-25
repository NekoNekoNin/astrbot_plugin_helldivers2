"""Source and pagination tests for official localized Steam announcements."""

import asyncio
import copy
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_helldivers2.official_news import (
    APP_ID,
    CLAN_ACCOUNT_ID,
    CLAN_STEAM_ID,
    OfficialNewsClient,
    _event_id,
    _plain_body,
)
from astrbot_plugin_helldivers2.source_http import SourceError, SourceHTTP


def run(awaitable):
    return asyncio.run(awaitable)


def event(
    gid=100,
    language=6,
    body="[h2]平衡性调整[/h2][list][*][p]伤害由 90 增加至 100[/p][/*][/list]",
):
    return {
        "gid": str(gid),
        "clan_steamid": CLAN_STEAM_ID,
        "appid": APP_ID,
        "published": 1,
        "hidden": 0,
        "announcement_body": {
            "gid": str(gid + 1),
            "event_gid": str(gid),
            "clanid": CLAN_ACCOUNT_ID,
            "headline": f"补丁 {gid}",
            "body": body,
            "posttime": 1792486911,
            "language": language,
            "hidden": 0,
            "banned": 0,
        },
    }


def source(handler):
    return SourceHTTP(
        {"proxy_mode": "direct"},
        {"store.steampowered.com"},
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("715666559007196081", "715666559007196081"),
        (
            "https://store.steampowered.com/news/app/553850/view/715666559007196081",
            "715666559007196081",
        ),
        (
            "https://store.steampowered.com/news/app/553850/view/715666559007196081?l=schinese",
            "715666559007196081",
        ),
    ],
)
def test_canonical_article_id(value, expected):
    assert _event_id(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "-1",
        "a123",
        "1&clanid=evil",
        "http://store.steampowered.com/news/app/553850/view/1",
        "https://example.com/news/app/553850/view/1",
        "https://store.steampowered.com.evil/news/app/553850/view/1",
        "https://store.steampowered.com/news/app/999/view/1",
        "https://user@store.steampowered.com/news/app/553850/view/1",
        "https://store.steampowered.com/news/app/553850/view/1?redirect=evil",
        "https://store.steampowered.com:99/news/app/553850/view/1",
        "https://store.steampowered.com/events/ajaxgetpartnerevent/?clan_accountid=999",
        "1\n2",
    ],
)
def test_bad_ids_and_arbitrary_urls_are_rejected(value):
    with pytest.raises(ValueError):
        _event_id(value)


def test_bbcode_renders_as_inert_text_with_numerical_context():
    text = _plain_body(
        '[h2][b]Damage[/b][/h2][list][*][p]90 to 100[/p][/*][*][p]2.7 s[/p][/*][/list][table][tr][td]90[/td][td]22[/td][/tr][/table][url="javascript:bad"]Read[/url]<script>execute bad</script>[img src="https://evil/a"][/img]'
    )
    assert "## Damage" in text
    assert "- 90 to 100" in text and "- 2.7 s" in text
    assert "90 | 22" in text
    assert (
        "javascript" not in text and "execute bad" not in text and "evil/a" not in text
    )
    assert "Read" in text


@pytest.mark.parametrize(
    "language,official,code",
    [(6, True, "zh-CN"), (0, False, "en"), (8, False, "steam-language-8")],
)
def test_only_actual_language_six_is_official_simplified_chinese(
    language, official, code
):
    def handler(request):
        assert request.url.host == "store.steampowered.com"
        assert request.url.params["clan_accountid"] == CLAN_ACCOUNT_ID
        assert request.url.params["lang_list"] == "6_0"
        return httpx.Response(
            200, json={"success": 1, "event": event(language=language)}
        )

    async def scenario():
        http = source(handler)
        try:
            article = await OfficialNewsClient(http).read("100")
            assert article["language"] == language
            assert article["official_zh_cn"] is official
            assert article["language_code"] == code
            assert article["published_at"] == "2026-10-20T09:01:51+00:00"
            assert article["sections"][0]["title"] == "平衡性调整"
            assert article["content_trust"] == "source_material_only"
        finally:
            await http.close()

    run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e.update(appid=999),
        lambda e: e.update(clan_steamid="999"),
        lambda e: e["announcement_body"].update(clanid="999"),
        lambda e: e.update(published=0),
        lambda e: e.update(hidden=1),
        lambda e: e["announcement_body"].update(language=None),
        lambda e: e["announcement_body"].update(language=6.5),
        lambda e: e["announcement_body"].pop("language"),
        lambda e: e["announcement_body"].update(body=None),
        lambda e: e["announcement_body"].update(event_gid="999"),
        lambda e: e.update(gid="101"),
    ],
)
def test_source_identity_and_format_changes_fail_closed(mutation):
    article = event()
    mutation(article)

    async def scenario():
        http = source(
            lambda request: httpx.Response(200, json={"success": 1, "event": article})
        )
        try:
            with pytest.raises(SourceError):
                await OfficialNewsClient(http).read("100")
        finally:
            await http.close()

    run(scenario())


def test_search_pagination_retains_all_matches_and_fixed_window():
    dataset = [
        event(
            100 + i,
            6 if i % 2 else 0,
            f"[p]item {i}" + (" 镰刀" if i % 3 == 1 else "") + "[/p]",
        )
        for i in range(61)
    ]
    calls = []

    def handler(request):
        p = request.url.params
        assert p["count"] == "20" and p["clan_accountid"] == CLAN_ACCOUNT_ID
        start = int(p["offset"])
        calls.append(start)
        return httpx.Response(
            200, json={"success": 1, "events": dataset[start : start + 20]}
        )

    async def scenario():
        http = source(handler)
        try:
            client = OfficialNewsClient(http)
            cursor, got = 0, []
            while True:
                result = await client.search("镰刀", limit=3, offset=cursor)
                got.extend(item["id"] for item in result["items"])
                assert all("text" not in item for item in result["items"])
                assert result["search_scope"]["newest_events_limit"] == 100
                if result["next_offset"] is None:
                    break
                assert result["next_offset"] > cursor
                cursor = result["next_offset"]
            assert got == [str(100 + i) for i in range(61) if i % 3 == 1]
            assert calls == [0, 20, 40, 60]
        finally:
            await http.close()

    run(scenario())


def test_search_never_claims_unbounded_history():
    calls = []

    def handler(request):
        start = int(request.url.params["offset"])
        calls.append(start)
        return httpx.Response(
            200,
            json={
                "success": 1,
                "events": [event(100 + i) for i in range(start, start + 20)],
            },
        )

    async def scenario():
        http = source(handler)
        try:
            result = await OfficialNewsClient(http).search("Not found")
            assert result["items"] == [] and result["next_offset"] is None
            assert (
                result["scanned_events"] == 100 and result["window_exhausted"] is True
            )
            assert calls == [0, 20, 40, 60, 80]
        finally:
            await http.close()

    run(scenario())


def test_full_article_continuation_is_lossless():
    raw = "[h2]Numbers[/h2]" + "".join(
        f"[p]Entry {i}: 90 to 100 damage; 2.7 seconds[/p]" for i in range(120)
    )

    async def scenario():
        http = source(
            lambda request: httpx.Response(
                200, json={"success": 1, "event": event(body=raw)}
            )
        )
        try:
            client = OfficialNewsClient(http)
            cursor, chunks = 0, []
            while True:
                result = await client.read("100", offset=cursor, max_chars=1000)
                assert len(result["text"]) <= 1000
                chunks.append(result["text"])
                if result["next_offset"] is None:
                    break
                assert result["next_offset"] > cursor
                cursor = result["next_offset"]
            assert "".join(chunks) == _plain_body(raw)
            with pytest.raises(ValueError, match="offset"):
                await client.read("100", offset=999999)
        finally:
            await http.close()

    run(scenario())


@pytest.mark.parametrize(
    "payload",
    [{}, {"success": 0}, {"success": 1, "events": None}, {"success": 1, "events": {}}],
)
def test_list_format_changes_and_errors_are_not_empty_success(payload):
    async def scenario():
        http = source(lambda request: httpx.Response(200, json=copy.deepcopy(payload)))
        try:
            with pytest.raises(SourceError):
                await OfficialNewsClient(http).search()
        finally:
            await http.close()

    run(scenario())
