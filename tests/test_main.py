"""Plugin integration using the installed framework's actual schema parser.

Only Star/context/events and HTTP clients are stubbed. The actual AstrBot
register_llm_tool and spec_to_func implementations are extracted without
importing or starting the production application or touching its account data.
"""

import ast
import copy
import importlib
import inspect
import json
import logging
import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import docstring_parser
import pytest

ROOT = Path(__file__).parents[1]
FRAMEWORK = (
    Path(os.environ["ASTRBOT_SOURCE"]) / "astrbot/core"
    if os.environ.get("ASTRBOT_SOURCE")
    else ROOT.parents[1] / "Astrbot/astrbot/core"
)


def _actual_definition(path, name, *, class_name=None):
    parsed = ast.parse(path.read_text(encoding="utf-8"))
    nodes = parsed.body
    if class_name:
        nodes = next(
            node
            for node in nodes
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ).body
    found = next(
        node
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__", names=[ast.alias(name="annotations")], level=0
            ),
            found,
        ],
        type_ignores=[],
    )
    return compile(ast.fix_missing_locations(module), str(path), "exec")


@pytest.fixture(scope="module")
def plugin_module():
    if not (FRAMEWORK / "star/register/star_handler.py").is_file():
        pytest.skip(
            "Set ASTRBOT_SOURCE to an AstrBot checkout for framework integration tests"
        )
    registry = {}
    parser_globals = {"copy": copy, "FuncTool": types.SimpleNamespace}
    # Execute only the fixed, locally installed framework definition isolated above.
    exec(  # noqa: S102
        _actual_definition(
            FRAMEWORK / "provider" / "func_tool_manager.py",
            "spec_to_func",
            class_name="FunctionToolManager",
        ),
        parser_globals,
    )

    class Registry:
        def add_func(self, name, args, desc, handler):
            registry[name] = parser_globals["spec_to_func"](
                self, name, args, desc, handler
            )

    decorator_globals = {
        "docstring_parser": docstring_parser,
        "re": re,
        "PY_TO_JSON_TYPE": {
            "int": "number",
            "float": "number",
            "bool": "boolean",
            "str": "string",
            "dict": "object",
            "list": "array",
            "tuple": "array",
            "set": "array",
        },
        "SUPPORTED_TYPES": ["string", "number", "object", "array", "boolean"],
        "llm_tools": Registry(),
        "EventType": types.SimpleNamespace(OnCallingFuncToolEvent=1),
        "get_handler_or_create": lambda handler, *args: types.SimpleNamespace(
            handler=handler
        ),
    }
    exec(  # noqa: S102
        _actual_definition(
            FRAMEWORK / "star" / "register" / "star_handler.py", "register_llm_tool"
        ),
        decorator_globals,
    )

    class FakeStar:
        def __init__(self, context):
            self.context = context
            self.logger = logging.getLogger("helldivers_plugin_test")

    def hook(**kwargs):
        def decorate(fn):
            fn._test_hook = kwargs
            return fn

        return decorate

    stubs = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.provider": types.ModuleType("astrbot.api.provider"),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "quart": types.ModuleType("quart"),
    }
    stubs["astrbot.api.event"].AstrMessageEvent = object
    stubs["astrbot.api.event"].filter = types.SimpleNamespace(
        llm_tool=decorator_globals["register_llm_tool"], on_llm_request=hook
    )
    stubs["astrbot.api.provider"].ProviderRequest = object
    stubs["astrbot.api.star"].Star = FakeStar
    stubs["astrbot.api.star"].Context = object
    stubs["quart"].jsonify = lambda *args, **kwargs: args[0] if args else kwargs
    stubs["quart"].request = types.SimpleNamespace(path="")
    package_name = "helldivers_main_test_plugin"
    package = types.ModuleType(package_name)
    package.__path__ = [str(ROOT)]
    with pytest.MonkeyPatch.context() as patch:
        for key, value in stubs.items():
            patch.setitem(sys.modules, key, value)
        patch.setitem(sys.modules, package_name, package)
        module = importlib.import_module(f"{package_name}.main")
    module._test_registry = registry
    return module


