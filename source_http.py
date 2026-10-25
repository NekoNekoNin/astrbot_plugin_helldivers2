"""Bounded HTTPS reads for explicitly allowed Helldivers knowledge sources."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import sys
import time
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

BODY_LIMIT = 4 * 1024 * 1024
CACHE_BYTE_LIMIT = 32 * 1024 * 1024
WAR_API_HOST = "api.helldivers2.dev"
WAR_API_INTERVAL = 2.1


class SourceError(Exception):
    """An actionable source error safe to display without response contents."""


def _number(value, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
        if isinstance(value, bool) or not math.isfinite(result):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        result = default
    return max(minimum, min(maximum, result))


def validate_proxy(value: str) -> str:
    """Validate a proxy endpoint without credentials or URL components.

    Args:
        value: An HTTP or HTTPS proxy endpoint.

    Returns:
        Validated proxy URL without a trailing slash.

    Raises:
        SourceError: If the endpoint is malformed or contains credentials.
    """
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port == 0
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in value
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise SourceError(
            "代理应为 http(s)://主机:端口，不包含凭据、路径或参数"
        ) from None
    return value.rstrip("/")


def _system_proxy() -> str:
    """Read Windows Internet Settings without changing system configuration."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return ""
            value = str(winreg.QueryValueEx(key, "ProxyServer")[0]).strip()
        if "=" in value:
            entries = dict(
                item.strip().split("=", 1) for item in value.split(";") if "=" in item
            )
            value = entries.get("https", entries.get("http", ""))
        if value and "://" not in value:
            value = "http://" + value
        return validate_proxy(value) if value else ""
    except OSError:
        return ""


