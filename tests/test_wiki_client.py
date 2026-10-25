"""Behavior tests for wiki source selection, table fidelity and continuations."""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_helldivers2.source_http import SourceError, SourceHTTP
from astrbot_plugin_helldivers2.wiki_client import (
    WikiClient,
    _render_document,
    _table_rows,
    _title_reference,
)
from bs4 import BeautifulSoup

HTML = """
<div class='mw-parser-output'>
<table class='tabs'><tr><td>Helldivers 1 navigation</td></tr></table>
<div>Last updated: 1.007.000 Current patch: 1.007.201</div>
<div class='druid-infobox'>Recoil 11 Damage 90</div>
<h2><span id='Detailed_Weapon_Statistics'>Detailed Weapon Statistics</span></h2>
<p>Use detailed values for calculation.</p>
<table><tr><th>AR-23</th><th>Value</th></tr>
<tr><td>Recoil</td><td>10.5</td></tr>
<tr><td>Damage</td><td>90 Ballistic</td></tr>
<tr><td>Durable Damage</td><td>22 Ballistic</td></tr>
<tr><td>Fire Rate</td><td>640 rpm</td></tr></table>
<h3><span id='Projectile'>Projectile</span></h3><p>900 m/s</p>
<h2><span id='Attachments'>Attachments</span></h2>
<table><tr><th>Weapon</th><th>Attachment</th><th>Capacity</th></tr>
<tr><td rowspan='2'>AR-23</td><td>Short magazine</td><td>30 rounds</td></tr>
<tr><td>Drum magazine</td><td>60 rounds</td></tr></table>
<div class='navbox'>No unrelated weapons</div>
<script>no executable instructions</script>
</div>
"""
SECTIONS = [
    {
        "line": "Detailed Weapon Statistics",
        "anchor": "Detailed_Weapon_Statistics",
        "index": "1",
        "level": "2",
    },
    {"line": "Projectile", "anchor": "Projectile", "index": "2", "level": "3"},
    {"line": "Attachments", "anchor": "Attachments", "index": "3", "level": "2"},
]


def run(awaitable):
    return asyncio.run(awaitable)


def transport(html=HTML, sections=None, title="AR-23 Liberator"):
    def handler(request):
        p = request.url.params
        if p.get("action") == "parse":
            return httpx.Response(
                200,
                json={
                    "parse": {
                        "title": title,
                        "pageid": 2341,
                        "revid": 139629,
                        "text": html,
                        "sections": SECTIONS if sections is None else sections,
                    }
                },
            )
        if p.get("prop") == "revisions":
            assert p["revids"] == "139629"
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "revisions": [
                                    {
                                        "revid": 139629,
                                        "timestamp": "2026-10-21T19:51:08Z",
                                    }
                                ]
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    return httpx.MockTransport(handler)


def source(mock):
    return SourceHTTP({"proxy_mode": "direct"}, {"helldivers.wiki.gg"}, transport=mock)


@pytest.mark.parametrize(
    "reference,language,title",
    [
        ("AR-23 Liberator", "en", "AR-23 Liberator"),
        ("AR-23_解放者", "zh", "AR-23 解放者"),
        (
            "https://helldivers.wiki.gg/zh/wiki/AR-23_%E8%A7%A3%E6%94%BE%E8%80%85",
            "zh",
            "AR-23 解放者",
        ),
        (
            "https://helldivers.wiki.gg/wiki/AR-23_Liberator#Detailed_Weapon_Statistics",
            "en",
            "AR-23 Liberator",
        ),
        (
            "https://helldivers.wiki.gg/wiki/AX%2FAR-23_Guard_Dog",
            "en",
            "AX/AR-23 Guard Dog",
        ),
    ],
)
def test_only_valid_article_reference_shapes(reference, language, title):
    assert _title_reference(reference) == (language, title)


@pytest.mark.parametrize(
    "reference",
    [
        "http://helldivers.wiki.gg/wiki/AR-23",
        "https://example.com/wiki/AR-23",
        "https://helldivers.wiki.gg.evil.test/wiki/AR-23",
        "https://a@helldivers.wiki.gg/wiki/AR-23",
        "https://helldivers.wiki.gg:99/wiki/AR-23",
        "https://helldivers.wiki.gg/api.php",
        "https://helldivers.wiki.gg/wiki/AR-23?action=raw",
        "https://helldivers.wiki.gg/wiki/Template%3ATest",
        "Template:Test",
        "File:Test.png",
        "Helldivers 1:AR-19 Liberator",
        "Helldivers 1",
        "",
        "A\nB",
        "[redirection]",
    ],
)
def test_reject_non_articles_and_other_game(reference):
    with pytest.raises(ValueError):
        _title_reference(reference)


