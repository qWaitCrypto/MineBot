"""Advisory Minecraft Wiki access with bounded retries and durable cache."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from minebot.app.runtime_state import RuntimeStateStore, WikiCacheRecord
from minebot.brain.registry import RegisteredTool, ToolRegistry, ToolSidecar
from minebot.contract import ToolResult


DEFAULT_WIKI_ENDPOINT = "https://minecraft.wiki/api.php"
DEFAULT_WIKI_USER_AGENT = "MineBot/0.1 (+https://github.com/qWaitCrypto/MineBot)"
_RETRYABLE_STATUS = {429, 502, 503, 504}
_BOILERPLATE_SECTIONS = {
    "history",
    "trivia",
    "gallery",
    "references",
    "see also",
    "external links",
    "development",
}


class WikiUnavailable(RuntimeError):
    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True)
class WikiConfig:
    endpoint: str = DEFAULT_WIKI_ENDPOINT
    user_agent: str = DEFAULT_WIKI_USER_AGENT
    timeout_s: float = 8.0
    max_attempts: int = 3
    maxlag_s: int = 5
    min_request_interval_s: float = 0.2
    backoff_base_s: float = 0.5
    backoff_max_s: float = 5.0
    search_ttl_s: float = 6 * 60 * 60
    page_ttl_s: float = 7 * 24 * 60 * 60
    negative_ttl_s: float = 15 * 60
    max_extract_chars: int = 12000

    def __post_init__(self) -> None:
        parts = urlsplit(self.endpoint)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password:
            raise ValueError("Wiki endpoint must be credential-free HTTPS")
        if not self.user_agent.strip() or len(self.user_agent) > 500:
            raise ValueError("Wiki user_agent must be descriptive and bounded")
        if self.timeout_s <= 0 or not 1 <= self.max_attempts <= 5:
            raise ValueError("Wiki timeout/max_attempts are outside bounded limits")
        if self.maxlag_s < 1 or self.min_request_interval_s < 0:
            raise ValueError("Wiki maxlag/rate interval is invalid")
        if self.search_ttl_s < 0 or self.page_ttl_s < 0 or self.negative_ttl_s < 0:
            raise ValueError("Wiki cache TTL must be non-negative")
        if not 1000 <= self.max_extract_chars <= 32000:
            raise ValueError("Wiki extract budget must be between 1000 and 32000 chars")


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes


class WikiTransport:
    def request(self, url: str, headers: Mapping[str, str], timeout_s: float) -> HttpResult:
        raise NotImplementedError


class UrlLibWikiTransport(WikiTransport):
    def request(self, url: str, headers: Mapping[str, str], timeout_s: float) -> HttpResult:
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with urlopen(request, timeout=timeout_s) as response:
                return _http_result(response)
        except HTTPError as exc:
            body = exc.read()
            return HttpResult(
                int(exc.code),
                {str(key).casefold(): str(value) for key, value in exc.headers.items()},
                _decode_content(body, str(exc.headers.get("Content-Encoding") or "")),
            )
        except (OSError, URLError) as exc:
            raise WikiUnavailable(f"wiki_transport_error:{type(exc).__name__}", retryable=True) from exc


class WikiKnowledge:
    def __init__(
        self,
        store: RuntimeStateStore,
        *,
        config: WikiConfig = WikiConfig(),
        transport: WikiTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_value: Callable[[], float] = random.random,
    ) -> None:
        self.store = store
        self.config = config
        self.transport = transport or UrlLibWikiTransport()
        self._sleep = sleep
        self._now = now
        self._random = random_value
        self._rate_lock = threading.Lock()
        self._last_request_monotonic: float | None = None

    def search(self, query: str, *, limit: int = 5) -> dict[str, object]:
        clean_query = " ".join(str(query or "").split())[:500]
        if not clean_query:
            raise ValueError("wiki_search query must not be empty")
        limit = max(1, min(10, int(limit)))
        request_key = json.dumps(
            {"query": clean_query, "limit": limit},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = _wiki_cache_key(self.config.endpoint, "search", request_key)
        cached = self.store.get_wiki_cache(cache_key)
        if _cache_fresh(cached, self._now()):
            return _cache_payload(cached, status="fresh")
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "search",
            "srnamespace": "0",
            "srlimit": str(limit),
            "srsearch": clean_query,
            "maxlag": str(self.config.maxlag_s),
        }
        try:
            response, cache_status = self._request_json(params, cached=cached)
            if response is None:
                assert cached is not None
                refreshed = self._refresh_cache(cached, self.config.search_ttl_s)
                return _cache_payload(refreshed, status=cache_status)
            search_rows = response.get("query", {}).get("search", [])
            results: list[dict[str, object]] = []
            if isinstance(search_rows, list):
                for row in search_rows[:limit]:
                    if not isinstance(row, dict):
                        continue
                    title = " ".join(str(row.get("title") or "").split())
                    if not title:
                        continue
                    results.append(
                        {
                            "title": title,
                            "snippet": _clean_snippet(str(row.get("snippet") or "")),
                            "page_id": row.get("pageid"),
                            "word_count": row.get("wordcount"),
                        }
                    )
            payload = {
                "query": clean_query,
                "results": results,
                "count": len(results),
                "complete": True,
                "stale": False,
                "cache_status": "network",
                "source": "minecraft.wiki",
            }
            stored = self._store_cache(
                cache_key,
                kind="search",
                request_key=request_key,
                payload=payload,
                ttl_s=self.config.search_ttl_s,
                headers=cache_status,
            )
            return _cache_payload(stored, status="network")
        except WikiUnavailable as exc:
            if cached is not None:
                return _cache_payload(cached, status="stale", error=exc.reason)
            raise

    def read(
        self,
        title: str,
        *,
        query: str = "",
        max_chars: int | None = None,
    ) -> dict[str, object] | None:
        clean_title = " ".join(str(title or "").split())[:500]
        clean_query = " ".join(str(query or "").split())[:500]
        if not clean_title:
            raise ValueError("wiki_read title must not be empty")
        budget = self.config.max_extract_chars if max_chars is None else int(max_chars)
        budget = max(1000, min(self.config.max_extract_chars, budget))
        request_key = json.dumps(
            {"title": clean_title, "query": clean_query, "max_chars": budget},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        cache_key = _wiki_cache_key(self.config.endpoint, "page", request_key)
        cached = self.store.get_wiki_cache(cache_key)
        if _cache_fresh(cached, self._now()):
            if _cache_is_not_found(cached):
                return None
            return _cache_payload(cached, status="fresh")
        params = {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "extracts|revisions",
            "explaintext": "1",
            "exsectionformat": "wiki",
            "redirects": "1",
            "rvlimit": "1",
            "rvprop": "ids|timestamp",
            "titles": clean_title,
            "maxlag": str(self.config.maxlag_s),
        }
        try:
            response, cache_status = self._request_json(params, cached=cached)
            if response is None:
                assert cached is not None
                ttl_s = (
                    self.config.negative_ttl_s
                    if _cache_is_not_found(cached)
                    else self.config.page_ttl_s
                )
                refreshed = self._refresh_cache(cached, ttl_s)
                if _cache_is_not_found(refreshed):
                    return None
                return _cache_payload(refreshed, status=cache_status)
            pages = response.get("query", {}).get("pages", [])
            page = pages[0] if isinstance(pages, list) and pages else None
            if not isinstance(page, dict) or page.get("missing") is True:
                self._store_cache(
                    cache_key,
                    kind="page",
                    request_key=request_key,
                    payload={
                        "title": clean_title,
                        "source": "minecraft.wiki",
                        "not_found": True,
                    },
                    ttl_s=self.config.negative_ttl_s,
                    headers=cache_status,
                )
                return None
            resolved_title = " ".join(str(page.get("title") or clean_title).split())
            extract = str(page.get("extract") or "")
            revisions = page.get("revisions")
            revision = revisions[0] if isinstance(revisions, list) and revisions else {}
            markdown, omitted = _clean_extract(
                extract,
                title=resolved_title,
                query=clean_query,
                max_chars=budget,
            )
            retrieved_at = _iso(self._now())
            revision_id = revision.get("revid") if isinstance(revision, dict) else None
            provenance = (
                f"> source: minecraft.wiki | revision: {revision_id or 'unknown'} "
                f"| retrieved: {retrieved_at}"
            )
            markdown = _append_provenance(markdown, provenance, max_chars=budget)
            payload = {
                "title": resolved_title,
                "markdown": markdown,
                "source": "minecraft.wiki",
                "source_url": _wiki_page_url(resolved_title),
                "revision_id": revision_id,
                "revision_timestamp": (
                    revision.get("timestamp") if isinstance(revision, dict) else None
                ),
                "retrieved_at": retrieved_at,
                "omitted_sections": omitted,
                "complete": omitted == 0,
                "stale": False,
                "cache_status": "network",
                "advisory": True,
            }
            stored = self._store_cache(
                cache_key,
                kind="page",
                request_key=request_key,
                payload=payload,
                ttl_s=self.config.page_ttl_s,
                headers=cache_status,
            )
            return _cache_payload(stored, status="network")
        except WikiUnavailable as exc:
            if cached is not None:
                if _cache_is_not_found(cached):
                    return None
                return _cache_payload(cached, status="stale", error=exc.reason)
            raise

    def _request_json(
        self,
        params: dict[str, str],
        *,
        cached: WikiCacheRecord | None,
    ) -> tuple[dict[str, object] | None, dict[str, str] | str]:
        url = f"{self.config.endpoint}?{urlencode(sorted(params.items()))}"
        headers = {
            "User-Agent": self.config.user_agent,
            "Api-User-Agent": self.config.user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "Accept-Language": "en",
        }
        if cached is not None and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached is not None and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        last_reason = "wiki_unavailable"
        for attempt in range(self.config.max_attempts):
            self._respect_rate_limit()
            try:
                result = self.transport.request(url, headers, self.config.timeout_s)
            except WikiUnavailable as exc:
                last_reason = exc.reason
                if not exc.retryable or attempt + 1 >= self.config.max_attempts:
                    raise
                self._sleep(self._retry_delay(attempt, None))
                continue
            if result.status == 304:
                return None, "revalidated"
            if result.status in _RETRYABLE_STATUS:
                last_reason = f"wiki_http_{result.status}"
                if attempt + 1 >= self.config.max_attempts:
                    raise WikiUnavailable(last_reason, retryable=True)
                self._sleep(
                    self._retry_delay(attempt, _retry_after(result.headers.get("retry-after")))
                )
                continue
            if result.status < 200 or result.status >= 300:
                raise WikiUnavailable(f"wiki_http_{result.status}", retryable=False)
            try:
                payload = json.loads(result.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WikiUnavailable("wiki_invalid_json", retryable=False) from exc
            if not isinstance(payload, dict):
                raise WikiUnavailable("wiki_invalid_payload", retryable=False)
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or "unknown")
                if code == "maxlag":
                    last_reason = "wiki_maxlag"
                    if attempt + 1 >= self.config.max_attempts:
                        raise WikiUnavailable(last_reason, retryable=True)
                    self._sleep(self._retry_delay(attempt, None))
                    continue
                raise WikiUnavailable(f"wiki_api_{code}", retryable=False)
            return payload, result.headers
        raise WikiUnavailable(last_reason, retryable=True)

    def _respect_rate_limit(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            if self._last_request_monotonic is not None:
                wait = self.config.min_request_interval_s - (
                    now - self._last_request_monotonic
                )
                if wait > 0:
                    self._sleep(wait)
            self._last_request_monotonic = time.monotonic()

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(self.config.backoff_max_s, max(0.0, retry_after))
        base = min(
            self.config.backoff_max_s,
            self.config.backoff_base_s * (2**attempt),
        )
        return base * (0.8 + 0.4 * self._random())

    def _store_cache(
        self,
        cache_key: str,
        *,
        kind: str,
        request_key: str,
        payload: dict[str, object],
        ttl_s: float,
        headers: Mapping[str, str] | str,
    ) -> WikiCacheRecord:
        now = self._now()
        response_headers = headers if isinstance(headers, Mapping) else {}
        return self.store.put_wiki_cache(
            cache_key=cache_key,
            endpoint=self.config.endpoint,
            kind=kind,
            request_key=request_key,
            payload=payload,
            fetched_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_s)),
            etag=response_headers.get("etag"),
            last_modified=response_headers.get("last-modified"),
        )

    def _refresh_cache(self, cached: WikiCacheRecord, ttl_s: float) -> WikiCacheRecord:
        now = self._now()
        refreshed = self.store.refresh_wiki_cache_expiry(
            cached.cache_key,
            fetched_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=ttl_s)),
        )
        assert refreshed is not None
        return refreshed


def register_wiki_tools(registry: ToolRegistry, knowledge: WikiKnowledge) -> None:
    registry.register(_wiki_search_tool(knowledge))
    registry.register(_wiki_read_tool(knowledge))


def _wiki_search_tool(knowledge: WikiKnowledge) -> RegisteredTool:
    def search(params: dict[str, object]) -> ToolResult:
        try:
            result = knowledge.search(
                str(params.get("query") or ""),
                limit=int(params.get("limit") or 5),
            )
        except (ValueError, WikiUnavailable) as exc:
            retryable = isinstance(exc, WikiUnavailable) and exc.retryable
            return ToolResult(
                False,
                "wiki_unavailable" if isinstance(exc, WikiUnavailable) else "wiki_query_rejected",
                retryable,
                metrics={"error": str(exc)},
            )
        reason = "wiki_search_empty" if not result["results"] else "wiki_search"
        return ToolResult(True, reason, False, metrics=result)

    return RegisteredTool(
        "wiki_search",
        "Search minecraft.wiki for advisory prose about obscure mechanics or version-specific behavior. For exact recipes, counts, inventory, stats, blocks, or entities, use live server tools instead.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        search,
        _wiki_sidecar("wiki_search"),
    )


def _wiki_read_tool(knowledge: WikiKnowledge) -> RegisteredTool:
    def read(params: dict[str, object]) -> ToolResult:
        try:
            result = knowledge.read(
                str(params.get("title") or ""),
                query=str(params.get("query") or ""),
                max_chars=(
                    None if params.get("max_chars") is None else int(params["max_chars"])
                ),
            )
        except (ValueError, WikiUnavailable) as exc:
            retryable = isinstance(exc, WikiUnavailable) and exc.retryable
            return ToolResult(
                False,
                "wiki_unavailable" if isinstance(exc, WikiUnavailable) else "wiki_read_rejected",
                retryable,
                metrics={"error": str(exc)},
            )
        if result is None:
            return ToolResult(False, "wiki_page_not_found", False, metrics={})
        return ToolResult(
            True,
            "wiki_read_stale_cache" if result.get("stale") else "wiki_read",
            bool(result.get("stale")),
            metrics=result,
        )

    return RegisteredTool(
        "wiki_read",
        "Read one minecraft.wiki page selected from wiki_search as bounded advisory Markdown. Recheck exact game facts with live server tools.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 500},
                "query": {"type": "string", "maxLength": 500},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 32000},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
        read,
        _wiki_sidecar("wiki_read"),
    )


def _wiki_sidecar(progress_key: str) -> ToolSidecar:
    return ToolSidecar(
        progress_key,
        mutating=False,
        source="agent.knowledge",
        tool_type="external_knowledge",
        permission="read_external_knowledge",
        body_scope=(),
        terminal_truth=("minecraft.wiki provenance", "cache completeness"),
        timeout_s=30.0,
    )


def _http_result(response: HTTPResponse) -> HttpResult:
    headers = {str(key).casefold(): str(value) for key, value in response.headers.items()}
    body = _decode_content(response.read(), headers.get("content-encoding", ""))
    return HttpResult(int(response.status), headers, body)


def _decode_content(body: bytes, encoding: str) -> bytes:
    if "gzip" in encoding.casefold():
        return gzip.decompress(body)
    return body


def _clean_snippet(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(without_tags).split())[:1000]


def _clean_extract(
    extract: str,
    *,
    title: str,
    query: str,
    max_chars: int,
) -> tuple[str, int]:
    heading_pattern = re.compile(r"(?m)^={2,6}\s*(.*?)\s*={2,6}\s*$")
    matches = list(heading_pattern.finditer(extract))
    intro = extract[: matches[0].start()] if matches else extract
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        heading = " ".join(match.group(1).split())
        end = matches[index + 1].start() if index + 1 < len(matches) else len(extract)
        body = extract[match.end() : end]
        if heading.casefold() in _BOILERPLATE_SECTIONS:
            continue
        clean_body = _clean_plain_text(body)
        if clean_body:
            sections.append((heading, clean_body))
    clean_intro = _clean_plain_text(intro)
    rendered_sections = [f"## {heading}\n\n{body}" for heading, body in sections]
    full = "\n\n".join(
        item for item in [f"# {title}", clean_intro, *rendered_sections] if item
    )
    if len(full) <= max_chars:
        return full, 0
    query_terms = set(re.findall(r"[\w]+", query.casefold(), flags=re.UNICODE))
    ranked = sorted(
        sections,
        key=lambda item: (
            -len(query_terms & set(re.findall(r"[\w]+", item[0].casefold()))),
            item[0].casefold(),
        ),
    )
    selected: list[str] = [f"# {title}"]
    if clean_intro:
        selected.append(clean_intro)
    omitted = len(sections)
    for heading, body in ranked:
        candidate = "\n\n".join([*selected, f"## {heading}\n\n{body}"])
        if len(candidate) > max_chars:
            continue
        selected.append(f"## {heading}\n\n{body}")
        omitted -= 1
    bounded = "\n\n".join(selected)
    if len(bounded) > max_chars:
        bounded = bounded[: max_chars - 1].rstrip() + "\u2026"
    return bounded, max(0, omitted)


def _clean_plain_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.splitlines()]
    compact: list[str] = []
    previous_empty = True
    for line in lines:
        if not line:
            if not previous_empty:
                compact.append("")
            previous_empty = True
            continue
        compact.append(line)
        previous_empty = False
    return "\n".join(compact).strip()


def _append_provenance(markdown: str, provenance: str, *, max_chars: int) -> str:
    separator = "\n\n"
    available = max(0, max_chars - len(separator) - len(provenance))
    body = markdown
    if len(body) > available:
        body = body[: max(0, available - 1)].rstrip() + "\u2026"
    return f"{body}{separator}{provenance}".strip()


def _wiki_cache_key(endpoint: str, kind: str, request_key: str) -> str:
    return hashlib.sha256(f"{endpoint}\n{kind}\n{request_key}".encode("utf-8")).hexdigest()


def _cache_fresh(cached: WikiCacheRecord | None, now: datetime) -> bool:
    return cached is not None and _parse_iso(cached.expires_at) > now


def _cache_is_not_found(cached: WikiCacheRecord | None) -> bool:
    return cached is not None and cached.payload.get("not_found") is True


def _cache_payload(
    cached: WikiCacheRecord,
    *,
    status: str,
    error: str | None = None,
) -> dict[str, object]:
    payload = dict(cached.payload)
    payload["cache_status"] = status
    payload["stale"] = status == "stale"
    payload["cache_fetched_at"] = cached.fetched_at
    if error is not None:
        payload["refresh_error"] = error
    return payload


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _wiki_page_url(title: str) -> str:
    return "https://minecraft.wiki/w/" + quote(title.replace(" ", "_"), safe="")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_WIKI_ENDPOINT",
    "DEFAULT_WIKI_USER_AGENT",
    "HttpResult",
    "UrlLibWikiTransport",
    "WikiConfig",
    "WikiKnowledge",
    "WikiTransport",
    "WikiUnavailable",
    "register_wiki_tools",
]
