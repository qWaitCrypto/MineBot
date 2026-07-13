import json
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from minebot.app.runtime_state import RuntimeStateStore
from minebot.app.wiki import (
    HttpResult,
    WikiConfig,
    WikiKnowledge,
    WikiTransport,
    WikiUnavailable,
    register_wiki_tools,
)
from minebot.brain.registry import ToolRegistry


class FakeTransport(WikiTransport):
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, url, headers, timeout_s):
        self.calls.append((url, dict(headers), timeout_s))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def response(payload, *, status=200, headers=None):
    return HttpResult(
        status,
        {str(key).casefold(): str(value) for key, value in (headers or {}).items()},
        json.dumps(payload).encode("utf-8"),
    )


def search_payload():
    return {
        "query": {
            "search": [
                {
                    "pageid": 12,
                    "title": "Nether portal",
                    "snippet": "A <span class=\"searchmatch\">Nether</span> portal.",
                    "wordcount": 120,
                }
            ]
        }
    }


def page_payload(*, missing=False):
    page = {"missing": True, "title": "Missing"} if missing else {
        "pageid": 44,
        "title": "Trading",
        "extract": (
            "Trading is an interaction mechanic.\n\n"
            "== Mechanics ==\nVillagers expose offers by profession.\n\n"
            "== History ==\nOld version details that should be dropped.\n\n"
            "== Usage ==\nOffers can become unavailable after repeated use."
        ),
        "revisions": [{"revid": 9001, "timestamp": "2026-07-01T00:00:00Z"}],
    }
    return {"query": {"pages": [page]}}


