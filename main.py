"""Native research tools and selectively loaded Helldivers 2 skill guidance."""

import asyncio
import json
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from quart import jsonify, request

from .official_news import OfficialNewsClient
from .settings import validate_settings
from .source_http import SourceError, SourceHTTP
from .terms import Terminology
from .war_client import WAR_HOSTS, WarClient
from .wiki_client import WIKI_HOST, WikiClient

PLUGIN = "astrbot_plugin_helldivers2"
QUERY = re.compile(
    r"绝地潜兵|絕地潛兵|絕地戰兵|helldivers?|银河战争|銀河戰爭|超级地球|超級地球"
    r"|终结族|机械族|光能者|强袭虫|吐酸泰坦|战略配备|helldiverscompanion|helldivers\.wiki\.gg"
    r"|爆裂铳|Eruptor|\b(?:AR|BR|MG|LAS|EXO|MLS|SMG|SG|JAR|AC|EAT|R)-\d+\b",
    re.IGNORECASE,
)


def _hong_kong_time(value):
    """Format a source timestamp in UTC+8 without relying on the host timezone.

    Args:
        value: An aware source ISO timestamp, or None when unavailable.

    Returns:
        An ISO timestamp with an explicit +08:00 offset, or None.
    """
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone(timedelta(hours=8))).isoformat(
            timespec="seconds"
        )
    except (ValueError, TypeError, AttributeError):
        return None