@pytest.fixture
def plugin(plugin_module, monkeypatch):
    context = types.SimpleNamespace(
        apis=[], register_web_api=lambda *args: context.apis.append(args)
    )
    http = types.SimpleNamespace(
        status=lambda: {"requests": 0}, clear_cache=lambda: None, close=AsyncMock()
    )
    wiki = types.SimpleNamespace(
        search=AsyncMock(
            return_value={"items": [], "retrieved_at": "2026-10-24T10:00:00Z"}
        ),
        read=AsyncMock(
            return_value={
                "title": "AR-23 Liberator",
                "retrieved_at": "2026-10-24T10:00:00Z",
            }
        ),
    )
    war = types.SimpleNamespace(
        current=AsyncMock(
            return_value={
                "active_planets": [],
                "planet": None,
                "retrieved_at": "2026-10-24T10:00:00Z",
            }
        ),
        find_planets=AsyncMock(return_value={"items": []}),
        history=AsyncMock(return_value={"points": []}),
        event_history=AsyncMock(return_value={"points": []}),
        assignment_history=AsyncMock(return_value={"points": []}),
    )
    news = types.SimpleNamespace(
        search=AsyncMock(
            return_value={"items": [], "retrieved_at": "2026-10-24T10:00:00Z"}
        ),
        read=AsyncMock(return_value={"event_id": "123"}),
    )
    monkeypatch.setattr(plugin_module, "SourceHTTP", lambda *args, **kwargs: http)
    monkeypatch.setattr(plugin_module, "WikiClient", lambda *args: wiki)
    monkeypatch.setattr(plugin_module, "WarClient", lambda *args: war)
    monkeypatch.setattr(plugin_module, "OfficialNewsClient", lambda *args: news)
    return plugin_module.Helldivers2Plugin(context, {"proxy_mode": "direct"})


def event(text="", stopped=False):
    return types.SimpleNamespace(
        get_message_str=lambda: text, is_stopped=lambda: stopped
    )


def test_seven_tools_parse_through_actual_astrbot_schema_builder(plugin_module):
    expected = {
        "helldivers_search": {"query": "string", "limit": "number"},
        "helldivers_read": {
            "reference": "string",
            "section": "string",
            "offset": "number",
            "max_chars": "number",
        },
        "helldivers_terms": {"query": "string", "limit": "number"},
        "helldivers_war": {
            "planet_id": "number",
            "query": "string",
            "history_hours": "number",
        },
        "helldivers_history": {
            "kind": "string",
            "planet_id": "number",
            "hours": "number",
            "limit": "number",
        },
        "helldivers_updates": {
            "query": "string",
            "event_id": "string",
            "offset": "number",
            "max_chars": "number",
            "limit": "number",
        },
        "helldivers_sources": {"probe": "boolean"},
    }
    assert set(plugin_module._test_registry) == set(expected)
    for name, properties in expected.items():
        tool = plugin_module._test_registry[name]
        assert tool.description
        assert tool.parameters["type"] == "object"
        assert {
            key: val["type"] for key, val in tool.parameters["properties"].items()
        } == properties
        assert set(inspect.signature(tool.handler).parameters) - {
            "self",
            "event",
        } == set(properties)
        assert all(
            value.get("description") for value in tool.parameters["properties"].values()
        )


