"""Companion's public live endpoint and bounded, timestamped CDN histories."""

from __future__ import annotations

import html
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .source_http import SourceError

COMPANION_LIVE = (
    "https://helldiverscompanion.com/api/hell-divers-2-api/get-api-data-live"
)
COMPANION_CDN = "https://cdn.helldiverscompanion.com/live"
WAR_HOSTS = {"helldiverscompanion.com", "cdn.helldiverscompanion.com"}
FACTION_LABELS = {
    0: "None",
    1: "Humans",
    2: "Terminids",
    3: "Automatons",
    4: "Illuminate",
    5: "Wizard",
}
EVENT_LABELS = {0: "TakeOver", 1: "Defense", 2: "Invasion", 3: "Attrition"}


def _integer(value, label: str, low: int, high: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise SourceError(f"{label}应为 {low}–{high} 的整数")
    return value


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _date(value) -> datetime | None:
    try:
        if not isinstance(value, str):
            return None
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _iso_unix(value) -> str | None:
    value = _number(value)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def _text(value, limit=1000) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))[:limit]


def _rows(value):
    return (
        [row for row in value if isinstance(row, dict)]
        if isinstance(value, list)
        else []
    )


def _progress(health, maximum) -> float | None:
    health, maximum = _number(health), _number(maximum)
    if health is None or maximum is None or maximum <= 0 or not 0 <= health <= maximum:
        return None
    return round(100 * (1 - health / maximum), 6)


def _regen(value, maximum) -> float | None:
    value, maximum = _number(value), _number(maximum)
    if value is None or maximum is None or maximum <= 0:
        return None
    return round(value * 3600 * 100 / maximum, 6)


def _sample(rows: list, limit: int) -> list:
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return rows[-1:]
    return [
        rows[round(index * (len(rows) - 1) / (limit - 1))] for index in range(limit)
    ]


