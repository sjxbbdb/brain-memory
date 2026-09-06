"""可验证的信息来源适配器。

第一版只访问明确的白名单来源：Wikipedia 的官方 MediaWiki JSON API、
arXiv 的官方 Atom API，以及由操作者配置的 RSS/Atom feed。这里不抓取
搜索结果页面，也不把占位文本伪装成事实。

设计边界：
* 所有请求都是 HTTPS、无重定向、有限时长和有限响应体；
* 结果保留来源 URL、来源类型、发布时间和检索时间；
* 离线模式完全不访问网络，并返回 ``simulated`` 质量；
* 解析器只输出有限字段，不把整篇远程响应写入记忆。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


DEFAULT_WIKIPEDIA_LANGS = ("zh", "en")
DEFAULT_USER_AGENT = "brain-memory/11 (+local autonomous source adapter)"
ALLOWED_PROVIDERS = frozenset({"wikipedia", "arxiv", "feeds"})
ATOM_NS = "http://www.w3.org/2005/Atom"


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(low, min(high, parsed))


def _bounded_text(value: Any, limit: int = 500) -> str:
    return ("" if value is None else str(value)).strip()[:limit]


def _clean_content(value: Any, limit: int = 500) -> str:
    """Reduce remote markup/control noise before it reaches cognition."""
    text = html.unescape("" if value is None else str(value))
    # Script/style payloads are not useful observations and are a common place
    # for prompt-like noise in syndicated content.
    text = re.sub(r"(?is)<script\b[^>]*>.*?</script\s*>", " ", text)
    text = re.sub(r"(?is)<style\b[^>]*>.*?</style\s*>", " ", text)
    text = re.sub(r"<[^>]{0,300}>", " ", text)
    text = "".join(
        char if char in "\n\t" or ord(char) >= 32 else " "
        for char in text
    )
    return " ".join(text.split())[:limit]


def _offline_flag() -> bool:
    return os.getenv("BRAIN_MEMORY_OFFLINE", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SourceItem:
    """一条带溯源信息的有限结果。"""

    title: str
    url: str
    summary: str = ""
    published: str = ""
    source: str = ""
    source_type: str = "official_json"
    verified: bool = True
    retrieved_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": _clean_content(self.title, 240),
            "url": _bounded_text(self.url, 1000),
            "summary": _clean_content(self.summary, 500),
            "published": _bounded_text(self.published, 80),
            "source": _bounded_text(self.source, 120),
            "source_type": _bounded_text(self.source_type, 40),
            "verified": bool(self.verified),
            "retrieved_at": _bounded_text(self.retrieved_at, 80),
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not silently follow a redirect to an unreviewed host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "redirect refused by source boundary",
            headers,
            fp,
        )


class SourceAdapter:
    """从白名单来源检索并标准化信息。"""

    def __init__(
        self,
        *,
        providers: Iterable[str] | None = None,
        feed_urls: Iterable[str] | None = None,
        wikipedia_langs: Iterable[str] | None = None,
        timeout: float | int | None = None,
        max_bytes: int | None = None,
        cache_ttl: float | int | None = None,
        cache_size: int = 64,
        offline: bool | None = None,
    ):
        if providers is None:
            raw_providers = os.getenv("BRAIN_MEMORY_SOURCE_PROVIDERS", "wikipedia")
            providers = [item.strip().lower() for item in raw_providers.split(",")]
        elif isinstance(providers, str):
            providers = [item.strip().lower() for item in providers.split(",")]
        normalized_providers = [
            str(item).strip().lower()
            for item in providers
            if str(item).strip().lower() in ALLOWED_PROVIDERS
        ]
        self.providers = tuple(dict.fromkeys(normalized_providers)) or ("wikipedia",)

        if feed_urls is None:
            raw_feeds = os.getenv("BRAIN_MEMORY_SOURCE_FEEDS", "")
            feed_urls = [item.strip() for item in raw_feeds.split(",") if item.strip()]
        elif isinstance(feed_urls, str):
            feed_urls = [item.strip() for item in feed_urls.split(",") if item.strip()]
        self.feed_urls = tuple(
            _bounded_text(url, 2000)
            for url in feed_urls
            if str(url).strip()
        )[:16]

        if wikipedia_langs is None:
            raw_langs = os.getenv(
                "BRAIN_MEMORY_SOURCE_WIKIPEDIA_LANGS",
                ",".join(DEFAULT_WIKIPEDIA_LANGS),
            )
            wikipedia_langs = [item.strip().lower() for item in raw_langs.split(",")]
        elif isinstance(wikipedia_langs, str):
            wikipedia_langs = [item.strip().lower() for item in wikipedia_langs.split(",")]
        self.wikipedia_langs = tuple(
            dict.fromkeys(
                item for item in (
                    re.sub(r"[^a-z-]", "", str(lang).lower())
                    for lang in wikipedia_langs
                )
                if item and len(item) <= 16
            )
        )[:8] or DEFAULT_WIKIPEDIA_LANGS

        try:
            parsed_timeout = float(timeout if timeout is not None else os.getenv(
                "BRAIN_MEMORY_SOURCE_TIMEOUT_SEC", "8"
            ))
        except (TypeError, ValueError, OverflowError):
            parsed_timeout = 8.0
        if not math.isfinite(parsed_timeout):
            parsed_timeout = 8.0
        self.timeout = max(1.0, min(30.0, parsed_timeout))
        self.max_bytes = _bounded_int(
            max_bytes if max_bytes is not None else os.getenv(
                "BRAIN_MEMORY_SOURCE_MAX_BYTES", "1000000"
            ),
            1_000_000,
            16_384,
            5_000_000,
        )
        try:
            parsed_ttl = float(cache_ttl if cache_ttl is not None else os.getenv(
                "BRAIN_MEMORY_SOURCE_CACHE_TTL_SEC", "60"
            ))
        except (TypeError, ValueError, OverflowError):
            parsed_ttl = 60.0
        if not math.isfinite(parsed_ttl):
            parsed_ttl = 60.0
        self.cache_ttl = max(0.0, min(3600.0, parsed_ttl))
        self.cache_size = _bounded_int(cache_size, 64, 1, 256)
        self.offline = _offline_flag() if offline is None else bool(offline)
        self._cache: OrderedDict[tuple[str, int], tuple[float, dict[str, Any]]] = OrderedDict()
        self._cache_lock = threading.RLock()
        # A read-only tool may be dispatched concurrently by several episodes;
        # keep network work bounded so a burst cannot exhaust the process.
        self._network_gate = threading.BoundedSemaphore(4)

    async def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """异步检索；阻塞式 stdlib 网络访问放到线程池。"""
        normalized_query = _bounded_text(query, 300)
        safe_limit = _bounded_int(limit, 5, 1, 10)
        if not normalized_query:
            return self._failure_result("query required", query="")
        if self.offline:
            return {
                "query": normalized_query,
                "count": 0,
                "results": [],
                "sources": [],
                "errors": [],
                "verified": False,
                "result_quality": "simulated",
                "fetched_at": _now_iso(),
                "note": "离线模式，未访问外部来源；结果不能作为外部事实",
            }

        cached = self._cache_get(normalized_query, safe_limit)
        if cached is not None:
            return cached
        result = await asyncio.to_thread(self._search_guarded, normalized_query, safe_limit)
        if result.get("result_quality") != "failed":
            self._cache_put(normalized_query, safe_limit, result)
        return result

    def _search_guarded(self, query: str, limit: int) -> dict[str, Any]:
        with self._network_gate:
            return self._search_sync(query, limit)

    def _search_sync(self, query: str, limit: int) -> dict[str, Any]:
        items: list[SourceItem] = []
        errors: list[str] = []
        successful_sources: list[str] = []

        for provider in self.providers:
            try:
                if provider == "wikipedia":
                    found = self._search_wikipedia(query, limit)
                elif provider == "arxiv":
                    found = self._search_arxiv(query, limit)
                elif provider == "feeds":
                    found = self._search_feeds(query, limit)
                else:  # defensive: providers are normalized in __init__
                    continue
                successful_sources.append(provider)
                items.extend(found)
            except Exception as exc:  # one source cannot kill the action loop
                errors.append(f"{provider}: {_clean_content(exc, 180)}")

        unique: list[SourceItem] = []
        seen: set[str] = set()
        for item in items:
            key = item.url or f"{item.source}:{item.title}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= limit:
                break

        quality = "verified" if successful_sources else "failed"
        note = (
            "已从白名单来源获取并保留来源 URL、类型与时间；远程内容仅作观察，仍需按来源自行判断"
            if unique
            else "已访问白名单来源，但没有匹配条目"
            if successful_sources
            else "白名单来源不可用，未生成占位事实"
        )
        result: dict[str, Any] = {
            "query": query,
            "count": len(unique),
            "results": [item.to_dict() for item in unique],
            "sources": successful_sources,
            "errors": errors[:4],
            "verified": bool(successful_sources),
            "result_quality": quality,
            "fetched_at": _now_iso(),
            "note": note,
        }
        if quality == "failed":
            result["error"] = "; ".join(errors[:2]) or "no source configured"
        return result

    def _search_wikipedia(self, query: str, limit: int) -> list[SourceItem]:
        last_error: Exception | None = None
        for language in self.wikipedia_langs:
            host = f"{language}.wikipedia.org"
            params = urllib.parse.urlencode({
                "action": "opensearch",
                "search": query,
                "limit": limit,
                "namespace": 0,
                "format": "json",
                "redirects": "resolve",
            })
            url = f"https://{host}/w/api.php?{params}"
            try:
                payload = json.loads(self._fetch_url(url).decode("utf-8", "replace"))
                if not isinstance(payload, list) or len(payload) < 4:
                    raise ValueError("unexpected Wikipedia response")
                titles = payload[1] if isinstance(payload[1], list) else []
                descriptions = payload[2] if isinstance(payload[2], list) else []
                urls = payload[3] if isinstance(payload[3], list) else []
                found: list[SourceItem] = []
                for index, title in enumerate(titles[:limit]):
                    result_url = urls[index] if index < len(urls) else ""
                    if not self._is_allowed_url(result_url, {host}):
                        continue
                    if not _clean_content(title, 240):
                        continue
                    found.append(SourceItem(
                        title=_bounded_text(title, 240),
                        url=result_url,
                        summary=_bounded_text(
                            descriptions[index] if index < len(descriptions) else "", 500
                        ),
                        source=f"Wikipedia ({language})",
                        source_type="official_json",
                    ))
                if found or language == self.wikipedia_langs[-1]:
                    return found
            except Exception as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        return []

    def _search_arxiv(self, query: str, limit: int) -> list[SourceItem]:
        params = urllib.parse.urlencode({
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": limit,
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        url = f"https://export.arxiv.org/api/query?{params}"
        root = self._parse_xml(self._fetch_url(url))
        found: list[SourceItem] = []
        for entry in root.findall(f"{{{ATOM_NS}}}entry")[:limit]:
            title = self._xml_text(entry.find(f"{{{ATOM_NS}}}title"))
            summary = self._xml_text(entry.find(f"{{{ATOM_NS}}}summary"))
            published = self._xml_text(entry.find(f"{{{ATOM_NS}}}published"))
            result_url = ""
            for link in entry.findall(f"{{{ATOM_NS}}}link"):
                href = _bounded_text(link.attrib.get("href", ""), 1000)
                rel = link.attrib.get("rel", "")
                if href and (rel in {"alternate", ""} or not result_url):
                    result_url = href
                    if rel == "alternate":
                        break
            if result_url.startswith("http://"):
                result_url = "https://" + result_url[len("http://"):]
            if not self._is_allowed_url(result_url, {"export.arxiv.org", "arxiv.org"}):
                continue
            if not title or not result_url:
                continue
            found.append(SourceItem(
                title=title,
                url=result_url,
                summary=summary,
                published=published,
                source="arXiv",
                source_type="official_atom",
            ))
        return found

    def _search_feeds(self, query: str, limit: int) -> list[SourceItem]:
        if not self.feed_urls:
            raise ValueError("no RSS/Atom feed configured")
        found: list[SourceItem] = []
        query_words = [word.lower() for word in re.findall(r"[\w\u4e00-\u9fff-]+", query) if word]
        for template in self.feed_urls:
            template_parts = urllib.parse.urlparse(template)
            if "{query}" in (template_parts.netloc or ""):
                raise ValueError("feed query template cannot change its host")
            url = template.replace(
                "{query}", urllib.parse.quote(query, safe="")
            )
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            if not self._is_allowed_url(url, {host} if host else set()):
                raise ValueError("feed URL must be an HTTPS public host without redirects")
            root = self._parse_xml(self._fetch_url(url))
            source_name = host[:120]
            entries = [(entry, "rss") for entry in self._descendants(root, {"item"})]
            entries.extend(
                (entry, "atom")
                for entry in self._descendants(root, {"entry"})
            )
            templated = "{query}" in template
            for entry, entry_type in entries:
                title = self._xml_text(self._find_child(entry, {"title"}))
                summary = self._xml_text(
                    self._find_child(entry, {"description", "summary", "content"})
                )
                haystack = f"{title} {summary}".lower()
                if not templated and query_words and not any(
                    word in haystack for word in query_words
                ):
                    continue
                result_url = self._feed_link(entry, url)
                if not self._is_allowed_url(result_url, {host}):
                    continue
                if not title or not result_url:
                    continue
                published = self._xml_text(
                    self._find_child(entry, {"pubDate", "published", "updated"})
                )
                found.append(SourceItem(
                    title=title,
                    url=result_url,
                    summary=summary,
                    published=published,
                    source=source_name,
                    source_type=entry_type,
                ))
                if len(found) >= limit:
                    return found
        return found

    def _fetch_url(self, url: str) -> bytes:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if not self._is_allowed_url(url, {host} if host else set()):
            raise ValueError("source URL rejected by HTTPS/public-host boundary")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml;q=0.9",
                "Accept-Encoding": "identity",
                "User-Agent": DEFAULT_USER_AGENT,
            },
        )
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=self.timeout) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError, OverflowError):
                    declared_length = 0
                if declared_length > self.max_bytes:
                    raise ValueError("source response exceeds size limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(min(64 * 1024, self.max_bytes - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > self.max_bytes:
                    raise ValueError("source response exceeds size limit")
            return b"".join(chunks)

    @staticmethod
    def _parse_xml(payload: bytes) -> ET.Element:
        lowered = payload.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise ValueError("unsafe XML declaration")
        return ET.fromstring(payload)

    @staticmethod
    def _xml_text(node: ET.Element | None) -> str:
        if node is None:
            return ""
        parts: list[str] = []

        def collect(current: ET.Element) -> None:
            local_name = (
                current.tag.rsplit("}", 1)[-1].lower()
                if isinstance(current.tag, str)
                else ""
            )
            if local_name in {"script", "style"}:
                return
            if current.text:
                parts.append(current.text)
            for child in list(current):
                collect(child)
                if child.tail:
                    parts.append(child.tail)

        collect(node)
        return _clean_content(" ".join(parts), 500)

    @staticmethod
    def _descendants(root: ET.Element, names: set[str]) -> list[ET.Element]:
        return [
            node for node in root.iter()
            if isinstance(node.tag, str)
            and node.tag.rsplit("}", 1)[-1] in names
        ]

    @staticmethod
    def _find_child(entry: ET.Element, names: set[str]) -> ET.Element | None:
        """Find a direct child by local XML name (RSS and Atom namespaces)."""
        normalized_names = {str(name).lower() for name in names}
        for child in list(entry):
            local_name = (
                child.tag.rsplit("}", 1)[-1].lower()
                if isinstance(child.tag, str)
                else ""
            )
            if local_name in normalized_names:
                return child
        return None

    @classmethod
    def _feed_link(cls, entry: ET.Element, base_url: str = "") -> str:
        link = cls._find_child(entry, {"link"})
        if link is not None:
            raw = _bounded_text(link.attrib.get("href", "") or link.text, 1000)
            if raw:
                return urllib.parse.urljoin(base_url, raw)
        guid = cls._find_child(entry, {"guid", "id"})
        raw = _bounded_text(guid.text if guid is not None else "", 1000)
        return urllib.parse.urljoin(base_url, raw) if raw else ""

    @staticmethod
    def _is_allowed_url(url: str, allowed_hosts: set[str]) -> bool:
        try:
            parsed = urllib.parse.urlparse(str(url))
            host = (parsed.hostname or "").lower().rstrip(".")
            port = parsed.port
        except (TypeError, ValueError):
            return False
        if parsed.scheme.lower() != "https" or not host:
            return False
        if parsed.username or parsed.password or port not in (None, 443):
            return False
        if host not in {item.lower().rstrip(".") for item in allowed_hosts if item}:
            return False
        if host in {"localhost", "localhost.localdomain"}:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return True
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
        )

    def _cache_get(self, query: str, limit: int) -> dict[str, Any] | None:
        if self.cache_ttl <= 0:
            return None
        key = (query, limit)
        with self._cache_lock:
            value = self._cache.get(key)
            if value is None:
                return None
            created, result = value
            if time.monotonic() - created > self.cache_ttl:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return json.loads(json.dumps(result, ensure_ascii=False))

    def _cache_put(self, query: str, limit: int, result: dict[str, Any]) -> None:
        if self.cache_ttl <= 0:
            return
        key = (query, limit)
        # Keep caller mutations from poisoning subsequent observations.
        cached_result = json.loads(json.dumps(result, ensure_ascii=False))
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), cached_result)
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    @staticmethod
    def _failure_result(message: str, query: str) -> dict[str, Any]:
        return {
            "query": _bounded_text(query, 300),
            "count": 0,
            "results": [],
            "sources": [],
            "errors": [_bounded_text(message, 180)],
            "verified": False,
            "result_quality": "failed",
            "fetched_at": _now_iso(),
            "error": _clean_content(message, 180),
            "note": "请求未执行或未获得可核验结果",
        }


_adapter: SourceAdapter | None = None
_adapter_lock = threading.RLock()


def get_source_adapter() -> SourceAdapter:
    """返回进程内共享适配器；网络访问仍在 ``search`` 调用时发生。"""
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            _adapter = SourceAdapter()
        return _adapter