def test_constructor_loads_packaged_skill_and_registers_fixed_admin_routes(plugin):
    assert "helldivers_war" in plugin.guidance
    assert not plugin.guidance.startswith("---")
    assert {row[0] for row in plugin.context.apis} == {
        "/astrbot_plugin_helldivers2/state",
        "/astrbot_plugin_helldivers2/diagnose",
        "/astrbot_plugin_helldivers2/cache/clear",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,switch,args,client,operation",
    [
        ("helldivers_search", "wiki_enabled", {"query": "AR-23"}, "wiki", "search"),
        ("helldivers_read", "wiki_enabled", {"reference": "AR-23"}, "wiki", "read"),
        ("helldivers_war", "war_enabled", {}, "war", "current"),
        ("helldivers_history", "history_enabled", {"planet_id": 75}, "war", "history"),
        ("helldivers_updates", "news_enabled", {}, "news", "search"),
    ],
)
async def test_disabled_source_never_calls_underlying_clients(
    plugin, method, switch, args, client, operation
):
    plugin.settings[switch] = False
    result = json.loads(await getattr(plugin, method)(event(), **args))
    assert result["ok"] is False
    getattr(getattr(plugin, client), operation).assert_not_awaited()


@pytest.mark.asyncio
async def test_global_disable_stopped_events_and_activity_bound(plugin):
    plugin.settings["enabled"] = False
    assert json.loads(await plugin.helldivers_terms(event(), "AR-23"))["ok"] is False
    plugin.settings["enabled"] = True
    assert (
        json.loads(await plugin.helldivers_search(event(stopped=True), "AR-23"))["ok"]
        is False
    )
    plugin.wiki.search.assert_not_awaited()
    for _ in range(35):
        await plugin.helldivers_search(event(stopped=True), "AR-23")
    assert len(plugin.recent) == 30


@pytest.mark.asyncio
async def test_war_names_require_disambiguation_then_actual_index(plugin):
    plugin.war.find_planets.return_value = {
        "items": [{"index": 75, "name": "Esker"}, {"index": 76, "name": "Esker II"}],
        "source_url": "https://helldiverscompanion.com/",
    }
    ambiguous = json.loads(await plugin.helldivers_war(event(), query="Esker"))
    assert ambiguous["selection_required"] is True
    assert len(ambiguous["items"]) == 2
    assert ambiguous["items"][0]["official_name"]["verified"] is True
    assert ambiguous["items"][0]["official_name"]["label"] == "艾斯克尔"
    assert ambiguous["items"][1]["official_name"]["verified"] is False
    plugin.war.current.assert_not_awaited()
    plugin.war.find_planets.return_value = {"items": [{"index": 75, "name": "Esker"}]}
    plugin.war.current.return_value = {
        "planet": {"index": 75, "name": "Esker", "owner_label_en": "Terminids"},
        "active_planets": [],
    }
    found = json.loads(await plugin.helldivers_war(event(), query="Esker"))
    plugin.war.current.assert_awaited_once_with(75)
    assert found["planet"]["official_name"]["verified"] is True
    assert found["planet"]["official_faction_name"]["verified"] is True


@pytest.mark.asyncio
async def test_war_empty_matches_and_invalid_combined_arguments_never_guess(
    plugin, plugin_module
):
    missing = await plugin._war(-1, "unrecognized-planet")
    assert missing["selection_required"] is True
    plugin.war.current.assert_not_awaited()
    for value in [True, -2, 1.5]:
        with pytest.raises(plugin_module.SourceError):
            await plugin._war(value, "")
    with pytest.raises(plugin_module.SourceError):
        await plugin._war(75, "Esker")


@pytest.mark.asyncio
async def test_history_dispatches_to_correct_real_source_and_rejects_unknown_kind(
    plugin, plugin_module
):
    await plugin._history("planet", 75, 24, 10)
    plugin.war.history.assert_awaited_once_with(75, hours=24, limit=10)
    await plugin._history("events", -1, 48, 10)
    plugin.war.event_history.assert_awaited_once_with(hours=48, limit=10)
    await plugin._history("orders", -1, 48, 10)
    plugin.war.assignment_history.assert_awaited_once_with(hours=48, limit=10)
    for kind, index in [("all-history", 75), ("planet", -1), ("planet", True)]:
        with pytest.raises(plugin_module.SourceError):
            await plugin._history(kind, index, 24, 10)


