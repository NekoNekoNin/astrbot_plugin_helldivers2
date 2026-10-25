"""Same-ID game localization evidence and Chinese-first routing regressions."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terms import Terminology


@pytest.mark.parametrize(
    ("english", "chinese", "string_id"),
    [
        ("Choepessa IV", "科埃佩萨IV", 1213932068),
        ("Luxuriant", "富源", 2834895362),
        ("Grafmere", "格拉夫米尔", 1706936029),
        ("HMG Emplacement", "重机枪部署支架", 1366895198),
        ("Orbital Railcannon Strike", "轨道炮攻击", 826884584),
        ("Anti-Materiel Rifle", "反器材步枪", 3162817893),
    ],
)
def test_packaged_game_names_have_same_id_simplified_evidence(
    english, chinese, string_id
):
    terms = Terminology()
    item = terms.label_for(english)
    assert item["verified"] is True
    assert item["official_label"] == chinese
    assert string_id in item["game_string_ids"]
    source = item["provenance"][0]
    assert source["source_type"] == "game_localization"
    assert source["language_code"] == "cn"
    assert source["build_id"] == "26088801"
    assert source["resource_pairs"]
    assert terms.resolve_query(chinese) == english


@pytest.mark.parametrize(
    "change",
    [
        "traditional",
        "missing_id",
        "fake_hash",
        "wrong_app",
        "machine_translation",
        "missing_pair",
    ],
)
def test_invalid_localization_evidence_cannot_certify_official_names(tmp_path, change):
    root = Path(__file__).resolve().parents[1]
    document = json.loads(
        (root / "resources/game_localization_names.json").read_text(encoding="utf-8")
    )
    item = copy.deepcopy(
        next(row for row in document["items"] if row["english"] == "Choepessa IV")
    )
    source_id = item["source_ids"][0]
    source = document["sources"][source_id]
    if change == "traditional":
        source["language_hash"] = "0x9eba952a"
        source["language_code"] = "tc"
    elif change == "missing_id":
        item["game_string_ids"] = []
    elif change == "fake_hash":
        source["manifest_sha256"] = "not-a-hash"
    elif change == "wrong_app":
        source["app_id"] = 1
    elif change == "machine_translation":
        source["method"] = "machine_translation"
    elif change == "missing_pair":
        item["game_resource_pairs"] = []
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"sources": {source_id: source}, "items": [item]}), encoding="utf-8"
    )
    assert Terminology(path).label_for("Choepessa IV")["verified"] is False


def test_unresolved_same_english_name_is_not_forced_to_one_translation():
    # The game uses this English name in more than one context with two labels.
    assert Terminology().label_for("Resilience")["verified"] is False


@pytest.mark.parametrize(
    "query", ["爆裂铳", "R-36", "R-36 Eruptor", "R-36_爆裂铳", "Eruptor"]
)
def test_eruptor_resolves_from_game_official_chinese_and_model(query):
    terms = Terminology()
    result = terms.lookup(query)
    assert result["exact_matches"] == 1
    item = result["items"][0]
    assert item["english"] == "R-36 Eruptor"
    assert item["official_label"] == "爆裂铳"
    assert item["verified"] is True
    assert 385177318 in item["game_string_ids"]
    assert terms.resolve_query(query) == "R-36 Eruptor"


def test_named_index_is_bounded_and_contains_no_dialogue_dump():
    root = Path(__file__).resolve().parents[1]
    document = json.loads(
        (root / "resources/game_localization_names.json").read_text(encoding="utf-8")
    )
    assert len(document["items"]) < 1000
    assert all(len(item["official_label"]) <= 160 for item in document["items"])
    assert not any("\n" in item["official_label"] for item in document["items"])
    assert not any(item["english"] == "Select Sector" for item in document["items"])
    status = Terminology().status()
    assert status["planet_names_verified"] >= 380
    assert status["game_verified"] > 500