def test_extract_rendered_values_and_merged_cells():
    text, tables, headings = _render_document(HTML)
    assert "10.5" in text and "640 rpm" in text
    assert "Helldivers 1" not in text and "No unrelated" not in text
    assert "no executable" not in text
    assert headings[0]["title"] == "Detailed Weapon Statistics"
    assert tables[0]["rows"][2] == ["Damage", "90 Ballistic"]
    assert tables[1]["rows"][2] == ["AR-23", "Drum magazine", "60 rounds"]


def test_colspan_and_invalid_spans_do_not_shift_numeric_values():
    table = BeautifulSoup(
        "<table><tr><th colspan='2'>Damage</th></tr><tr><td rowspan='bad'>Main</td><td>90</td></tr></table>",
        "html.parser",
    ).table
    assert _table_rows(table) == [["Damage", "Damage"], ["Main", "90"]]


def test_actual_wiki_armor_and_penetration_icons_survive_table_and_text_rendering():
    # These cells preserve the real Charger and AR-23 render markup structure.
    html = """<div class='mw-parser-output'><h2>Anatomy</h2><table>
    <tr><th>Part Name</th><th>Health</th><th>AV</th></tr>
    <tr><td>Head</td><td>1,200</td><td><span class="Templateicons icon-outline"><a href="/wiki/Damage#Armor_Penetration_&amp;_Armor_Values" title="Damage"><img alt="" data-file-height="221" data-file-width="188" decoding="async" height="221" loading="lazy" src="/images/Armor_AV4_Icon.png?6d16b7" width="188"/></a> <a href="/wiki/Damage#Armor_Penetration_&amp;_Armor_Values" title="Damage">Heavy</a></span></td></tr>
    </table><h2>Detailed Weapon Statistics</h2><table>
    <tr><td>Direct</td><td><span class="Templateicons icon-outline"><a href="/wiki/Damage#Armor_Penetration_&amp;_Armor_Values" title="Damage"><img alt="" data-file-height="221" data-file-width="188" decoding="async" height="221" loading="lazy" src="/images/Armor_AP2_Icon.png?253eaf" width="188"/></a> <a href="/wiki/Damage#Armor_Penetration_&amp;_Armor_Values" title="Damage">Light</a></span></td></tr>
    </table></div>"""
    text, tables, _ = _render_document(html)
    assert tables[0]["rows"][1] == ["Head", "1,200", "[AV4] Heavy"]
    assert tables[1]["rows"][0] == ["Direct", "[AP2] Light"]
    assert "Head | 1,200 | [AV4] Heavy" in text
    assert "Direct | [AP2] Light" in text


def test_armor_icons_are_exact_same_source_filenames_and_deduplicated():
    table = BeautifulSoup(
        """<table><tr><td>
        <img src='/images/Armor_AP0_Icon.png?q=1'>Unarmored
        <img src='https://helldivers.wiki.gg/images/Armor_AP0_Icon.png'>
        <img src='/images/thumb/Armor_AV4_Icon.png/20px-Armor_AV4_Icon.png'>Heavy
        <img src='https://evil.test/images/Armor_AV9_Icon.png'>
        <img src='/images/NotArmor_AP7_Icon.png'>
        <img src='/images/Armor_AP8_Icon.png.fake'>
        <img alt='Armor_AV6_Icon.png'>
        </td></tr></table>""",
        "html.parser",
    ).table
    first = _table_rows(table)[0][0]
    second = _table_rows(table)[0][0]
    assert first == second
    assert first.count("[AP0]") == 1 and first.count("[AV4]") == 1
    assert "Unarmored" in first and "Heavy" in first
    assert "[AV9]" not in first and "[AP7]" not in first
    assert "[AP8]" not in first and "[AV6]" not in first


def test_read_revision_provenance_and_numerical_conditions():
    async def scenario():
        http = source(transport())
        try:
            result = await WikiClient(http).read("AR-23 Liberator")
            assert result["revision_id"] == 139629
            assert result["modified_at"] == "2026-10-21T19:51:08Z"
            assert result["revision_url"].endswith("?oldid=139629")
            assert result["source_type"] == "community_wiki"
            assert result["stale"] is False
            assert "Current patch: 1.007.201" in result["versions"]
            assert ["Durable Damage", "22 Ballistic"] in result["tables"][0]["rows"]
            assert any("详细武器" in note for note in result["analysis_notes"])
        finally:
            await http.close()

    run(scenario())