@pytest.mark.asyncio
async def test_recommendation_fetches_compact_candidate_histories_once(plugin):
    plugin.war.current.return_value = {
        "upstream_timestamp": "2026-10-25T02:26:39Z",
        "retrieved_at": "2026-10-25T02:26:48+00:00",
        "active_planets": [
            {
                "index": index,
                "name": f"Planet {index}",
                "players": index * 100,
                "owner_label_en": "Terminids",
            }
            for index in range(1, 31)
        ],
        "planet": None,
    }
    plugin.war.history.return_value = {
        "coverage": {"matched_points": 96, "returned_points": 8, "sampled": True},
        "points": [{"timestamp": "2026-10-25T02:00:00Z", "players": 123}],
        "trend": {"net_liberation_pct_points_per_hour": 1.2},
    }
    result = json.loads(await plugin.helldivers_war(event(), history_hours=24))
    assert result["ok"] is True
    assert [row["planet_id"] for row in result["candidate_histories"]] == [30, 29, 28]
    assert plugin.war.history.await_count == 3
    assert all(
        call.kwargs == {"hours": 24, "limit": 8}
        for call in plugin.war.history.await_args_list
    )
    assert (
        result["candidate_histories"][0]["history"]["trend"][
            "net_liberation_pct_points_per_hour"
        ]
        == 1.2
    )
    assert "Terminids" in result["official_name_evidence"]
    assert "provenance" not in result["active_planets"][0]["official_faction_name"]
    assert len(json.dumps(result, ensure_ascii=False)) < 20000
    assert result["recommendation_facts"]["candidates"][0]["current_players"] == 3000
    point = result["candidate_histories"][0]["history"]["points"][0]
    assert point["players_at_historical_sample"] == 123
    assert "players" not in point


def test_source_times_are_explicit_utc_plus_eight(plugin_module):
    assert (
        plugin_module._hong_kong_time("2026-10-25T02:26:39Z")
        == "2026-10-25T10:26:39+08:00"
    )
    assert (
        plugin_module._hong_kong_time("2026-10-25T02:26:39+00:00")
        == "2026-10-25T10:26:39+08:00"
    )
    assert plugin_module._hong_kong_time(None) is None
    assert plugin_module._hong_kong_time("2026-10-25T02:26:39") is None


@pytest.mark.asyncio
async def test_war_dispatch_equipment_without_official_evidence_requires_english(
    plugin,
):
    plugin.war.current.return_value = {
        "active_planets": [],
        "planet": None,
        "news": [
            {
                "message": "To bolster efforts, the Unmapped Test Cannon Stratagem has been made freely available."
            }
        ],
    }
    result = json.loads(await plugin.helldivers_war(event(), history_hours=24))
    assert result["recommendation_facts"][
        "unverified_equipment_names_keep_english"
    ] == ["Unmapped Test Cannon"]


@pytest.mark.asyncio
async def test_recommendation_history_switch_and_partial_failure(plugin, plugin_module):
    plugin.war.current.return_value = {
        "active_planets": [{"index": 75, "name": "Esker", "players": 10}],
        "planet": None,
    }
    plugin.settings["history_enabled"] = False
    result = json.loads(await plugin.helldivers_war(event(), history_hours=24))
    assert result["ok"] is True
    assert result["history_status"] == "disabled"
    plugin.war.history.assert_not_awaited()
    plugin.settings["history_enabled"] = True
    plugin.war.history.side_effect = plugin_module.SourceError("Source unavailable")
    result = json.loads(await plugin.helldivers_war(event(), history_hours=24))
    assert result["ok"] is True
    assert result["candidate_histories"][0]["history"]["ok"] is False
    assert result["candidate_histories"][0]["history"]["error"] == "Source unavailable"
    plugin.war.history.reset_mock()
    result = json.loads(await plugin.helldivers_war(event(), history_hours=169))
    assert result["ok"] is False
    plugin.war.history.assert_not_awaited()


