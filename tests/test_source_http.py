"""Contract tests for source boundaries, transport limits and provenance."""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from itertools import pairwise
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import source_http
from source_http import BODY_LIMIT, SourceError, SourceHTTP, validate_proxy

HOSTS = {"helldivers.wiki.gg", "api.helldivers2.dev"}
WIKI = "https://helldivers.wiki.gg/api.php"
API = "https://api.helldivers2.dev/api/v1/war"


def run(awaitable):
    return asyncio.run(awaitable)


def client(handler, config=None):
    return SourceHTTP(
        {"proxy_mode": "direct", **(config or {})},
        HOSTS,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("payload", [{}, [], 0, False, None, {"health": 0}])
def test_real_zero_and_empty_json_are_not_errors(payload):
    async def scenario():
        source = client(
            lambda request: httpx.Response(200, content=json.dumps(payload))
        )
        try:
            result = await source.json(WIKI)
            assert result["data"] == payload
            assert result["cached"] is False
            assert result["stale"] is False
            assert result["source_url"] == WIKI
            assert datetime.fromisoformat(
                result["retrieved_at"]
            ).utcoffset() == timedelta(0)
        finally:
            await source.close()

    run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "http://helldivers.wiki.gg/api.php",
        "https://helldivers.wiki.gg.evil.test/api.php",
        "https://evil.test/api.php",
        "https://helldivers.wiki.gg@evil.test/api.php",
        "https://user:password@helldivers.wiki.gg/api.php",
        "https://helldivers.wiki.gg:8443/api.php",
        "https://helldivers.wiki.gg:0/api.php",
        "https://helldivers.wiki.gg:bad/api.php",
        "https://helldivers.wiki.gg\\evil.test/api.php",
        "https://helldivers.wiki.gg/api.php\n",
        "https://helldivers.wiki.gg/api.php\t",
        "https://helldivers.wiki.gg/api.php\x7f",
        "https://helldivers.wiki.gg/api.php#fragment",
        "file:///Windows/secrets",
        " https://helldivers.wiki.gg/api.php",
        None,
    ],
)
def test_invalid_sources_never_reach_transport(url):
    def handler(request):
        raise AssertionError("Invalid source reached transport")

    async def scenario():
        source = client(handler)
        try:
            with pytest.raises(SourceError, match="HTTPS"):
                await source.json(url)
        finally:
            await source.close()

    run(scenario())


def test_metadata_canonical_url_and_headers():
    def handler(request):
        assert request.method == "GET"
        assert request.url.params["lang"] == "zh-CN"
        assert request.headers["x-super-client"] == "astrbot-helldivers2-knowledge"
        return httpx.Response(200, json={"now": 0})

    async def scenario():
        source = client(handler)
        try:
            result = await source.json(API, params={"lang": "zh-CN"})
            assert result["source_url"] == API + "?lang=zh-CN"
        finally:
            await source.close()

    run(scenario())


