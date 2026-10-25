"""Build a small named-entity index from same-ID English and Simplified game strings.

Input is Filediver's JSON export; only named planets, sectors, existing terms and
explicit stratagem names are retained. Full dialogues stay outside the plugin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
ENGLISH_HASH = "0x03f97b57"
SIMPLIFIED_HASH = "0x5942ccf7"


def normalized(value: str) -> str:
    """Normalize punctuation and case for name matching, never translate text.

    Args:
        value: A complete English entity name or alias.

    Returns:
        A case-folded name key.
    """
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def build_index(exports: Path, manifest: Path, output: Path) -> dict:
    """Pair actual locale keys and retain only unambiguous named entities.

    Args:
        exports: Directory of .strings.json files from the installed game.
        manifest: Steam appmanifest_553850.acf for the same installation.
        output: Destination derived dictionary; no game files are written.

    Returns:
        Coverage, conflicts and representative paired names.

    Raises:
        ValueError: If language tables, manifest or required coverage are invalid.
    """
    manifest_bytes = manifest.read_bytes()
    manifest_text = manifest_bytes.decode("utf-8-sig")
    if not re.search(r'"appid"\s+"553850"', manifest_text):
        raise ValueError("Expected the HELLDIVERS 2 Steam manifest")
    build_match = re.search(r'"buildid"\s+"([0-9]+)"', manifest_text)
    if build_match is None:
        raise ValueError("The installation build ID is unavailable")
    build_id = build_match[1]
    tables = {ENGLISH_HASH: defaultdict(list), SIMPLIFIED_HASH: defaultdict(list)}
    assets = {}
    files = sorted(exports.glob("*.strings.json"))
    if len(files) > 512:
        raise ValueError("Too many exported localization tables")
    for path in files:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Localization table is unexpectedly large")
        raw = path.read_bytes()
        document = json.loads(raw)
        language = (document.get("Language") or {}).get("Hash")
        if language not in tables:
            continue
        resource = path.name.removesuffix(".json")
        assets[resource] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "language_hash": language,
        }
        for row in document.get("Items", []):
            if (
                type(row.get("Key")) is int
                and 0 <= row["Key"] <= 0xFFFFFFFF
                and isinstance(row.get("Value"), str)
            ):
                tables[language][row["Key"]].append((row["Value"], resource))
    english, chinese = tables[ENGLISH_HASH], tables[SIMPLIFIED_HASH]
    if len(english) < 100 or len(chinese) < 100:
        raise ValueError(
            "Actual English and cn Simplified tables are required; Traditional is not accepted"
        )
    name_ids = defaultdict(set)
    for key, values in english.items():
        for text, _ in values:
            if len(text) <= 160:
                name_ids[normalized(text)].add(key)
    base = json.loads((PLUGIN / "terminology.json").read_text(encoding="utf-8"))
    planets = json.loads(
        (PLUGIN / "resources/companion_planets.json").read_text(encoding="utf-8")
    )["planets"]
    planet_keys = {normalized(row["name"]) for row in planets.values()}
    planet_resource_counts = defaultdict(int)
    for values in english.values():
        for name, resource in values:
            if normalized(name) in planet_keys:
                planet_resource_counts[resource] += 1
    planet_resources = {
        name for name, count in planet_resource_counts.items() if count >= 20
    }
    targets = {}
    for planet in planets.values():
        targets[planet["name"]] = {
            "category": "planet",
            "planet_index": planet["index"],
        }
    for item in base["items"]:
        target = targets.setdefault(
            item["english"], {"category": item.get("category", "game_term")}
        )
        target["wiki_title"] = item.get("wiki_title", item["english"])
    for name in ("HMG Emplacement", "Orbital Railcannon Strike", "Anti-Materiel Rifle"):
        targets[name] = {"category": "stratagem", "wiki_title": name}
    targets["R-36 Eruptor"] = {"category": "weapon", "wiki_title": "R-36 Eruptor"}
    for values in english.values():
        for name, resource in values:
            if resource in planet_resources and re.fullmatch(
                r"[A-Za-z0-9 '\-]{1,60} Sector", name, re.IGNORECASE
            ):
                targets.setdefault(name.title(), {"category": "sector"})
    source_id = f"game-localization-{build_id}"
    items, unmatched, conflicts = [], [], []
    for name, extra in targets.items():
        alternatives = {name, extra.get("wiki_title", name)}
        for alternative in tuple(alternatives):
            alternatives.add(
                re.sub(r"^[A-Z]+(?:/[A-Z]+)?-[0-9]+[A-Z]?\s+", "", alternative)
            )
        ids = set().union(
            *(name_ids.get(normalized(text), set()) for text in alternatives)
        )
        valid_ids = []
        translations = set()
        for key in ids:
            if extra.get("category") in {"planet", "sector"} and not any(
                resource in planet_resources for _, resource in english[key]
            ):
                continue
            en_values = {value for value, _ in english[key]}
            zh_values = {
                value.strip() for value, _ in chinese.get(key, []) if value.strip()
            }
            if len(en_values) != 1 or len(zh_values) != 1:
                continue
            label = next(iter(zh_values))
            if len(label) > 160 or any(char in label for char in "\n\r\t<>{}"):
                continue
            valid_ids.append(key)
            translations.add(label)
        if not valid_ids:
            unmatched.append(name)
            continue
        if len(translations) != 1:
            conflicts.append({"english": name, "translations": sorted(translations)})
            continue
        pairs = sorted(
            {
                (en_resource, zh_resource)
                for key in valid_ids
                for _, en_resource in english[key]
                for _, zh_resource in chinese[key]
            }
        )
        aliases = {value for key in valid_ids for value, _ in english[key]}
        model = re.match(r"^([A-Z]+(?:/[A-Z]+)?-[0-9]+[A-Z]?)\s+", name)
        if model:
            aliases.update({model[1], model[1] + " " + next(iter(translations))})
        items.append(
            {
                "english": name,
                "official_label": next(iter(translations)),
                "verified": True,
                "aliases": sorted(aliases),
                "source_ids": [source_id],
                "game_string_ids": sorted(valid_ids),
                "game_resource_pairs": [
                    {"english": en, "simplified": zh} for en, zh in pairs
                ],
                **extra,
            }
        )
    if not any(row["english"] == "Choepessa IV" for row in items):
        raise ValueError("Expected known planet name coverage was not found")
    date = datetime.now(UTC).astimezone(timezone(timedelta(hours=8))).date().isoformat()
    source = {
        "source_type": "game_localization",
        "publisher": "Arrowhead Game Studios",
        "app_id": 553850,
        "build_id": build_id,
        "language": "zh-Hans",
        "language_code": "cn",
        "language_hash": SIMPLIFIED_HASH,
        "english_language_code": "us",
        "english_language_hash": ENGLISH_HASH,
        "method": "same_string_id",
        "verified_at": date,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "reader": "xypwn/filediver v0.7.63",
        "assets": assets,
        "title": f"本地绝地潜兵2简体中文本地化（Steam构建{build_id}，相同字符串ID对照）",
    }
    document = {
        "schema_version": 1,
        "version": f"steam-{build_id}",
        "verified_at": date,
        "sources": {source_id: source},
        "items": items,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "build_id": build_id,
        "english_keys": len(english),
        "simplified_keys": len(chinese),
        "named_entities": len(items),
        "planets": sum(row.get("category") == "planet" for row in items),
        "sectors": sum(row.get("category") == "sector" for row in items),
        "unmatched": unmatched,
        "conflicts": conflicts,
        "assets": len(assets),
        "examples": [
            {key: row[key] for key in ("english", "official_label", "game_string_ids")}
            for row in items
            if row["english"]
            in {
                "Choepessa IV",
                "Luxuriant",
                "Grafmere",
                "HMG Emplacement",
                "Orbital Railcannon Strike",
                "Anti-Materiel Rifle",
            }
        ],
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=PLUGIN / "resources/game_localization_names.json"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = build_index(args.exports, args.manifest, args.output)
    if args.report:
        args.report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False))