class Helldivers2Plugin(Star):
    """Use fixed public sources without changing unrelated models or chat routes."""

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.settings = validate_settings(dict(config))
        self.http = SourceHTTP(
            self.settings, {WIKI_HOST, *WAR_HOSTS, "store.steampowered.com"}
        )
        self.terms = Terminology()
        self.wiki = WikiClient(self.http)
        self.war = WarClient(self.http)
        self.news = OfficialNewsClient(self.http)
        self.recent = deque(maxlen=30)
        self.last_checks = {}
        skill_path = Path(__file__).parent / "skills/helldivers2-knowledge/SKILL.md"
        self.guidance = (
            skill_path.read_text(encoding="utf-8").split("---", 2)[-1].strip()
        )
        for endpoint, methods in {
            "state": ["GET"],
            "diagnose": ["POST"],
            "cache/clear": ["POST"],
        }.items():
            context.register_web_api(
                f"/{PLUGIN}/{endpoint}",
                self.web_api,
                methods,
                f"Helldivers 2 source management: {endpoint}",
            )

    async def _execute(self, event, component: str, action: str, query: str, operation):
        """Apply switches and record bounded source health without conversation data.

        Args:
            event: Current AstrBot event, if invoked from a chat tool.
            component: Source switch, or terms for the local terminology lookup.
            action: Activity name for the administrator.
            query: Bounded topic or public identifier, without credentials.
            operation: Callable returning an awaitable data result.

        Returns:
            A JSON string with actual evidence or a bounded failure explanation.
        """
        entry = {"at": time.time(), "action": action, "query": str(query)[:120]}
        try:
            if not self.settings["enabled"]:
                raise SourceError("绝地潜兵2资料助手已关闭，请在插件配置中启用")
            if component != "terms" and not self.settings[f"{component}_enabled"]:
                raise SourceError("此来源能力已在插件配置中关闭")
            if event is not None and event.is_stopped():
                raise SourceError("当前消息已停止处理")
            async with asyncio.timeout(self.settings["timeout_seconds"] + 5):
                data = await operation()
            data = dict(data)
            data.setdefault("report_timezone", "Asia/Hong_Kong")
            entry["ok"] = True
            return json.dumps({**data, "ok": True}, ensure_ascii=False)
        except SourceError as exc:
            entry.update(ok=False, error=str(exc)[:300])
            return json.dumps(
                {"ok": False, "error": str(exc)[:300]}, ensure_ascii=False
            )
        except (ValueError, TypeError):
            entry.update(
                ok=False, error="查询参数或来源格式不正确，请核对条目、编号和范围"
            )
            return json.dumps(
                {"ok": False, "error": entry["error"]}, ensure_ascii=False
            )
        except TimeoutError:
            entry.update(ok=False, error="本次资料查询超时，请缩小范围或稍后重试")
            return json.dumps(
                {"ok": False, "error": entry["error"]}, ensure_ascii=False
            )
        except Exception:
            self.logger.exception("Helldivers 2 source operation failed")
            entry.update(ok=False, error="来源数据暂时无法解读，请检查插件日志")
            return json.dumps(
                {"ok": False, "error": entry["error"]}, ensure_ascii=False
            )
        finally:
            self.recent.appendleft(entry)

    async def _search(self, query, limit):
        resolved = self.terms.resolve_query(query)
        effective = query if re.search(r"[\u3400-\u9fff]", query) else resolved
        data = await self.wiki.search(effective, limit=limit)
        if not data.get("items") and effective != resolved:
            effective = resolved
            data = await self.wiki.search(effective, limit=limit)
        data["requested_query"] = query
        data["effective_query"] = effective
        data["canonical_english_query"] = resolved
        data["terminology"] = self.terms.lookup(query, limit=3)
        for item in data.get("items", []):
            item["official_name"] = self.terms.label_for(item.get("title", ""))
        return data

    async def _read(self, reference, section, offset, max_chars):
        resolved = (
            reference
            if section
            or offset
            or re.search(r"[\u3400-\u9fff]", str(reference))
            or "://" in str(reference)
            else self.terms.resolve_query(reference)
        )
        data = await self.wiki.read(
            resolved, section=section, offset=offset, max_chars=max_chars
        )
        data["official_name"] = self.terms.label_for(data.get("title", ""))
        return data

    async def _war(self, planet_id, query, history_hours=0):
        if type(history_hours) is not int or not 0 <= history_hours <= 168:
            raise SourceError("history_hours 应为 0（仅实况）或 1–168 小时")
        if type(planet_id) is not int or planet_id < -1:
            raise SourceError("planet_id 应为 -1（总览）或已核实的星球索引")
        if query:
            if planet_id != -1:
                raise SourceError("星球名称与索引请选择一种查询方式")
            matches = await self.war.find_planets(
                self.terms.resolve_query(query), limit=10
            )
            items = matches.get("items", [])
            for item in items:
                item["official_name"] = self.terms.label_for(item.get("name") or "")
            if len(items) != 1:
                return {
                    **matches,
                    "selection_required": True,
                    "note": "请从真实目录确认星球索引，再查询实况或历史。",
                }
            planet_id = items[0]["index"]
        data = await self.war.current(None if planet_id == -1 else planet_id)
        rows = [*data.get("active_planets", [])]
        if data.get("planet"):
            rows.append(data["planet"])
        evidence = {}
        for item in rows:
            item["regional_campaign"] = bool(item.get("regions"))
            if item["regional_campaign"]:
                item["progress_interpretation"] = (
                    "Regional campaign: use current available regions. Planet HP history "
                    "does not measure overall campaign progress or the region's net rate. "
                    "No region history is supplied, so do not infer stalled progress from flat planet HP."
                )
            for field, name in (
                ("official_name", item.get("name") or ""),
                ("official_faction_name", item.get("owner_label_en")),
            ):
                if name is None:
                    continue
                label = self.terms.label_for(name)
                item[field] = {
                    key: label[key]
                    for key in (
                        "english",
                        "label",
                        "official_label",
                        "verified",
                        "label_kind",
                        "community_aliases",
                    )
                    if key in label
                }
                if label["verified"]:
                    evidence[name] = label["provenance"]
        data["official_name_evidence"] = evidence
        if not history_hours:
            return data
        if not self.settings["history_enabled"]:
            data["history_status"] = "disabled"
            return data
        candidates = (
            [data["planet"]]
            if data.get("planet")
            else sorted(
                data.get("active_planets", []),
                key=lambda item: item.get("players") or 0,
                reverse=True,
            )[:3]
        )
        jobs = [
            self.war.history(item["index"], hours=history_hours, limit=8)
            for item in candidates
        ]
        jobs.extend(
            [
                self.war.assignment_history(hours=min(history_hours, 48), limit=3),
                self.war.event_history(hours=min(history_hours, 48), limit=3),
            ]
        )
        results = await asyncio.gather(*jobs, return_exceptions=True)
        normalized = []
        for result in results:
            if isinstance(result, SourceError):
                normalized.append({"ok": False, "error": str(result)[:300]})
            elif isinstance(result, BaseException):
                normalized.append(
                    {"ok": False, "error": "本次历史来源未能返回可用数据"}
                )
            else:
                normalized.append({**result, "ok": True})
        data["history_status"] = (
            "partial" if any(not row["ok"] for row in normalized) else "ready"
        )
        data["candidate_selection"] = (
            "Selected planet, or up to three highest-player active planets returned by the snapshot. "
            "This is a transparent candidate set, not proof of a globally optimal objective; "
            "check major orders, defense deadlines and directional links before recommending."
        )
        data["candidate_histories"] = [
            {
                "planet_id": item["index"],
                "name": item.get("name"),
                "history": result,
                "planet_hp_trend_is_campaign_progress": not bool(
                    item.get("regions") or item.get("event")
                ),
            }
            for item, result in zip(candidates, normalized, strict=False)
        ]
        data["analysis_constraints"] = (
            "For EVERY regional candidate, planet-HP trends and planet regeneration are not "
            "the active region's net progress: never label the campaign stalled from flat planet HP. "
            "Compare region and planet metrics separately; lower enemy recovery alone does not "
            "prove greater support efficiency. A kill-based major order need not favor liberation "
            "or the most populated planet; distinguish practical preference from proven optimality. "
            "Use current live event count as well as event history; empty history alone proves nothing. "
            "Quote source_url links and timestamps for the recommendation."
        )
        data["order_history"] = normalized[-2]
        data["event_history"] = normalized[-1]
        data["next_step"] = (
            "Use these already-fetched histories and their full-series recent trend to answer now. "
            "Displayed points are sampled with coverage metadata; do not repeat identical requests. "
            "Only fetch another planet if major orders or urgent defense materially require it."
        )
        facts = []
        for item, history in zip(candidates, normalized, strict=False):
            coverage = history.get("coverage", {})
            for point in history.get("points", []):
                if "players" in point:
                    point["players_at_historical_sample"] = point.pop("players")
            facts.append(
                {
                    "planet_id": item["index"],
                    "name": item.get("name"),
                    "official_name": item.get("official_name"),
                    "faction_name": item.get("official_faction_name"),
                    "current_players": item.get("players"),
                    "current_planet_liberation_pct": item.get("planet_liberation_pct"),
                    "current_enemy_recovery_pph": item.get("regen_pct_points_per_hour"),
                    "regional_campaign": item.get("regional_campaign", False),
                    "current_available_regions": [
                        region
                        for region in item.get("regions", [])
                        if region.get("is_available")
                    ],
                    "history_from_hong_kong": _hong_kong_time(
                        coverage.get("matched_from")
                    ),
                    "history_to_hong_kong": _hong_kong_time(coverage.get("matched_to")),
                    "history_matched_points": coverage.get("matched_points"),
                    "history_displayed_points": coverage.get("returned_points"),
                    "history_source_url": history.get("source_url"),
                }
            )
        unverified_equipment = []
        equipment_names = {}
        for dispatch in data.get("news", []):
            for name in re.findall(
                r"the ([A-Za-z][A-Za-z -]{1,80}) Stratagem", dispatch.get("message", "")
            ):
                label = self.terms.label_for(name)
                equipment_names[name] = {
                    "label": label["label"],
                    "verified": label["verified"],
                }
                if label["verified"]:
                    data["official_name_evidence"][name] = label["provenance"]
                if not label["verified"] and name not in unverified_equipment:
                    unverified_equipment.append(name)
        return {
            "recommendation_facts": {
                "snapshot_upstream_hong_kong": _hong_kong_time(
                    data.get("upstream_timestamp")
                ),
                "snapshot_retrieved_hong_kong": _hong_kong_time(
                    data.get("retrieved_at")
                ),
                "source_url": data.get("source_url"),
                "candidates": facts,
                "unverified_equipment_names_keep_english": unverified_equipment,
                "equipment_names": equipment_names,
                "instructions": "Use current_players for current population, never players_at_historical_sample. Use the explicit +08:00 timestamps verbatim for Hong Kong time. Describe actual history endpoints, not an exact full 24-hour interval. Regional planet-HP trends do not establish region efficiency. Keep listed unverified equipment names in English; never present guessed Chinese as official names.",
            },
            **data,
        }

    async def _history(self, kind, planet_id, hours, limit):
        if kind == "planet":
            if type(planet_id) is not int or planet_id < 0:
                raise SourceError("星球历史需要先通过 helldivers_war 核实 planet_id")
            return await self.war.history(planet_id, hours=hours, limit=limit)
        if kind == "events":
            return await self.war.event_history(hours=hours, limit=limit)
        if kind == "orders":
            return await self.war.assignment_history(hours=hours, limit=limit)
        raise SourceError("历史类型应为 planet、events 或 orders")

    async def _sources(self, probe=False):
        if probe:
            for name, switch, operation in (
                ("wiki", "wiki", lambda: self.wiki.search("AR-23", limit=1)),
                ("companion", "war", self.war.current),
                ("official_news", "news", lambda: self.news.search(limit=1)),
            ):
                if not self.settings[f"{switch}_enabled"]:
                    self.last_checks[name] = {"ok": None, "skipped": "disabled"}
                    continue
                try:
                    data = await operation()
                    self.last_checks[name] = {
                        "ok": True,
                        "retrieved_at": data.get("retrieved_at"),
                    }
                except SourceError as exc:
                    self.last_checks[name] = {"ok": False, "error": str(exc)[:240]}
                except (ValueError, TypeError, TimeoutError):
                    self.last_checks[name] = {
                        "ok": False,
                        "error": "来源未能返回可用数据或查询超时",
                    }
        return {
            "sources": {
                "wiki": "https://helldivers.wiki.gg/zh/",
                "wiki_english": "https://helldivers.wiki.gg/",
                "war_map": "https://helldiverscompanion.com/#map",
                "official_zh_cn": "https://www.playstation.com/zh-hans-hk/games/helldivers-2/",
                "official_news": "https://store.steampowered.com/news/app/553850?l=schinese",
            },
            "history_coverage": {
                "planet_max_hours": 168,
                "events_max_hours": 48,
                "orders_max_hours": 48,
                "arbitrary_dates": False,
            },
            "official_names": self.terms.status(),
            "checks": self.last_checks,
            "network": self.http.status(),
            "note": "Wiki与Companion为社区来源；术语只以已核对的官方简中资料认证。历史覆盖以每次真实返回为准。",
        }

    @filter.on_llm_request(priority=50)
    async def add_guidance(self, event: AstrMessageEvent, req: ProviderRequest):
        topic = event.get_message_str() or req.prompt or ""
        relevant = bool(QUERY.search(topic))
        if not relevant and re.search(r"^(那|它|这|刚才|前面|继续|如果|相比)", topic):
            recent_users = [
                item.get("content", "")
                for item in getattr(req, "contexts", [])[-8:]
                if isinstance(item, dict) and item.get("role") == "user"
            ][-2:]
            relevant = any(
                isinstance(text, str) and QUERY.search(text[:2000])
                for text in recent_users
            )
        if (
            self.settings["enabled"]
            and self.settings["attach_guidance"]
            and relevant
            and "[HD2核实指导]" not in (req.system_prompt or "")
        ):
            req.system_prompt = f"{req.system_prompt or ''}\n[HD2核实指导]\n{self.guidance}\n[/HD2核实指导]".strip()

    @filter.llm_tool(name="helldivers_search")
    async def helldivers_search(
        self, event: AstrMessageEvent, query: str, limit: int = 5
    ):
        """Search Helldivers 2 Wiki for weapons, enemies, stratagems or mechanics, resolving verified official Simplified Chinese terms. Read relevant detailed tables using helldivers_read before numeric analysis; search snippets alone are incomplete.

        Args:
            query(string): A concise topic, official name, nickname or equipment model.
            limit(number): Maximum results from 1 to 10.
        """
        return await self._execute(
            event, "wiki", "search", query, lambda: self._search(query, limit)
        )

    @filter.llm_tool(name="helldivers_read")
    async def helldivers_read(
        self,
        event: AstrMessageEvent,
        reference: str,
        section: str = "",
        offset: int = 0,
        max_chars: int = 10000,
    ):
        """Read actual Helldivers 2 Wiki prose and structured tables with revision, time and continuation. Match weapon variants, patches, armor and body parts before calculations; community names are distinct from verified official Simplified Chinese labels.

        Args:
            reference(string): Confirmed article title or exact helldivers.wiki.gg article URL.
            section(string): Optional returned heading, anchor or section index.
            offset(number): Character continuation within the page or selected section.
            max_chars(number): Response window from 1000 to 24000 characters.
        """
        return await self._execute(
            event,
            "wiki",
            "read",
            reference,
            lambda: self._read(reference, section, offset, max_chars),
        )

    @filter.llm_tool(name="helldivers_terms")
    async def helldivers_terms(
        self, event: AstrMessageEvent, query: str, limit: int = 10
    ):
        """Look up a PARTIAL cache of verified Helldivers 2 official Simplified Chinese names and English/model aliases. An empty result means not indexed, never that a weapon or its Chinese name does not exist. Continue helldivers_search/read using the original Chinese name or model before asking the user. The zh and en pages are language versions of one Wiki, and Chinese game translations are not unofficial merely because this cache lacks them.

        Args:
            query(string): Chinese/English name, nickname or equipment model; empty lists coverage.
            limit(number): Maximum terminology candidates from 1 to 20.
        """

        async def lookup():
            return self.terms.lookup(query, limit=limit)

        return await self._execute(event, "terms", "terms", query, lookup)

    @filter.llm_tool(name="helldivers_war")
    async def helldivers_war(
        self,
        event: AstrMessageEvent,
        planet_id: int = -1,
        query: str = "",
        history_hours: int = 0,
    ):
        """Read the real current galactic war and recommend support planets. For a recommendation, call once with history_hours=24: returns current orders, directional links, official-name evidence and compact histories/trends for up to three high-player candidates plus order/event history. Use those results to answer instead of repeatedly calling history. Regeneration is enemy recovery, not net liberation; a snapshot alone cannot establish ETA.

        Args:
            planet_id(number): Verified planet index, or -1 for the active war overview.
            query(string): Optional planet name or numeric index to search; use instead of planet_id.
            history_hours(number): 0 for snapshot only; set 24 for support recommendations, range 1–168. Includes histories in the same response and respects the history switch.
        """
        return await self._execute(
            event,
            "war",
            "war",
            query or str(planet_id),
            lambda: self._war(planet_id, query, history_hours),
        )

    @filter.llm_tool(name="helldivers_history")
    async def helldivers_history(
        self,
        event: AstrMessageEvent,
        kind: str = "planet",
        planet_id: int = -1,
        hours: int = 24,
        limit: int = 72,
    ):
        """Read a specific real history not already supplied by helldivers_war(history_hours=24). For kind=planet you MUST provide a confirmed planet_id; omission is invalid. Planet curves cover up to 168 hours, events/orders up to 48. Use 8–12 displayed samples for ordinary analysis; trend is computed before sampling. Never repeat identical successful calls to get a new interpretation.

        Args:
            kind(string): planet, events or orders.
            planet_id(number): Verified planet index for kind=planet; otherwise ignored.
            hours(number): Lookback hours, 1–168 for planet or 1–48 for other kinds.
            limit(number): Maximum returned samples from 1 to 200.
        """
        return await self._execute(
            event,
            "history",
            "history",
            f"{kind}:{planet_id}:{hours}",
            lambda: self._history(kind, planet_id, hours, limit),
        )

    @filter.llm_tool(name="helldivers_updates")
    async def helldivers_updates(
        self,
        event: AstrMessageEvent,
        query: str = "",
        event_id: str = "",
        offset: int = 0,
        max_chars: int = 10000,
        limit: int = 3,
    ):
        """Find and read official Helldivers 2 Steam updates to verify patch changes and localized names. Actual announcement language 6 means official Simplified Chinese; English fallback is explicitly labeled. Use event_id to read an exact result and next_offset for continuation.

        Args:
            query(string): Optional topic or patch number for the bounded recent official archive.
            event_id(string): Exact official event ID returned by search; when given, read that post.
            offset(number): Search archive offset, or character offset when reading one event.
            max_chars(number): Character limit for a read, from 1000 to 24000.
            limit(number): Search results from 1 to 10.
        """
        operation = (
            (lambda: self.news.read(event_id, offset=offset, max_chars=max_chars))
            if event_id
            else (lambda: self.news.search(query, limit=limit, offset=offset))
        )
        return await self._execute(
            event, "news", "updates", event_id or query, operation
        )

    @filter.llm_tool(name="helldivers_sources")
    async def helldivers_sources(self, event: AstrMessageEvent, probe: bool = False):
        """Describe source provenance, official-name coverage and history limits. Optionally probe real source access; successful HTTP access does not certify every fact or all-time history.

        Args:
            probe(boolean): True to test Wiki, Companion and official updates; false for capability status.
        """
        return await self._execute(
            event,
            "terms",
            "sources",
            "probe" if probe else "status",
            lambda: self._sources(probe),
        )

    async def web_api(self):
        endpoint = request.path.split(f"/{PLUGIN}/", 1)[-1]
        if endpoint == "cache/clear":
            self.http.clear_cache()
            return jsonify(ok=True, network=self.http.status())
        if endpoint == "diagnose":
            result = json.loads(
                await self._execute(
                    None, "terms", "diagnose", "", lambda: self._sources(True)
                )
            )
            return jsonify(result)
        return jsonify(
            ok=True,
            settings=self.settings,
            sources=await self._sources(),
            recent=list(self.recent),
        )

    async def terminate(self):
        await self.http.close()