def test_cache_preserves_timestamp_and_isolates_returned_mutations(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(source_http.time, "monotonic", lambda: clock[0])
    count = [0]

    def handler(request):
        count[0] += 1
        return httpx.Response(200, json={"rows": [{"value": 0}]})

    async def scenario():
        source = client(handler)
        try:
            first = await source.json(WIKI, ttl_seconds=30)
            first["data"]["rows"][0]["value"] = 999
            clock[0] = 1
            second = await source.json(WIKI, ttl_seconds=30)
            assert second["cached"] is True
            assert second["data"]["rows"][0]["value"] == 0
            assert second["retrieved_at"] == first["retrieved_at"]
            clock[0] = 31
            third = await source.json(WIKI, ttl_seconds=30)
            assert third["cached"] is False
            assert count[0] == 2
        finally:
            await source.close()

    run(scenario())


def test_zero_ttl_bypasses_existing_cache_and_query_keys_do_not_collide():
    count = [0]

    def handler(request):
        count[0] += 1
        return httpx.Response(200, json={"call": count[0]})

    async def scenario():
        source = client(handler)
        try:
            await source.json(WIKI, params={"id": 1}, ttl_seconds=30)
            bypass = await source.json(WIKI, params={"id": 1}, ttl_seconds=0)
            other = await source.json(WIKI, params={"id": 2}, ttl_seconds=30)
            assert bypass["data"]["call"] == 2
            assert other["data"]["call"] == 3
            assert bypass["cached"] is False
        finally:
            await source.close()

    run(scenario())


def test_cache_entry_bound_clear_and_close():
    async def scenario():
        source = client(
            lambda request: httpx.Response(200, json={}), {"cache_max_entries": 2}
        )
        for index in range(3):
            await source.json(WIKI, params={"id": index}, ttl_seconds=30)
        assert source.status()["cache_entries"] == 2
        source.clear_cache()
        assert source.status()["cache_entries"] == 0
        assert source.status()["cache_bytes"] == 0
        await source.close()
        await source.close()
        assert source.status()["closed"] is True
        with pytest.raises(SourceError, match="关闭"):
            await source.json(WIKI)

    run(scenario())


def test_cache_byte_bound(monkeypatch):
    monkeypatch.setattr(source_http, "CACHE_BYTE_LIMIT", 6)

    async def scenario():
        source = client(lambda request: httpx.Response(200, json={"x": 1}))
        try:
            await source.json(WIKI, ttl_seconds=30)
            assert source.status()["cache_bytes"] == 0
            assert source.status()["cache_entries"] == 0
        finally:
            await source.close()

    run(scenario())


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 500])
def test_redirects_and_failures_do_not_follow_or_disclose_body(status):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            status,
            headers={"location": "https://evil.test/"},
            text="secret response body",
        )

    async def scenario():
        source = client(handler)
        try:
            with pytest.raises(SourceError) as caught:
                await source.json(WIKI)
            assert "secret" not in str(caught.value)
            assert len(calls) == 1
        finally:
            await source.close()

    run(scenario())


@pytest.mark.parametrize("content", [b"", b"<html>private error</html>", b"\xff", b"{"])
def test_bad_json_is_sanitized(content):
    async def scenario():
        source = client(lambda request: httpx.Response(200, content=content))
        try:
            with pytest.raises(SourceError, match="JSON") as caught:
                await source.json(WIKI)
            assert "private" not in str(caught.value)
            assert source.status()["cache_entries"] == 0
        finally:
            await source.close()

    run(scenario())


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(5):
            yield b"x" * (1024 * 1024)


@pytest.mark.parametrize("kind", ["json", "text"])
@pytest.mark.parametrize("declared", [False, True])
def test_declared_and_streaming_response_limit(kind, declared):
    def handler(request):
        return (
            httpx.Response(200, headers={"content-length": str(BODY_LIMIT + 1)})
            if declared
            else httpx.Response(200, stream=Chunks())
        )

    async def scenario():
        source = client(handler)
        try:
            with pytest.raises(SourceError, match="4 MiB"):
                await getattr(source, kind)(WIKI, ttl_seconds=30)
            assert source.status()["cache_entries"] == 0
        finally:
            await source.close()

    run(scenario())


def test_text_keeps_actual_content_and_cache_provenance():
    async def scenario():
        source = client(lambda request: httpx.Response(200, text="<p>绝地潜兵 2</p>"))
        try:
            result = await source.text(WIKI, ttl_seconds=30)
            cached = await source.text(WIKI, ttl_seconds=30)
            assert result["text"] == "<p>绝地潜兵 2</p>"
            assert cached["retrieved_at"] == result["retrieved_at"]
            assert cached["cached"] is True
        finally:
            await source.close()

    run(scenario())


@pytest.mark.parametrize("retry_after", ["12", "0", "not a date"])
def test_429_returns_retry_hint_without_retry(retry_after):
    calls = [0]

    def handler(request):
        calls[0] += 1
        return httpx.Response(429, headers={"retry-after": retry_after}, text="secret")

    async def scenario():
        source = client(handler)
        try:
            with pytest.raises(SourceError, match="429") as caught:
                await source.json(WIKI)
            assert calls[0] == 1
            assert "secret" not in str(caught.value)
            if retry_after.isdigit():
                assert f"{retry_after} 秒" in str(caught.value)
            assert source.status()["cache_entries"] == 0
        finally:
            await source.close()

    run(scenario())


