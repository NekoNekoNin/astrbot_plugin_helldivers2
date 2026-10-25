"""Evidence-backed official Simplified Chinese terminology and honest fallbacks."""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s_\-‘’“”'\"!！™®]+", "", value)


class Terminology:
    """Resolve aliases without upgrading community labels to official translations."""

    def __init__(self, path: Path | str | None = None):
        resource = Path(path) if path else Path(__file__).with_name("terminology.json")
        document = json.loads(resource.read_text(encoding="utf-8"))
        self.game_build_id = None
        if path is None:
            game_path = Path(__file__).parent / "resources/game_localization_names.json"
            if game_path.is_file():
                game = json.loads(game_path.read_text(encoding="utf-8"))
                document.setdefault("sources", {}).update(game.get("sources", {}))
                positions = {
                    _key(row["english"]): index
                    for index, row in enumerate(document["items"])
                }
                for row in game.get("items", []):
                    position = positions.get(_key(row["english"]))
                    if position is None:
                        document["items"].append(row)
                    else:
                        previous = document["items"][position]
                        aliases = [
                            *previous.get("aliases", []),
                            previous.get("official_label"),
                            *row.get("aliases", []),
                        ]
                        document["items"][position] = {
                            **previous,
                            **row,
                            "aliases": list(
                                dict.fromkeys(alias for alias in aliases if alias)
                            ),
                        }
                document["version"] = (
                    f"{document.get('version', '')}+{game.get('version', 'game')}"
                )
                document["verified_at"] = game.get(
                    "verified_at", document.get("verified_at")
                )
                document["note"] = (
                    "官方简中依据包括本地游戏us/cn同字符串ID对照、PlayStation简中与Steam实际language=6公告。"
                    "游戏资源证据优先；同名冲突保留已核对的来源或英文。词库有界，不代表所有游戏文本；装备数值另查当前版本。"
                )
                sources = list(game.get("sources", {}).values())
                self.game_build_id = sources[0].get("build_id") if sources else None
        self.version = document.get("version", "unknown")
        self.verified_at = document.get("verified_at")
        self.note = document.get("note", "")
        self._sources = document.get("sources", {})
        self._items = []
        self._index = {}
        for raw in document.get("items", []):
            item = copy.deepcopy(raw)
            evidence = []
            for source_id in item.get("source_ids", []):
                source = self._sources.get(source_id, {})
                if source.get("source_type") == "game_localization":
                    assets = source.get("assets", {})
                    pairs = item.get("game_resource_pairs", [])
                    ids = item.get("game_string_ids", [])
                    valid = (
                        source.get("publisher") == "Arrowhead Game Studios"
                        and source.get("app_id") == 553850
                        and source.get("language") == "zh-Hans"
                        and source.get("language_code") == "cn"
                        and source.get("language_hash") == "0x5942ccf7"
                        and source.get("english_language_hash") == "0x03f97b57"
                        and source.get("method") == "same_string_id"
                        and re.fullmatch(r"[0-9]+", str(source.get("build_id", "")))
                        and re.fullmatch(
                            r"[a-f0-9]{64}", source.get("manifest_sha256", "")
                        )
                        and ids
                        and all(
                            type(key) is int and 0 <= key <= 0xFFFFFFFF for key in ids
                        )
                        and pairs
                        and all(
                            assets.get(pair.get("english"), {}).get("language_hash")
                            == "0x03f97b57"
                            and assets.get(pair.get("simplified"), {}).get(
                                "language_hash"
                            )
                            == "0x5942ccf7"
                            and all(
                                re.fullmatch(
                                    r"[a-f0-9]{64}",
                                    assets.get(pair.get(key), {}).get("sha256", ""),
                                )
                                for key in ("english", "simplified")
                            )
                            for pair in pairs
                        )
                    )
                    if valid:
                        evidence.append(
                            {
                                "source_id": source_id,
                                "source_type": "game_localization",
                                "publisher": source["publisher"],
                                "app_id": 553850,
                                "language": "zh-Hans",
                                "language_code": "cn",
                                "build_id": source["build_id"],
                                "title": source["title"],
                                "string_ids": ids,
                                "resource_pairs": [
                                    {
                                        "english": pair["english"],
                                        "simplified": pair["simplified"],
                                        "english_sha256": assets[pair["english"]][
                                            "sha256"
                                        ],
                                        "simplified_sha256": assets[pair["simplified"]][
                                            "sha256"
                                        ],
                                    }
                                    for pair in pairs
                                ],
                            }
                        )
                    continue
                url = urlsplit(source.get("url", ""))
                if (
                    source.get("language") == "zh-Hans"
                    and source.get("publisher")
                    in {"PlayStation", "Arrowhead Game Studios"}
                    and url.scheme == "https"
                    and url.hostname
                    in {"www.playstation.com", "store.steampowered.com"}
                    and url.username is None
                    and url.password is None
                    and url.port in (None, 443)
                    and (
                        (
                            url.hostname == "www.playstation.com"
                            and url.path.startswith("/zh-hans-")
                        )
                        or (
                            url.hostname == "store.steampowered.com"
                            and url.path.startswith("/news/app/553850/")
                            and source.get("steam_language_id") == 6
                        )
                    )
                ):
                    evidence.append({"source_id": source_id, **source})
            item["verified"] = bool(
                item.get("verified") is True and item.get("official_label") and evidence
            )
            item["official_label"] = (
                item.get("official_label") if item["verified"] else None
            )
            item["label"] = item["official_label"] or item["english"]
            item["label_kind"] = (
                "official_zh_cn" if item["verified"] else "english_fallback"
            )
            item["provenance"] = evidence if item["verified"] else []
            item["community_aliases"] = item.get("community_aliases", [])
            self._items.append(item)
            aliases = [item["english"], item["label"]]
            aliases.extend(item.get("aliases", []))
            aliases.extend(item["community_aliases"])
            for alias in aliases:
                if key := _key(alias):
                    self._index.setdefault(key, []).append(item)

    def lookup(self, query: str, limit: int = 10) -> dict:
        """Find names and aliases while retaining each label's verification status.

        Args:
            query: Chinese, English, model code, or a known community alias.
            limit: Maximum number of items, clamped to 1 through 30.

        Returns:
            Matching items, provenance, coverage and ambiguity information.
        """
        text = str(query or "").strip()[:240]
        key = _key(text)
        try:
            maximum = max(1, min(30, int(limit)))
        except (ValueError, TypeError, OverflowError):
            maximum = 10
        matches = []
        seen = set()
        for item in self._index.get(key, []):
            if item["english"] not in seen:
                matches.append(item)
                seen.add(item["english"])
        exact_count = len(matches)
        if key and len(matches) < maximum:
            for alias, items in self._index.items():
                if key not in alias:
                    continue
                for item in items:
                    if item["english"] not in seen:
                        matches.append(item)
                        seen.add(item["english"])
        return {
            "query": text,
            "items": copy.deepcopy(matches[:maximum]),
            "total_matches": len(matches),
            "exact_matches": exact_count,
            "ambiguous": exact_count > 1,
            "coverage": "partial",
            "existence": "indexed_reference" if exact_count == 1 else "not_determined",
            "wiki_followup_required": exact_count != 1,
            "lookup_note": "本地索引是部分核对记录。空结果或模糊候选表示未完成名称解析，应继续按原中文名或型号查阅同一Wiki的中文页面；不能据此认定武器不存在、名称错误或译名非官方。",
            "verified_at": self.verified_at,
            "version": self.version,
            "note": self.note,
        }

    def resolve_query(self, query: str) -> str:
        """Return an English wiki title only for an unambiguous exact alias.

        Args:
            query: A name, model code or alias supplied by the user.

        Returns:
            The corresponding title, or the original query when unmatched/ambiguous.
        """
        matches = self.lookup(query, 30)
        if matches["exact_matches"] == 1:
            return (
                matches["items"][0].get("wiki_title") or matches["items"][0]["english"]
            )
        return query

    def label_for(self, english: str) -> dict:
        """Return a verified official label or the unchanged English fallback.

        Args:
            english: An English source name or unambiguous exact model code.

        Returns:
            A label record that always states whether official Chinese is verified.
        """
        matches = self.lookup(english, 30)
        if matches["exact_matches"] == 1:
            return matches["items"][0]
        return {
            "english": english,
            "label": english,
            "official_label": None,
            "verified": False,
            "label_kind": "source_name_unindexed"
            if re.search(r"[\u3400-\u9fff]", english)
            else "english_fallback",
            "community_aliases": [],
            "provenance": [],
            "ambiguous": matches["ambiguous"],
            "verification_status": "not_in_local_index",
            "existence": "not_determined",
        }

    def status(self) -> dict:
        return {
            "version": self.version,
            "verified_at": self.verified_at,
            "items": len(self._items),
            "official_verified": sum(item["verified"] for item in self._items),
            "unverified": sum(not item["verified"] for item in self._items),
            "game_build_id": self.game_build_id,
            "game_verified": sum(
                item["verified"] and bool(item.get("game_string_ids"))
                for item in self._items
            ),
            "planet_names_verified": sum(
                item["verified"] and item.get("category") == "planet"
                for item in self._items
            ),
            "note": self.note,
        }