@pytest.mark.parametrize(
    "section", ["1", "Detailed_Weapon_Statistics", "detailed weapon statistics"]
)
def test_section_selection_keeps_subsections_and_excludes_other_tables(section):
    async def scenario():
        http = source(transport())
        try:
            result = await WikiClient(http).read("AR-23 Liberator", section)
            assert "900 m/s" in result["text"]
            assert "Drum magazine" not in result["text"]
            assert len(result["tables"]) == 1
            assert result["next_offset"] is None
        finally:
            await http.close()

    run(scenario())


def test_unknown_section_and_invalid_offsets_are_explicit():
    async def scenario():
        http = source(transport())
        try:
            client = WikiClient(http)
            with pytest.raises(ValueError, match="sections"):
                await client.read("AR-23 Liberator", "Not a heading")
            with pytest.raises(ValueError, match="offset"):
                await client.read("AR-23 Liberator", offset=-1)
            with pytest.raises(ValueError, match="offset"):
                await client.read("AR-23 Liberator", offset=999999)
        finally:
            await http.close()

    run(scenario())


def test_pagination_is_lossless_and_tables_follow_window():
    async def scenario():
        html = (
            "<div class='mw-parser-output'><h2>Data</h2><table>"
            + "".join(
                f"<tr><td>Target {i}</td><td>{i} dmg</td></tr>" for i in range(130)
            )
            + "</table><p>End.</p></div>"
        )
        http = source(transport(html=html, sections=[]))
        try:
            client = WikiClient(http)
            chunks, cursor, counts = [], 0, []
            while True:
                result = await client.read(
                    "AR-23 Liberator", offset=cursor, max_chars=1000
                )
                assert len(result["text"]) <= 1000
                chunks.append(result["text"])
                counts.extend(t["row_start"] for t in result["tables"])
                if result["next_offset"] is None:
                    break
                assert result["next_offset"] > cursor
                cursor = result["next_offset"]
            assert "".join(chunks) == _render_document(html)[0]
            assert max(counts) > 50
        finally:
            await http.close()

    run(scenario())


def test_missing_article_is_not_fabricated():
    async def scenario():
        http = source(
            httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"error": {"code": "missingtitle"}}
                )
            )
        )
        try:
            with pytest.raises(SourceError):
                await WikiClient(http).read("Missing")
        finally:
            await http.close()

    run(scenario())


def test_redirect_to_non_main_namespace_is_rejected():
    async def scenario():
        http = source(transport(title="Template:Weapon"))
        try:
            with pytest.raises(ValueError):
                await WikiClient(http).read("Weapon")
        finally:
            await http.close()

    run(scenario())


def test_search_dual_language_namespaces_and_untrusted_snippets():
    calls = []

    def handler(request):
        calls.append(request.url)
        assert request.url.params["srnamespace"] == "0"
        if request.url.path.startswith("/zh/"):
            return httpx.Response(
                200,
                json={
                    "query": {
                        "search": [
                            {
                                "title": "AR-23 解放者",
                                "pageid": 4330,
                                "ns": 0,
                                "snippet": "<b>90</b> 射弹",
                                "timestamp": "2026-10-18T20:06:14Z",
                            },
                            {"title": "Template:Weapon", "ns": 10},
                            {"title": "Helldivers 1:AR-19", "ns": 0},
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "query": {
                    "search": [{"title": "AR-23 Liberator", "ns": 0, "pageid": 2341}]
                }
            },
        )

    async def scenario():
        http = source(httpx.MockTransport(handler))
        try:
            result = await WikiClient(http).search("AR-23 Liberator", 5)
            assert len(result["items"]) == 2
            assert result["items"][0]["title"] == "AR-23 解放者"
            assert result["items"][0]["name_provenance"] == "wiki_title"
            assert result["source_family"] == "helldivers.wiki.gg"
            assert result["items"][0]["snippet"] == "90 射弹"
            assert len(calls) == 2
        finally:
            await http.close()

    run(scenario())


def test_partial_search_failure_is_reported_without_losing_evidence():
    def handler(request):
        if request.url.path.startswith("/zh/"):
            return httpx.Response(403)
        return httpx.Response(
            200, json={"query": {"search": [{"title": "AR-23 Liberator", "ns": 0}]}}
        )

    async def scenario():
        http = source(httpx.MockTransport(handler))
        try:
            result = await WikiClient(http).search("解放者")
            assert len(result["items"]) == 1 and result["errors"]
        finally:
            await http.close()

    run(scenario())