def test_429_http_date_cooldown_stops_new_request_within_deadline():
    calls = [0]

    def handler(request):
        calls[0] += 1
        return httpx.Response(
            429,
            headers={
                "retry-after": format_datetime(
                    datetime.now(UTC) + timedelta(seconds=10)
                )
            },
        )

    async def scenario():
        source = client(handler, {"timeout_seconds": 0.05})
        try:
            with pytest.raises(SourceError, match="429"):
                await source.json(WIKI)
            with pytest.raises(SourceError, match="超时"):
                await source.json(WIKI)
            assert calls[0] == 1
        finally:
            await source.close()

    run(scenario())


def test_real_api_rate_interval_and_concurrency():
    starts = []

    async def handler(request):
        starts.append(time.monotonic())
        assert request.headers["x-super-client"] == "astrbot-helldivers2-knowledge"
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={})

    async def scenario():
        source = client(handler)
        try:
            await asyncio.gather(
                *(source.json(API, params={"id": index}) for index in range(3))
            )
            assert all(later - earlier >= 2.05 for earlier, later in pairwise(starts))
            assert source_http.WAR_API_INTERVAL == 2.1
        finally:
            await source.close()

    run(scenario())


def test_other_source_has_at_most_three_concurrent_requests():
    active = [0]
    maximum = [0]

    async def handler(request):
        active[0] += 1
        maximum[0] = max(maximum[0], active[0])
        await asyncio.sleep(0.01)
        active[0] -= 1
        return httpx.Response(200, json={})

    async def scenario():
        source = client(handler, {"max_concurrency": 100})
        try:
            await asyncio.gather(
                *(source.json(WIKI, params={"id": index}) for index in range(8))
            )
            assert maximum[0] == 3
        finally:
            await source.close()

    run(scenario())


def test_timeout_and_external_cancellation_do_not_poison_semaphore():
    arrived = asyncio.Event()
    blocked = [True]

    async def handler(request):
        arrived.set()
        if blocked[0]:
            await asyncio.sleep(1)
        return httpx.Response(200, json={})

    async def scenario():
        source = client(handler, {"timeout_seconds": 0.05, "max_concurrency": 1})
        try:
            with pytest.raises(SourceError, match="超时"):
                await source.json(WIKI, ttl_seconds=30)
            assert source.status()["cache_entries"] == 0
            arrived.clear()
            task = asyncio.create_task(source.json(WIKI, ttl_seconds=30))
            await arrived.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            blocked[0] = False
            assert (await source.json(WIKI))["data"] == {}
        finally:
            await source.close()

    run(scenario())


def test_transport_errors_never_expose_proxy_credentials():
    async def scenario():
        def handler(request):
            raise httpx.ConnectError("failed http://user:password@private-proxy/secret")

        source = client(handler)
        try:
            with pytest.raises(SourceError, match="网络") as caught:
                await source.json(WIKI)
            assert "password" not in str(caught.value)
        finally:
            await source.close()

    run(scenario())


@pytest.mark.parametrize(
    "value",
    [
        "http://user:password@127.0.0.1:7890",
        "socks5://127.0.0.1:7890",
        "http://localhost/path",
        "http://localhost:0",
        "http://localhost?secret=1",
        "http://localhost\n",
        "",
    ],
)
def test_proxy_rejects_unsupported_or_credential_endpoints(value):
    with pytest.raises(SourceError):
        validate_proxy(value)


def test_proxy_modes_and_no_implicit_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@invalid.test")
    monkeypatch.setattr(source_http, "_system_proxy", lambda: "http://127.0.0.1:7890")

    async def scenario():
        sources = [
            client(
                lambda request: httpx.Response(200, json={}), {"proxy_mode": "direct"}
            ),
            client(
                lambda request: httpx.Response(200, json={}), {"proxy_mode": "system"}
            ),
            client(
                lambda request: httpx.Response(200, json={}),
                {"proxy_mode": "custom", "proxy_url": "http://localhost:7890"},
            ),
        ]
        try:
            assert sources[0].status()["proxy_active"] is False
            assert sources[1].status()["proxy_active"] is True
            assert sources[2].status()["proxy_active"] is True
            for source in sources:
                assert "secret" not in str(source.status())
                await source.json(WIKI)
        finally:
            for source in sources:
                await source.close()
        with pytest.raises(SourceError):
            client(None, {"proxy_mode": "invalid"})
        with pytest.raises(SourceError):
            client(None, {"proxy_mode": "custom", "proxy_url": ""})

    run(scenario())
