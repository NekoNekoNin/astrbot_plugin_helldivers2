"""Contracts for real Companion schemas and carefully bounded war analysis."""

import copy
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PACKAGE = "helldivers_war_test_plugin"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(
    f"{PACKAGE}.war_client", ROOT / "war_client.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
WarClient, SourceError = module.WarClient, module.SourceError


class FakeHTTP:
    def __init__(self, payload, *, retrieved="2026-10-24T10:00:00+00:00"):
        self.payload = payload
        self.retrieved = retrieved
        self.calls = []

    async def json(self, url, params=None, ttl_seconds=0):
        self.calls.append((url, params, ttl_seconds))
        return {
            "data": copy.deepcopy(self.payload),
            "source_url": url,
            "retrieved_at": self.retrieved,
            "cached": False,
            "stale": False,
        }


def current_data():
    return {
        "clientTime": 1792836000000,
        "warId": 801,
        "timeSinceStart": 86795687,
        "warInfo": {
            "startDate": 1706040313,
            "planetInfos": [
                {"index": 75, "maxHealth": 1000000, "waypoints": [122]},
                {"index": 122, "maxHealth": 1000000, "waypoints": []},
            ],
            "planetRegions": [
                {"planetIndex": 75, "regionIndex": 0, "maxHealth": 400000}
            ],
        },
        "warStatus": {
            "time": 84895784,
            "impactMultiplier": 0.02,
            "planetStatus": [
                {
                    "index": 75,
                    "owner": 2,
                    "players": 20000,
                    "health": 100000,
                    "regenPerSecond": 6.9444444,
                },
                {
                    "index": 122,
                    "owner": 1,
                    "players": 10,
                    "health": 1000000,
                    "regenPerSecond": 4.1666665,
                },
            ],
            "campaigns": [{"id": 900, "planetIndex": 75, "type": 0}],
            "planetEvents": [],
            "planetAttacks": [{"source": 122, "target": 75}],
            "planetRegions": [
                {
                    "planetIndex": 75,
                    "regionIndex": 0,
                    "health": 200000,
                    "regerPerSecond": 1.1111111,
                    "isAvailable": True,
                    "players": 20,
                }
            ],
        },
        "majorOrders": [],
        "news": [{"id": 42, "published": 84895780, "message": "<i=3>NEWS</i>"}],
    }


def historical(rows):
    return {"timestampUtc": "2026-10-24T09:45:00Z", "data": rows}


def point(t, health, *, owner=2, maximum=1000000):
    return {
        "timestampUtc": t,
        "owner": owner,
        "health": health,
        "maxHealth": maximum,
        "players": 20000,
        "regenPerSecond": 6.9444444,
    }


@pytest.mark.asyncio
async def test_current_uses_exact_companion_endpoint_and_units():
    http = FakeHTTP(current_data())
    result = await WarClient(http).current(75)
    assert http.calls == [(module.COMPANION_LIVE, None, 30)]
    planet = result["planet"]
    assert planet["planet_liberation_pct"] == 90
    assert planet["regen_pct_points_per_hour"] == pytest.approx(2.5)
    assert planet["regen_hp_per_second"] == pytest.approx(6.9444444)
    assert planet["regen_is_net_liberation_rate"] is False
    assert planet["net_liberation_pct_points_per_hour"] is None
    assert planet["eta"] is None
    assert result["total_players"] == 20010
    assert result["source_url"] == module.COMPANION_LIVE
    assert result["stale"] is False


@pytest.mark.asyncio
async def test_epoch_uses_corrected_game_war_clock_not_start_date_alone():
    result = await WarClient(FakeHTTP(current_data())).current()
    assert result["war_time_epoch_unix"] == 1707940216
    assert result["upstream_timestamp"] == "2026-10-24T10:00:00+00:00"
    assert result["news"][0]["published_at"] == "2026-10-24T09:59:56+00:00"
    assert result["news"][0]["message"] == "NEWS"


@pytest.mark.asyncio
async def test_directional_links_are_not_reversed_and_regions_not_aggregated():
    client = WarClient(FakeHTTP(current_data()), {75: "Esker"})
    result = await client.current(75)
    planet = result["planet"]
    assert planet["supply_lines_outgoing"] == [122]
    assert planet["attack_origins"] == [122]
    assert planet["attack_targets"] == []
    assert planet["regions"][0]["progress_pct"] == 50
    assert planet["regions"][0]["regen_pct_points_per_hour"] == pytest.approx(1)
    assert planet["progress_basis"] == "region_system_requires_separate_progress"
    assert planet["name"] == "Esker"
    other = await client.current(122)
    assert other["planet"]["supply_lines_outgoing"] == []
    assert other["planet"]["planet_liberation_pct"] == 100


@pytest.mark.asyncio
async def test_event_hp_is_separate_from_planet_hp_and_clock_is_calibrated():
    data = current_data()
    data["warStatus"]["planetEvents"] = [
        {
            "id": 2,
            "planetIndex": 122,
            "eventType": 99,
            "health": 150000,
            "maxHealth": 300000,
            "race": 3,
            "startTime": 84895700,
            "expireTime": 84900000,
        }
    ]
    result = await WarClient(FakeHTTP(data)).current(122)
    event = result["planet"]["event"]
    assert event["event_hp_progress_pct"] == 50
    assert event["type"] == 99
    assert event["start_at"] == "2026-10-24T09:58:36+00:00"
    assert result["planet_event_count"] == 1
    assert result["planet"]["progress_basis"] == "event_health"
    assert result["planet"]["planet_liberation_pct"] == 100


@pytest.mark.asyncio
async def test_stale_live_data_is_marked():
    result = await WarClient(
        FakeHTTP(current_data(), retrieved="2026-10-24T10:06:00Z")
    ).current()
    assert result["stale"] is True
    assert result["source_age_seconds"] == 360


@pytest.mark.asyncio
async def test_active_planets_are_capped_with_true_count():
    data = current_data()
    data["warInfo"]["planetInfos"] = [
        {"index": x, "maxHealth": 1000000, "waypoints": []} for x in range(40)
    ]
    data["warStatus"]["planetStatus"] = [
        {"index": x, "owner": 2, "players": x, "health": 1000000} for x in range(40)
    ]
    data["warStatus"]["campaigns"] = [{"planetIndex": x, "id": x} for x in range(40)]
    result = await WarClient(FakeHTTP(data)).current()
    assert result["active_planet_count"] == 40
    assert len(result["active_planets"]) == 30
    assert result["active_planets_truncated"] is True
    assert result["active_planets"][0]["index"] == 39


@pytest.mark.asyncio
async def test_unknown_planet_never_fabricates_data():
    with pytest.raises(SourceError, match="不在当前"):
        await WarClient(FakeHTTP(current_data())).current(999)


@pytest.mark.asyncio
async def test_real_catalog_search_matches_current_hash_and_real_index():
    data = current_data()
    data["warInfo"]["planetInfos"][0]["settingsHash"] = 2729833133
    client = WarClient(FakeHTTP(data))
    result = await client.find_planets("ESKER")
    assert result["items"][0]["index"] == 75
    assert result["items"][0]["name"] == "Esker"
    assert result["items"][0]["official_chinese_verified"] is False
    assert result["name_source_version"] == "02.182.013"
    current = await client.current(75)
    assert current["planet"]["name"] == "Esker"
    assert current["planet"]["owner_label_en"] == "Terminids"
    assert (await client.find_planets("75"))["items"][0]["index"] == 75


@pytest.mark.asyncio
async def test_changed_layout_never_reuses_a_stale_name_for_same_index():
    data = current_data()
    data["warInfo"]["planetInfos"][0]["settingsHash"] = 2729833133
    http = FakeHTTP(data)
    client = WarClient(http)
    assert (await client.current(75))["planet"]["name"] == "Esker"
    http.payload["warInfo"]["planetInfos"][0]["settingsHash"] = 999999999
    assert (await client.current(75))["planet"]["name"] is None
    assert (await client.find_planets("Esker"))["items"] == []


@pytest.mark.asyncio
async def test_catalog_query_is_bounded_and_never_a_url_or_new_network_destination():
    http = FakeHTTP(current_data())
    client = WarClient(http)
    for query in ["", " " * 4, "a" * 121, None]:
        with pytest.raises(SourceError):
            await client.find_planets(query)
    with pytest.raises(SourceError):
        await client.find_planets("Esker", limit=31)
    assert http.calls == []
    result = await client.find_planets("https://127.0.0.1/secret")
    assert result["items"] == []
    assert http.calls[0][0] == module.COMPANION_LIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"warInfo": {}, "warStatus": {}}])