@pytest.mark.asyncio
async def test_regional_candidate_cannot_equate_planet_hp_trend_with_campaign_progress(
    plugin,
):
    plugin.war.current.return_value = {
        "active_planets": [
            {
                "index": 158,
                "name": "Choepessa IV",
                "players": 25000,
                "regions": [{"index": 2, "is_available": True, "progress_pct": 10.6}],
            },
            {"index": 268, "name": "Luxuriant", "players": 14000, "regions": []},
        ],
        "planet": None,
    }
    result = json.loads(await plugin.helldivers_war(event(), history_hours=24))
    assert result["active_planets"][0]["regional_campaign"] is True
    assert (
        result["candidate_histories"][0]["planet_hp_trend_is_campaign_progress"]
        is False
    )
    assert (
        result["candidate_histories"][1]["planet_hp_trend_is_campaign_progress"] is True
    )
    assert "regional" in result["analysis_constraints"].lower()


@pytest.mark.asyncio
async def test_update_read_passes_exact_returned_id_and_continuation(plugin):
    result = json.loads(
        await plugin.helldivers_updates(
            event(), event_id="123", offset=1000, max_chars=2000
        )
    )
    assert result["ok"] is True
    plugin.news.read.assert_awaited_once_with("123", offset=1000, max_chars=2000)
    plugin.news.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_with_explicit_section_keeps_source_page_language_and_title(plugin):
    await plugin.helldivers_read(
        event(),
        reference="AR-23 解放者",
        section="详细武器数据",
        offset=1000,
        max_chars=2000,
    )
    plugin.wiki.read.assert_awaited_once_with(
        "AR-23 解放者", section="详细武器数据", offset=1000, max_chars=2000
    )
    plugin.wiki.read.reset_mock()
    await plugin.helldivers_read(event(), reference="AR-23 解放者", offset=1000)
    plugin.wiki.read.assert_awaited_once_with(
        "AR-23 解放者", section="", offset=1000, max_chars=10000
    )
    plugin.wiki.read.reset_mock()
    await plugin.helldivers_read(event(), reference="AR-23")
    plugin.wiki.read.assert_awaited_once_with(
        "AR-23 Liberator", section="", offset=0, max_chars=10000
    )


@pytest.mark.asyncio
async def test_chinese_search_is_preserved_then_falls_back_to_english_when_needed(
    plugin,
):
    plugin.wiki.search.side_effect = [{"items": []}, {"items": [{"title": "Charger"}]}]
    result = json.loads(await plugin.helldivers_search(event(), "强袭虫"))
    assert result["ok"] is True
    assert plugin.wiki.search.await_args_list[0].args == ("强袭虫",)
    assert plugin.wiki.search.await_args_list[1].args == ("Charger",)
    plugin.wiki.read.reset_mock()
    await plugin.helldivers_read(event(), reference="AR-23 解放者")
    plugin.wiki.read.assert_awaited_once_with(
        "AR-23 解放者", section="", offset=0, max_chars=10000
    )


@pytest.mark.asyncio
async def test_sources_status_is_offline_and_reports_true_terminology_coverage(plugin):
    result = json.loads(await plugin.helldivers_sources(event()))
    assert result["ok"] is True
    assert result["history_coverage"]["arbitrary_dates"] is False
    assert result["official_names"]["items"] >= 100
    assert (
        result["official_names"]["official_verified"]
        + result["official_names"]["unverified"]
        == result["official_names"]["items"]
    )
    for mock in [plugin.wiki.search, plugin.war.current, plugin.news.search]:
        mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_respects_switches_and_has_per_source_failures(
    plugin, plugin_module
):
    plugin.settings["wiki_enabled"] = False
    plugin.war.current.side_effect = plugin_module.SourceError("来源暂时不可用")
    result = json.loads(await plugin.helldivers_sources(event(), probe=True))
    assert result["ok"] is True
    assert result["checks"]["wiki"]["skipped"] == "disabled"
    assert result["checks"]["companion"]["ok"] is False
    assert result["checks"]["official_news"]["ok"] is True
    plugin.wiki.search.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("private-response-token-123"),
        TypeError("private-response-token-123"),
        ValueError("private-response-token-123"),
    ],
)
async def test_unexpected_exception_bodies_never_reach_model_or_activity_log(
    plugin, error
):
    plugin.wiki.search.side_effect = error
    result = await plugin.helldivers_search(event(), "AR-23")
    assert json.loads(result)["ok"] is False
    assert "private-response-token-123" not in result
    assert "private-response-token-123" not in json.dumps(list(plugin.recent))