class WarClient:
    """Read the same public JSON files referenced by Companion's map frontend.

    No arbitrary URL, calendar archive, reverse supply line, or inferred task schema
    is accepted. Every result identifies the actual source and available coverage.
    """

    def __init__(self, http, planet_names: dict | None = None):
        self.http = http
        self.planet_names = planet_names or {}
        self._resolved_names = {}
        try:
            self._directory = json.loads(
                (
                    Path(__file__).parent / "resources" / "companion_planets.json"
                ).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            self._directory = {}

    def _name(self, index: int, info: dict) -> str | None:
        entry = self._directory.get("planets", {}).get(
            str(info.get("settingsHash")), {}
        )
        if entry.get("index") == index and isinstance(entry.get("name"), str):
            self._resolved_names[index] = entry["name"]
            return entry["name"]
        self._resolved_names.pop(index, None)
        return self.planet_names.get(index, self.planet_names.get(str(index)))

    async def find_planets(self, query: str, limit: int = 10) -> dict:
        _integer(limit, "结果上限", 1, 30)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
            raise SourceError("请输入 1–120 字的星球名或真实数字索引")
        query = query.strip()
        response = await self.http.json(COMPANION_LIVE, ttl_seconds=30)
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("warInfo"), dict):
            raise SourceError("Companion 未提供可验证的当前星球目录")
        current = _rows(data["warInfo"].get("planetInfos"))
        normalized = re.sub(r"[\W_]+", "", query.casefold())
        candidates = []
        for info in current:
            index = info.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                continue
            name = self._name(index, info)
            exact_id = query.isascii() and query.isdecimal() and int(query) == index
            name_normalized = re.sub(r"[\W_]+", "", (name or "").casefold())
            if exact_id or (normalized and normalized in name_normalized):
                candidates.append(
                    {
                        "index": index,
                        "name": name,
                        "settings_hash": info.get("settingsHash"),
                        "name_language": "en" if name else None,
                        "official_chinese_verified": False,
                        "exact_match": exact_id or name_normalized == normalized,
                    }
                )
        candidates.sort(key=lambda row: (not row["exact_match"], row["index"]))
        client_ms = _number(data.get("clientTime"))
        result = self._base(
            response, _iso_unix(client_ms / 1000) if client_ms is not None else None
        )
        result.update(
            {
                "query": query,
                "items": candidates[:limit],
                "total_matches": len(candidates),
                "truncated": len(candidates) > limit,
                "current_planet_count": len(current),
                "name_origin": "Companion public catalog/game names",
                "name_source_url": self._directory.get("source_url"),
                "name_source_version": self._directory.get("frontend_version"),
                "name_source_retrieved_at": self._directory.get("retrieved_at"),
                "limitations": [
                    "Names are Companion English names, not verified official Simplified Chinese.",
                    "Only exact live settingsHash+index matches are named; unrecognized layouts remain unnamed.",
                    "Use the returned real index for a war or history lookup; do not invent indices from translations.",
                ],
            }
        )
        return result

    def _base(self, response: dict, timestamp: str | None) -> dict:
        retrieved = response.get("retrieved_at")
        observed, now = _date(timestamp), _date(retrieved)
        age = (now - observed).total_seconds() if observed and now else None
        return {
            "source": "Helldivers Companion public JSON (community aggregation)",
            "source_url": response.get("source_url"),
            "retrieved_at": retrieved,
            "upstream_timestamp": timestamp,
            "source_age_seconds": round(age, 1) if age is not None else None,
            "cached": bool(response.get("cached")),
            "stale": bool(response.get("stale"))
            or age is None
            or bool(age > 1800 or age < -60),
            "external_content_is_untrusted": True,
        }

    def _event(self, row: dict, epoch: float | None) -> dict:
        result = {
            "id": row.get("id", row.get("id32")),
            "planet_index": row.get("planetIndex"),
            "type": row.get("eventType", row.get("type")),
            "event_type_label_en": EVENT_LABELS.get(
                row.get("eventType", row.get("type"))
            ),
            "race": row.get("race"),
            "race_label_en": FACTION_LABELS.get(row.get("race")),
            "health": _number(row.get("health")),
            "max_health": _number(row.get("maxHealth")),
            "event_hp_progress_pct": _progress(row.get("health"), row.get("maxHealth")),
            "start_war_seconds": _number(row.get("startTime")),
            "end_war_seconds": _number(row.get("expireTime")),
        }
        for original, target in [
            ("startTime", "start_at"),
            ("expireTime", "expires_at"),
        ]:
            seconds = _number(row.get(original))
            result[target] = (
                _iso_unix(epoch + seconds)
                if epoch is not None and seconds is not None
                else None
            )
        result["interpretation"] = (
            "Event HP progress is separate from planet liberation; unknown event types require source verification."
        )
        return result

    def _assignment(self, row: dict, epoch: float | None = None) -> dict:
        setting = row.get("setting") if isinstance(row.get("setting"), dict) else {}
        start = _number(row.get("startTime"))
        return {
            "id": row.get("id32"),
            "title": _text(setting.get("overrideTitle"), 200),
            "brief": _text(setting.get("overrideBrief"), 1800),
            "description": _text(setting.get("taskDescription"), 1800),
            "start_war_seconds": start,
            "start_at": _iso_unix(epoch + start)
            if epoch is not None and start is not None
            else None,
            "expires_in_seconds_at_snapshot": _number(row.get("expiresIn")),
            "progress": row.get("progress")
            if isinstance(row.get("progress"), list)
            else [],
            "tasks": _rows(setting.get("tasks"))[:20],
            "rewards": _rows(setting.get("rewards"))[:20],
            "interpretation": "Task types/values/valueTypes are raw upstream schema, not automatically decoded as planet IDs or completion booleans.",
        }

    async def current(self, planet_id: int | None = None) -> dict:
        if planet_id is not None:
            _integer(planet_id, "星球索引", 0, 10000)
        response = await self.http.json(COMPANION_LIVE, ttl_seconds=30)
        data = response.get("data")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("warInfo"), dict)
            or not isinstance(data.get("warStatus"), dict)
        ):
            raise SourceError("Companion 实况数据结构已变化，无法可靠解读")
        info, status = data["warInfo"], data["warStatus"]
        planets_info, planets_status = (
            _rows(info.get("planetInfos")),
            _rows(status.get("planetStatus")),
        )
        if not planets_info or not planets_status:
            raise SourceError("Companion 未提供可用的星球实况")
        client_ms = _number(data.get("clientTime"))
        timestamp = _iso_unix(client_ms / 1000) if client_ms is not None else None
        epoch_parts = [
            _number(info.get("startDate")),
            _number(data.get("timeSinceStart")),
            _number(status.get("time")),
        ]
        epoch = (
            epoch_parts[0] + epoch_parts[1] - epoch_parts[2]
            if all(x is not None for x in epoch_parts)
            else None
        )
        result = self._base(response, timestamp)
        result["stale"] = result["stale"] or bool(
            result["source_age_seconds"] is not None
            and result["source_age_seconds"] > 300
        )
        info_by_id = {row.get("index"): row for row in planets_info}
        campaign_rows, event_rows = (
            _rows(status.get("campaigns")),
            _rows(status.get("planetEvents")),
        )
        campaigns = {row.get("planetIndex"): row for row in campaign_rows}
        events = {row.get("planetIndex"): row for row in event_rows}
        region_info = {
            (row.get("planetIndex"), row.get("regionIndex")): row
            for row in _rows(info.get("planetRegions"))
        }
        attacks = _rows(status.get("planetAttacks"))
        all_planets = []
        for row in planets_status:
            index = row.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                continue
            details = info_by_id.get(index, {})
            maximum, health, regen = (
                _number(details.get("maxHealth")),
                _number(row.get("health")),
                _number(row.get("regenPerSecond")),
            )
            regions = []
            for region in _rows(status.get("planetRegions")):
                if region.get("planetIndex") != index:
                    continue
                region_max = _number(
                    region_info.get((index, region.get("regionIndex")), {}).get(
                        "maxHealth"
                    )
                )
                region_regen = _number(
                    region.get("regerPerSecond", region.get("regenPerSecond"))
                )
                regions.append(
                    {
                        "index": region.get("regionIndex"),
                        "owner": region.get("owner"),
                        "health": _number(region.get("health")),
                        "max_health": region_max,
                        "players": _number(region.get("players")),
                        "is_available": region.get("isAvailable") is True,
                        "progress_pct": _progress(region.get("health"), region_max),
                        "regen_hp_per_second": region_regen,
                        "regen_pct_points_per_hour": _regen(region_regen, region_max),
                    }
                )
            event = self._event(events[index], epoch) if index in events else None
            liberation = 100.0 if row.get("owner") == 1 else _progress(health, maximum)
            all_planets.append(
                {
                    "index": index,
                    "name": self._name(index, details),
                    "name_language": "en" if self._name(index, details) else None,
                    "official_chinese_verified": False,
                    "owner": row.get("owner"),
                    "owner_label_en": FACTION_LABELS.get(row.get("owner")),
                    "players": _number(row.get("players")),
                    "health": health,
                    "max_health": maximum,
                    "planet_liberation_pct": liberation,
                    "regen_hp_per_second": regen,
                    "regen_pct_points_per_hour": _regen(regen, maximum),
                    "regen_is_net_liberation_rate": False,
                    "net_liberation_pct_points_per_hour": None,
                    "eta": None,
                    "campaign": campaigns.get(index),
                    "event": event,
                    "supply_lines_outgoing": [
                        x
                        for x in details.get("waypoints", [])
                        if isinstance(x, int) and not isinstance(x, bool)
                    ]
                    if isinstance(details.get("waypoints"), list)
                    else [],
                    "attack_origins": [
                        x.get("source") for x in attacks if x.get("target") == index
                    ],
                    "attack_targets": [
                        x.get("target") for x in attacks if x.get("source") == index
                    ],
                    "regions": regions,
                    "progress_basis": "event_health"
                    if event
                    else "region_system_requires_separate_progress"
                    if regions
                    else "planet_health",
                }
            )
        if planet_id is not None and not any(
            row["index"] == planet_id for row in all_planets
        ):
            raise SourceError(
                "该索引不在当前实况星球列表中，请先查询总览或核对真实星球索引"
            )
        active = [
            row
            for row in all_planets
            if row["campaign"] is not None or row["event"] is not None
        ]
        active.sort(
            key=lambda row: (row["event"] is not None, row.get("players") or 0),
            reverse=True,
        )
        result.update(
            {
                "war_id": data.get("warId", status.get("warId")),
                "war_time_seconds": _number(status.get("time")),
                "war_time_epoch_unix": epoch,
                "war_time_epoch_method": "warInfo.startDate + timeSinceStart - warStatus.time (Companion frontend)",
                "impact_multiplier": _number(status.get("impactMultiplier")),
                "total_players": sum(row["players"] or 0 for row in all_planets),
                "planet_count": len(all_planets),
                "active_planet_count": len(active),
                "campaign_count": len(campaign_rows),
                "planet_event_count": len(event_rows),
                "defense_event_count": sum(
                    row.get("eventType") == 1 for row in event_rows
                ),
                "name_source_url": self._directory.get("source_url"),
                "name_source_version": self._directory.get("frontend_version"),
                "name_origin": "Companion public catalog/game names",
                "active_planets": active[:30] if planet_id is None else [],
                "active_planets_truncated": planet_id is None and len(active) > 30,
                "planet": next(
                    (row for row in all_planets if row["index"] == planet_id), None
                ),
                "major_orders": [
                    self._assignment(row, epoch)
                    for row in _rows(data.get("majorOrders"))[:20]
                ],
                "news": [
                    {
                        "id": row.get("id"),
                        "published_war_seconds": row.get("published"),
                        "published_at": _iso_unix(epoch + row["published"])
                        if epoch is not None
                        and _number(row.get("published")) is not None
                        else None,
                        "message": _text(row.get("message"), 1800),
                    }
                    for row in _rows(data.get("news"))[-10:]
                ],
                "coverage": {
                    "kind": "current_snapshot",
                    "all_planets_counted": len(all_planets),
                    "active_planets_returned": min(len(active), 30)
                    if planet_id is None
                    else 0,
                },
                "limitations": [
                    "Community aggregation is not Arrowhead endorsement.",
                    "Player counts and regeneration alone cannot predict net liberation or ETA.",
                    "Outgoing waypoints and directional attacks are preserved without inventing reverse links.",
                    "Region HP and defense event HP are separate from planet HP; task type values remain raw until verified.",
                ],
            }
        )
        return result

    async def _history(
        self, path: str, hours: int, maximum: int, limit: int
    ) -> tuple[dict, list]:
        _integer(hours, "小时范围", 1, maximum)
        _integer(limit, "结果上限", 1, 200)
        response = await self.http.json(f"{COMPANION_CDN}/{path}", ttl_seconds=600)
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise SourceError("Companion 历史数据结构已变化，无法可靠解读")
        raw_rows = _rows(data["data"])
        rows = [row for row in raw_rows if _date(row.get("timestampUtc"))]
        rows.sort(key=lambda row: _date(row["timestampUtc"]))
        now = _date(response.get("retrieved_at")) or datetime.now(UTC)
        cutoff = now - timedelta(hours=hours)
        matching = [
            row
            for row in rows
            if cutoff <= _date(row["timestampUtc"]) <= now + timedelta(seconds=60)
        ]
        result = self._base(response, data.get("timestampUtc"))
        result["coverage"] = {
            "kind": "rolling_cdn_history",
            "requested_hours": hours,
            "upstream_window_max_hours": maximum,
            "arbitrary_dates_supported": False,
            "available_from": rows[0]["timestampUtc"] if rows else None,
            "available_to": rows[-1]["timestampUtc"] if rows else None,
            "matched_from": matching[0]["timestampUtc"] if matching else None,
            "matched_to": matching[-1]["timestampUtc"] if matching else None,
            "upstream_points": len(rows),
            "matched_points": len(matching),
            "returned_points": min(len(matching), limit),
            "sampled": len(matching) > limit,
            "invalid_timestamp_rows": len(raw_rows) - len(rows),
            "full_requested_window_covered": bool(
                rows and _date(rows[0]["timestampUtc"]) <= cutoff
            ),
        }
        result["limitations"] = [
            "Rolling CDN window only, not a complete launch-to-present archive.",
            "Sampling cadence and gaps are source-dependent; an empty array does not prove no historical events occurred.",
            "Rates describe observed samples, not guaranteed future outcomes.",
        ]
        return result, matching

    async def history(self, planet_id: int, hours: int = 168, limit: int = 100) -> dict:
        _integer(planet_id, "星球索引", 0, 10000)
        result, rows = await self._history(
            f"planets/{planet_id}/7days.json", hours, 168, limit
        )
        points = []
        for row in rows:
            points.append(
                {
                    "timestamp": row["timestampUtc"],
                    "owner": row.get("owner"),
                    "health": _number(row.get("health")),
                    "max_health": _number(row.get("maxHealth")),
                    "players": _number(row.get("players")),
                    "liberation_pct": 100.0
                    if row.get("owner") == 1
                    else _progress(row.get("health"), row.get("maxHealth")),
                    "regen_hp_per_second": _number(row.get("regenPerSecond")),
                    "regen_pct_points_per_hour": _regen(
                        row.get("regenPerSecond"), row.get("maxHealth")
                    ),
                }
            )
        trend = {
            "net_liberation_pct_points_per_hour": None,
            "observed_hours": None,
            "eta": None,
            "basis": "Recent same-owner, same-maxHealth contiguous planet-HP samples only; not defense or region progress.",
        }
        segment = []
        for row in reversed(points):
            if row["liberation_pct"] is None:
                break
            if segment and (
                row["owner"] != segment[-1]["owner"]
                or row["max_health"] != segment[-1]["max_health"]
                or (
                    _date(segment[-1]["timestamp"]) - _date(row["timestamp"])
                ).total_seconds()
                > 3600
            ):
                break
            segment.append(row)
            if (
                len(segment) > 1
                and (
                    _date(segment[0]["timestamp"]) - _date(row["timestamp"])
                ).total_seconds()
                >= 3600
            ):
                break
        if len(segment) > 1:
            end, start = segment[0], segment[-1]
            elapsed = (
                _date(end["timestamp"]) - _date(start["timestamp"])
            ).total_seconds() / 3600
            if elapsed >= 1 / 60:
                trend.update(
                    {
                        "observed_hours": round(elapsed, 6),
                        "from": start["timestamp"],
                        "to": end["timestamp"],
                        "net_liberation_pct_points_per_hour": round(
                            (end["liberation_pct"] - start["liberation_pct"]) / elapsed,
                            6,
                        ),
                    }
                )
        result.update(
            {
                "planet_id": planet_id,
                "name": self.planet_names.get(
                    planet_id,
                    self.planet_names.get(
                        str(planet_id), self._resolved_names.get(planet_id)
                    ),
                ),
                "points": _sample(points, limit),
                "trend": trend,
            }
        )
        return result

    async def event_history(self, hours: int = 48, limit: int = 50) -> dict:
        result, rows = await self._history("planetEvents/2days.json", hours, 48, limit)
        result["points"] = [
            {
                "timestamp": row["timestampUtc"],
                "war_time_seconds": row.get("warTime"),
                "events": [
                    self._event(event, None)
                    for event in _rows(row.get("planetEvents"))[:100]
                ],
            }
            for row in _sample(rows, limit)
        ]
        return result

    async def assignment_history(self, hours: int = 48, limit: int = 50) -> dict:
        result, rows = await self._history("assignments/2days.json", hours, 48, limit)
        result["points"] = [
            {
                "timestamp": row["timestampUtc"],
                "war_time_seconds": row.get("warTime"),
                "assignments": [
                    self._assignment(assignment)
                    for assignment in _rows(row.get("assignments"))[:20]
                ],
            }
            for row in _sample(rows, limit)
        ]
        return result