async def test_changed_current_schema_fails_closed(payload):
    with pytest.raises(SourceError):
        await WarClient(FakeHTTP(payload)).current()


@pytest.mark.asyncio
async def test_history_uses_real_rolling_seven_days_endpoint_and_observed_trend():
    rows = [
        point("2026-10-24T08:45:00Z", 500000),
        point("2026-10-24T09:00:00Z", 450000),
        point("2026-10-24T09:30:00Z", 400000),
        point("2026-10-24T09:45:00Z", 350000),
    ]
    http = FakeHTTP(historical(rows))
    result = await WarClient(http).history(75, hours=2, limit=2)
    assert http.calls[0][0] == f"{module.COMPANION_CDN}/planets/75/7days.json"
    assert len(result["points"]) == 2
    assert result["coverage"]["matched_points"] == 4
    assert result["coverage"]["sampled"] is True
    assert result["coverage"]["arbitrary_dates_supported"] is False
    assert result["trend"]["net_liberation_pct_points_per_hour"] == 15
    assert result["trend"]["eta"] is None
    assert result["points"][0]["liberation_pct"] == 50
    assert result["points"][-1]["liberation_pct"] == 65


@pytest.mark.asyncio
async def test_history_filters_requested_window_and_discloses_missing_coverage():
    result = await WarClient(
        FakeHTTP(
            historical(
                [
                    point("2026-10-24T08:00:00Z", 600000),
                    point("2026-10-24T09:30:00Z", 500000),
                    point("2026-10-25T09:30:00Z", 400000),
                    {"timestampUtc": "broken"},
                ]
            )
        )
    ).history(75, hours=1)
    assert len(result["points"]) == 1
    assert result["coverage"]["invalid_timestamp_rows"] == 1
    assert result["trend"]["net_liberation_pct_points_per_hour"] is None