class SourceHTTP:
    """Read fixed HTTPS sources with total deadlines, bounded cache and rate limits."""

    def __init__(self, config: dict, allowed_hosts: set[str], *, transport=None):
        self.allowed_hosts = frozenset(str(host).lower() for host in allowed_hosts)
        self.timeout_seconds = _number(config.get("timeout_seconds"), 30, 0.05, 120)
        self.max_concurrency = int(_number(config.get("max_concurrency"), 3, 1, 3))
        self.cache_max_entries = int(
            _number(config.get("cache_max_entries"), 128, 1, 128)
        )
        self.proxy_mode = str(config.get("proxy_mode", "system"))
        if self.proxy_mode not in {"system", "direct", "custom"}:
            raise SourceError("代理模式应为 system、direct 或 custom")
        proxy = ""
        if self.proxy_mode == "system":
            proxy = _system_proxy()
        elif self.proxy_mode == "custom":
            proxy = validate_proxy(str(config.get("proxy_url", "")))
        self._proxy_active = bool(proxy)
        self._client = httpx.AsyncClient(
            transport=transport,
            proxy=proxy if transport is None and proxy else None,
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=self.max_concurrency,
                max_keepalive_connections=self.max_concurrency,
            ),
            headers={
                "User-Agent": "AstrBot-Helldivers2-Knowledge/1.0",
                "Accept": "application/json, text/html;q=0.9, text/plain;q=0.8",
            },
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._war_lock = asyncio.Lock()
        self._war_next_at = 0.0
        self._cooldown_until = {}
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._requests = 0
        self._cache_hits = 0
        self._closed = False

    def _url(self, value: str, params=None) -> httpx.URL:
        try:
            if not isinstance(value, str):
                raise TypeError
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in self.allowed_hosts
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in {None, 443}
                or parsed.fragment
                or "\\" in value
                or any(ord(char) < 33 or ord(char) == 127 for char in value)
            ):
                raise ValueError
            url = httpx.URL(value)
            return url.copy_merge_params(params) if params is not None else url
        except (ValueError, TypeError, httpx.InvalidURL):
            raise SourceError("只允许已配置资料来源的 HTTPS 地址") from None

    def _purge_cache(self):
        now = time.monotonic()
        for key in list(self._cache):
            if self._cache[key][0] <= now:
                self._cache_bytes -= self._cache.pop(key)[1]

    async def _read(self, kind: str, value: str, params, ttl_seconds) -> dict:
        """Fetch and decode one response while bounding queueing and response bytes.

        Args:
            kind: Either json or text.
            value: Fixed-source HTTPS URL.
            params: Optional URL query parameters.
            ttl_seconds: Cache lifetime; zero explicitly bypasses stored responses.

        Returns:
            Payload with source URL, original retrieval time and cache provenance.

        Raises:
            SourceError: For invalid sources, limits, source failures or bad JSON.
        """
        if self._closed:
            raise SourceError("资料连接已关闭")
        url = self._url(value, params)
        ttl = _number(ttl_seconds, 0, 0, 86400)
        cache_key = (kind, str(url))
        self._purge_cache()
        if ttl and cache_key in self._cache:
            self._cache_hits += 1
            self._cache.move_to_end(cache_key)
            result = copy.deepcopy(self._cache[cache_key][2])
            result["cached"] = True
            return result

        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with self._semaphore:
                    headers = {}
                    if url.host == WAR_API_HOST:
                        headers["X-Super-Client"] = "astrbot-helldivers2-knowledge"
                        async with self._war_lock:
                            while (
                                delay := max(
                                    self._war_next_at,
                                    self._cooldown_until.get(url.host, 0),
                                )
                                - time.monotonic()
                            ) > 0:
                                await asyncio.sleep(delay)
                            self._war_next_at = time.monotonic() + WAR_API_INTERVAL
                    else:
                        while (
                            delay := self._cooldown_until.get(url.host, 0)
                            - time.monotonic()
                        ) > 0:
                            await asyncio.sleep(delay)
                    self._requests += 1
                    async with self._client.stream(
                        "GET", url, headers=headers
                    ) as response:
                        if response.status_code == 429:
                            retry = response.headers.get("retry-after", "")
                            wait = None
                            try:
                                wait = max(0, int(retry))
                            except (ValueError, TypeError):
                                try:
                                    until = parsedate_to_datetime(retry)
                                    if until.tzinfo is None:
                                        until = until.replace(tzinfo=UTC)
                                    wait = max(
                                        0,
                                        math.ceil(
                                            (until - datetime.now(UTC)).total_seconds()
                                        ),
                                    )
                                except (ValueError, TypeError, OverflowError):
                                    pass
                            if wait is not None:
                                self._cooldown_until[url.host] = max(
                                    self._cooldown_until.get(url.host, 0),
                                    time.monotonic() + wait,
                                )
                            label = (
                                f"，请至少 {wait} 秒后重试"
                                if wait is not None
                                else "，请稍后重试"
                            )
                            raise SourceError("资料来源限流（HTTP 429）" + label)
                        if 300 <= response.status_code < 400:
                            raise SourceError(
                                "资料来源返回重定向，已停止；请使用受支持的资料地址"
                            )
                        if response.status_code != 200:
                            raise SourceError(
                                f"资料来源请求失败（HTTP {response.status_code}）"
                            )
                        declared = response.headers.get("content-length", "")
                        if declared.isdigit() and (
                            len(declared) > 12 or int(declared) > BODY_LIMIT
                        ):
                            raise SourceError("资料响应超过 4 MiB 限制")
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > BODY_LIMIT:
                                raise SourceError("资料响应超过 4 MiB 限制")
                            data.extend(chunk)
                        if kind == "json":
                            try:
                                payload = json.loads(data)
                            except (ValueError, UnicodeError, RecursionError):
                                raise SourceError(
                                    "资料来源未返回有效 JSON 数据"
                                ) from None
                        else:
                            encoding = response.encoding or "utf-8"
                            try:
                                payload = data.decode(encoding, errors="replace")
                            except LookupError:
                                payload = data.decode("utf-8", errors="replace")
        except (httpx.TimeoutException, TimeoutError):
            raise SourceError("资料请求超时，请稍后重试或检查代理设置") from None
        except httpx.HTTPError:
            raise SourceError("无法连接资料来源，请检查网络或代理设置") from None

        result = {
            "data" if kind == "json" else "text": payload,
            "source_url": str(url),
            "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "cached": False,
            "stale": False,
        }
        if ttl:
            if cache_key in self._cache:
                self._cache_bytes -= self._cache.pop(cache_key)[1]
            self._cache[cache_key] = (
                time.monotonic() + ttl,
                len(data),
                copy.deepcopy(result),
            )
            self._cache_bytes += len(data)
            while (
                len(self._cache) > self.cache_max_entries
                or self._cache_bytes > CACHE_BYTE_LIMIT
            ):
                _, (_, size, _) = self._cache.popitem(last=False)
                self._cache_bytes -= size
        return result

    async def json(self, url: str, params=None, ttl_seconds=0) -> dict:
        return await self._read("json", url, params, ttl_seconds)

    async def text(self, url: str, params=None, ttl_seconds=0) -> dict:
        return await self._read("text", url, params, ttl_seconds)

    def status(self) -> dict:
        self._purge_cache()
        return {
            "closed": self._closed,
            "proxy_mode": self.proxy_mode,
            "proxy_active": self._proxy_active,
            "allowed_hosts": sorted(self.allowed_hosts),
            "timeout_seconds": self.timeout_seconds,
            "max_concurrency": self.max_concurrency,
            "cache_entries": len(self._cache),
            "cache_bytes": self._cache_bytes,
            "requests": self._requests,
            "cache_hits": self._cache_hits,
        }

    def clear_cache(self):
        self._cache.clear()
        self._cache_bytes = 0

    async def close(self):
        if not self._closed:
            self._closed = True
            self.clear_cache()
            await self._client.aclose()