class WikiClientTests(unittest.TestCase):
    def setUp(self):
        self.store = RuntimeStateStore(":memory:")
        self.clock = Clock()

    def tearDown(self):
        self.store.close()

    def config(self, **overrides):
        values = {
            "min_request_interval_s": 0,
            "backoff_base_s": 0.01,
            "backoff_max_s": 10,
        }
        values.update(overrides)
        return WikiConfig(**values)

    def test_search_uses_mediawiki_headers_maxlag_get_and_fresh_cache(self):
        transport = FakeTransport(response(search_payload()))
        knowledge = WikiKnowledge(
            self.store,
            config=self.config(),
            transport=transport,
            now=self.clock,
        )

        first = knowledge.search("nether portal")
        second = knowledge.search("nether portal")

        self.assertEqual(first["results"][0]["snippet"], "A Nether portal.")
        self.assertEqual(first["cache_status"], "network")
        self.assertEqual(second["cache_status"], "fresh")
        self.assertEqual(len(transport.calls), 1)
        url, headers, timeout = transport.calls[0]
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["maxlag"], ["5"])
        self.assertEqual(query["format"], ["json"])
        self.assertIn("MineBot/0.1", headers["User-Agent"])
        self.assertEqual(headers["Api-User-Agent"], headers["User-Agent"])
        self.assertEqual(headers["Accept-Encoding"], "gzip")
        self.assertEqual(timeout, 8.0)

    def test_read_cleans_boilerplate_bounds_output_and_keeps_provenance(self):
        transport = FakeTransport(response(page_payload()))
        knowledge = WikiKnowledge(
            self.store,
            config=self.config(max_extract_chars=1200),
            transport=transport,
            now=self.clock,
        )

        result = knowledge.read("Trading", query="offers", max_chars=1200)

        self.assertIn("## Mechanics", result["markdown"])
        self.assertIn("## Usage", result["markdown"])
        self.assertNotIn("Old version details", result["markdown"])
        self.assertIn("source: minecraft.wiki", result["markdown"])
        self.assertLessEqual(len(result["markdown"]), 1200)
        self.assertEqual(result["revision_id"], 9001)
        self.assertTrue(result["advisory"])

    def test_rate_limit_retry_after_and_maxlag_are_bounded(self):
        sleeps = []
        transport = FakeTransport(
            HttpResult(429, {"retry-after": "2"}, b""),
            response({"error": {"code": "maxlag", "info": "Waiting for replicas"}}),
            response(search_payload()),
        )
        knowledge = WikiKnowledge(
            self.store,
            config=self.config(max_attempts=3),
            transport=transport,
            sleep=sleeps.append,
            now=self.clock,
            random_value=lambda: 0.5,
        )

        result = knowledge.search("portal")

        self.assertEqual(result["count"], 1)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps[0], 2.0)
        self.assertEqual(sleeps[1], 0.02)

    def test_expired_page_revalidates_with_etag_and_304(self):
        transport = FakeTransport(
            response(
                page_payload(),
                headers={"ETag": '"rev-9001"', "Last-Modified": "Wed, 01 Jul 2026 00:00:00 GMT"},
            ),
            HttpResult(304, {}, b""),
        )
        knowledge = WikiKnowledge(
            self.store,
            config=self.config(page_ttl_s=0),
            transport=transport,
            now=self.clock,
        )

        knowledge.read("Trading")
        result = knowledge.read("Trading")

        self.assertEqual(result["cache_status"], "revalidated")
        self.assertEqual(transport.calls[1][1]["If-None-Match"], '"rev-9001"')
        self.assertEqual(
            transport.calls[1][1]["If-Modified-Since"],
            "Wed, 01 Jul 2026 00:00:00 GMT",
        )

    def test_transport_failure_serves_explicit_stale_cache_but_cold_miss_is_honest(self):
        transport = FakeTransport(
            response(page_payload()),
            WikiUnavailable("wiki_transport_error:TimeoutError", retryable=True),
        )
        knowledge = WikiKnowledge(
            self.store,
            config=self.config(page_ttl_s=0, max_attempts=1),
            transport=transport,
            now=self.clock,
        )
        knowledge.read("Trading")

        stale = knowledge.read("Trading")

        self.assertTrue(stale["stale"])
        self.assertEqual(stale["cache_status"], "stale")
        self.assertEqual(stale["refresh_error"], "wiki_transport_error:TimeoutError")

        cold = WikiKnowledge(
            self.store,
            config=self.config(max_attempts=1),
            transport=FakeTransport(
                WikiUnavailable("wiki_transport_error:TimeoutError", retryable=True)
            ),
            now=self.clock,
        )
        with self.assertRaises(WikiUnavailable):
            cold.search("never cached")

    def test_missing_page_is_short_lived_negative_cache(self):
        transport = FakeTransport(response(page_payload(missing=True)))
        knowledge = WikiKnowledge(
            self.store,
            config=self.config(),
            transport=transport,
            now=self.clock,
        )

        self.assertIsNone(knowledge.read("Missing"))
        self.assertIsNone(knowledge.read("Missing"))
        self.assertEqual(len(transport.calls), 1)

    def test_expired_negative_cache_revalidates_without_becoming_page_content(self):
        transport = FakeTransport(
            response(page_payload(missing=True), headers={"ETag": '"missing"'}),
            HttpResult(304, {}, b""),
        )
        knowledge = WikiKnowledge(
            self.store,
            config=self.config(negative_ttl_s=0),
            transport=transport,
            now=self.clock,
        )

        self.assertIsNone(knowledge.read("Missing"))
        self.assertIsNone(knowledge.read("Missing"))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[1][1]["If-None-Match"], '"missing"')

    def test_endpoint_rejects_embedded_credentials(self):
        with self.assertRaises(ValueError):
            WikiConfig(endpoint="https://user:password@minecraft.wiki/api.php")


class WikiToolTests(unittest.TestCase):
    def test_tools_keep_wiki_advisory_and_surface_cold_outage(self):
        store = RuntimeStateStore(":memory:")
        knowledge = WikiKnowledge(
            store,
            config=WikiConfig(min_request_interval_s=0, max_attempts=1),
            transport=FakeTransport(
                WikiUnavailable("wiki_transport_error:TimeoutError", retryable=True)
            ),
        )
        registry = ToolRegistry()
        register_wiki_tools(registry, knowledge)

        result = registry.get("wiki_search").callable({"query": "trial chambers"})

        self.assertFalse(result.success)
        self.assertTrue(result.can_retry)
        self.assertEqual(result.reason, "wiki_unavailable")
        self.assertEqual(set(registry.names()), {"wiki_search", "wiki_read"})
        self.assertTrue(all(not registry.sidecar(name).mutating for name in registry.names()))
        self.assertIn("exact recipes", registry.get("wiki_search").description)
        store.close()


if __name__ == "__main__":
    unittest.main()