@pytest.mark.asyncio
async def test_owner_reset_health_reset_and_long_gaps_do_not_create_fake_rates():
    for rows in [
        [
            point("2026-10-24T09:00:00Z", 10000, owner=2),
            point("2026-10-24T09:30:00Z", 1000000, owner=1),
        ],
        [
            point("2026-10-24T09:00:00Z", 500000),
            point("2026-10-24T09:30:00Z", 400000, maximum=500000),
        ],
        [point("2026-10-24T07:00:00Z", 500000), point("2026-10-24T09:30:00Z", 400000)],
    ]:
        result = await WarClient(FakeHTTP(historical(rows))).history(75)
        assert result["trend"]["net_liberation_pct_points_per_hour"] is None


@pytest.mark.asyncio
async def test_empty_histories_are_disclosed_and_unknown_task_schema_stays_raw():
    events = await WarClient(FakeHTTP(historical([]))).event_history()
    assert events["points"] == []
    assert events["coverage"]["available_from"] is None
    assert events["coverage"]["full_requested_window_covered"] is False
    rows = [
        {
            "timestampUtc": "2026-10-24T09:45:00Z",
            "warTime": 100,
            "assignments": [
                {
                    "id32": 123,
                    "progress": [500000],
                    "expiresIn": 100,
                    "setting": {
                        "overrideTitle": "<i=1>ORDER</i>",
                        "tasks": [{"type": 999, "values": [75], "valueTypes": [123]}],
                    },
                }
            ],
        }
    ]
    result = await WarClient(FakeHTTP(historical(rows))).assignment_history(limit=1)
    assignment = result["points"][0]["assignments"][0]
    assert assignment["title"] == "ORDER"
    assert assignment["tasks"][0] == {"type": 999, "values": [75], "valueTypes": [123]}
    assert assignment["progress"] == [500000]
    assert result["points"][0]["timestamp"] == rows[0]["timestampUtc"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,args",
    [
        ("current", {"planet_id": True}),
        ("current", {"planet_id": -1}),
        ("history", {"planet_id": 75, "hours": 169}),
        ("history", {"planet_id": 75, "hours": 0}),
        ("history", {"planet_id": 75, "limit": 201}),
        ("history", {"planet_id": 75, "limit": 0}),
        ("event_history", {"hours": 49}),
        ("assignment_history", {"limit": True}),
    ],
)
async def test_invalid_parameters_never_issue_network_calls(method, args):
    http = FakeHTTP({})
    with pytest.raises(SourceError):
        await getattr(WarClient(http), method)(**args)
    assert http.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"data": "invalid"}])
async def test_history_changed_schema_is_not_silently_empty(payload):
    with pytest.raises(SourceError, match="结构"):
        await WarClient(FakeHTTP(payload)).history(75)


def test_invalid_numbers_and_health_are_not_converted_to_plausible_percentages():
    assert module._progress(-1, 100) is None
    assert module._progress(101, 100) is None
    assert module._progress(float("nan"), 100) is None
    assert module._progress(True, 100) is None
    assert module._regen(5, 0) is None
