"""Official Simplified Chinese provenance and honest fallback tests."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from terms import Terminology


@pytest.mark.parametrize(
    ("query", "english", "label"),
    [
        ("强袭虫", "Charger", "强袭虫"),
        ("冲锋虫", "Charger", "强袭虫"),
        ("Hunter", "Hunter", "追猎虫"),
        ("Stalker", "Stalker", "追踪虫"),
        ("Hulk", "Hulk", "巨型者"),
        ("Illuminate", "Illuminate", "光能者"),
        ("Automaton", "Automatons", "机器人"),
        ("AR23", "AR-23 Liberator", "解放者"),
        ("SG-8", "SG-8 Punisher", "制裁者"),
        ("JAR-5", "JAR-5 Dominator", "主宰"),
        ("P-19", "P-19 Redeemer", "救世主"),
        ("CQC-20", "CQC-20 Breaching Hammer", "破门锤"),
        ("三叉戟", "LAS-13 Trident", "LAS-13“三叉戟”"),
        ("类星体加农炮", "Quasar Cannon", "类星体加农炮"),
        ("Meridia", "Meridia", "默里迪亚"),
    ],
)
def test_verified_labels_have_original_simplified_provenance(query, english, label):
    result = Terminology().lookup(query)
    assert result["exact_matches"] == 1
    item = result["items"][0]
    assert item["english"] == english
    assert item["official_label"] == label
    assert item["verified"] is True
    assert item["label_kind"] == "official_zh_cn"
    assert item["provenance"]
    assert all(source["language"] == "zh-Hans" for source in item["provenance"])


def test_enemies_with_different_localized_names_are_not_confused():
    terms = Terminology()
    assert terms.resolve_query("追猎虫") == "Hunter"
    assert terms.resolve_query("追踪虫") == "Stalker"
    assert terms.resolve_query("强袭虫") == "Charger"
    assert terms.label_for("Hulk")["label"] == "巨型者"


@pytest.mark.parametrize(
    "query", ["突击步枪", "冲锋枪", "手枪", "霰弹枪", "特种作战手枪"]
)
def test_generic_weapon_categories_do_not_resolve_to_a_single_equipment(query):
    terms = Terminology()
    assert terms.resolve_query(query) == query
    assert terms.lookup(query)["exact_matches"] == 0
    assert terms.resolve_query("StA-11") == "StA-11 SMG"
    assert terms.resolve_query("StA-11冲锋枪") == "StA-11 SMG"
    assert terms.resolve_query("MA5C突击步枪") == "MA5C Assault Rifle"


def test_community_labels_do_not_become_official(tmp_path):
    path = tmp_path / "community.json"
    path.write_text(
        json.dumps(
            {"items": [{"english": "RS-422 Railgun", "community_aliases": ["磁轨炮"]}]}
        ),
        encoding="utf-8",
    )
    item = Terminology(path).lookup("磁轨炮")["items"][0]
    assert item["english"] == "RS-422 Railgun"
    assert item["official_label"] is None
    assert item["verified"] is False
    assert item["label"] == "RS-422 Railgun"
    assert item["community_aliases"] == ["磁轨炮"]
    assert item["provenance"] == []


def test_unmatched_names_remain_unchanged_without_fabricated_translation():
    terms = Terminology()
    item = terms.label_for("Some Unverified New Planet")
    assert item["label"] == "Some Unverified New Planet"
    assert item["verified"] is False
    assert item["official_label"] is None
    assert terms.resolve_query("没有核实的译名") == "没有核实的译名"
    assert terms.lookup("没有核实的译名")["items"] == []
    assert terms.lookup("")["items"] == []


def test_known_titles_normalize_case_spacing_quotes_and_model_hyphens():
    terms = Terminology()
    assert terms.resolve_query("  las 16  ") == "LAS-16 Sickle"
    assert terms.resolve_query("LAS－16『镰刀』") == "LAS－16『镰刀』"
    assert terms.resolve_query('AR-23"解放者"') == "AR-23 Liberator"
    assert terms.resolve_query("The_Voteless") == "Voteless"
    assert terms.resolve_query("类星体加农炮") == "LAS-99 Quasar Cannon"


def test_query_with_multiple_english_hits_is_not_silently_resolved(tmp_path):
    path = tmp_path / "terms.json"
    path.write_text(
        json.dumps(
            {
                "items": [
                    {"english": "One", "community_aliases": ["模糊名"]},
                    {"english": "Two", "community_aliases": ["模糊名"]},
                ]
            }
        ),
        encoding="utf8",
    )
    terms = Terminology(path)
    result = terms.lookup("模糊名")
    assert result["ambiguous"] is True
    assert result["exact_matches"] == 2
    assert terms.resolve_query("模糊名") == "模糊名"
    assert terms.label_for("模糊名")["verified"] is False


@pytest.mark.parametrize(
    ("url", "language", "language_id", "publisher"),
    [
        ("https://helldivers.wiki.gg/zh/wiki/Charger", "zh-Hans", None, "PlayStation"),
        (
            "https://www.playstation.com/zh-hant-hk/games/helldivers-2/",
            "zh-Hans",
            None,
            "PlayStation",
        ),
        (
            "https://store.steampowered.com/news/app/553850/view/1?l=schinese",
            "zh-Hans",
            0,
            "Arrowhead Game Studios",
        ),
        (
            "https://store.steampowered.com/news/app/9999/view/1?l=schinese",
            "zh-Hans",
            6,
            "Arrowhead Game Studios",
        ),
        (
            "https://www.playstation.com/zh-hans-hk/games/helldivers-2/",
            "zh-Hant",
            None,
            "PlayStation",
        ),
        (
            "https://www.playstation.com/zh-hans-hk/games/helldivers-2/",
            "zh-Hans",
            None,
            "Community",
        ),
        (
            "https://www.playstation.com.evil.test/zh-hans-hk/",
            "zh-Hans",
            None,
            "PlayStation",
        ),
        ("http://www.playstation.com/zh-hans-hk/", "zh-Hans", None, "PlayStation"),
    ],
)
def test_untrusted_or_traditional_sources_cannot_verify_an_official_label(
    tmp_path, url, language, language_id, publisher
):
    resource = {
        "sources": {
            "untrusted": {
                "url": url,
                "language": language,
                "steam_language_id": language_id,
                "publisher": publisher,
            }
        },
        "items": [
            {
                "english": "Charger",
                "official_label": "假官简",
                "verified": True,
                "source_ids": ["untrusted"],
            }
        ],
    }
    path = tmp_path / "terms.json"
    path.write_text(json.dumps(resource), encoding="utf8")
    item = Terminology(path).label_for("Charger")
    assert item["verified"] is False
    assert item["official_label"] is None
    assert item["label"] == "Charger"


def test_result_mutation_does_not_corrupt_future_lookups():
    terms = Terminology()
    original = terms.label_for("Charger")["provenance"]
    item = terms.lookup("Charger")["items"][0]
    item["official_label"] = "wrong"
    item["provenance"][0]["source_id"] = "wrong"
    assert terms.label_for("Charger")["label"] == "强袭虫"
    assert terms.label_for("Charger")["provenance"] == original


def test_limit_coverage_and_empty_query_are_bounded():
    terms = Terminology()
    assert len(terms.lookup("AR", 1)["items"]) <= 1
    assert len(terms.lookup("AR", 999999)["items"]) <= 30
    assert len(terms.lookup("AR", "invalid")["items"]) <= 10
    assert len(terms.lookup("AR", float("inf"))["items"]) <= 10
    status = terms.status()
    assert status["official_verified"] >= 100
    assert status["unverified"] + status["official_verified"] == status["items"]
    assert terms.label_for("Some Unverified New Planet")["verified"] is False
    assert status["items"] == status["official_verified"] + status["unverified"]
    assert status["verified_at"] >= "2026-10-24"


def test_all_packaged_verified_items_have_primary_language_evidence():
    resource = json.loads(
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("terminology.json")
        .read_text(encoding="utf8")
    )
    names = set()
    for item in resource["items"]:
        assert item["english"] not in names
        names.add(item["english"])
        if not item["verified"]:
            assert item["official_label"] is None
            continue
        assert item["source_ids"]
        assert item["verified_at"] == "2026-10-24"
        for source_id in item["source_ids"]:
            source = resource["sources"][source_id]
            assert source["language"] == "zh-Hans"
            if source_id.startswith("steam-"):
                assert source["steam_language_id"] == 6
                assert len(source["content_sha256"]) == 64
                assert source["published_at"]


def test_unindexed_name_requires_wiki_followup_without_deciding_existence():
    result = Terminology().lookup("绝不存在于本地索引的测试名称")
    assert result["items"] == []
    assert result["coverage"] == "partial"
    assert result["existence"] == "not_determined"
    assert result["wiki_followup_required"] is True