@pytest.mark.asyncio
async def test_expected_source_failure_and_success_are_structured(
    plugin, plugin_module
):
    plugin.wiki.search.side_effect = plugin_module.SourceError("请核对条目标题")
    failed = json.loads(await plugin.helldivers_search(event(), "AR-23"))
    assert failed == {"ok": False, "error": "请核对条目标题"}
    plugin.wiki.search.side_effect = None
    success = json.loads(await plugin.helldivers_search(event(), "AR-23"))
    assert success["ok"] is True
    assert success["report_timezone"] == "Asia/Hong_Kong"
    assert success["effective_query"] == "AR-23 Liberator"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "绝地潜兵2银河战争现在该打哪里",
        "Helldivers 2 AR-23 damage",
        "战略配备怎么选",
        "机械族有什么弱点？",
    ],
)
async def test_relevant_game_topics_receive_guidance_exactly_once(plugin, text):
    req = types.SimpleNamespace(prompt=text, system_prompt="existing-persona")
    await plugin.add_guidance(event(text), req)
    await plugin.add_guidance(event(text), req)
    assert req.system_prompt.count("[HD2核实指导]") == 1
    assert req.system_prompt.startswith("existing-persona")


@pytest.mark.asyncio
async def test_unrelated_messages_and_disabled_guidance_do_not_expand_context(plugin):
    req = types.SimpleNamespace(prompt="你好", system_prompt="existing-persona")
    await plugin.add_guidance(event("你好"), req)
    assert req.system_prompt == "existing-persona"
    plugin.settings["attach_guidance"] = False
    await plugin.add_guidance(event("绝地潜兵2"), req)
    assert req.system_prompt == "existing-persona"


@pytest.mark.asyncio
async def test_followup_uses_only_recent_user_context_and_only_when_linguistically_linked(
    plugin,
):
    req = types.SimpleNamespace(
        prompt="那它呢？",
        system_prompt="persona",
        contexts=[
            {"role": "user", "content": "绝地潜兵2 AR-23 怎么配装"},
            {"role": "assistant", "content": "先核对补丁"},
        ],
    )
    await plugin.add_guidance(event("那它呢？"), req)
    assert "[HD2核实指导]" in req.system_prompt
    req.system_prompt = "persona"
    await plugin.add_guidance(event("今天吃什么"), req)
    assert req.system_prompt == "persona"
    req.contexts = [{"role": "assistant", "content": "绝地潜兵2"}]
    await plugin.add_guidance(event("继续"), req)
    assert req.system_prompt == "persona"
    req.contexts = [
        {"role": "user", "content": "绝地潜兵2"},
        {"role": "user", "content": "做饭"},
        {"role": "user", "content": "种菜"},
    ]
    await plugin.add_guidance(event("继续"), req)
    assert req.system_prompt == "persona"


@pytest.mark.asyncio
async def test_terminate_closes_only_the_plugin_http_client(plugin):
    await plugin.terminate()
    plugin.http.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_chinese_weapon_name_without_game_prefix_still_gets_research_guidance(
    plugin,
):
    request = types.SimpleNamespace(prompt="查询武器爆裂铳", system_prompt="persona")
    await plugin.add_guidance(event("查询武器爆裂铳"), request)
    assert "[HD2核实指导]" in request.system_prompt
