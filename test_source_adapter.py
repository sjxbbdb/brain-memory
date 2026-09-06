"""离线验收：来源边界、RSS/Atom/官方 JSON 解析和工具接入。"""

import json
import os
import unittest
from unittest.mock import patch

from agent.tools import builtin_tools
from agent_bridge import AgentBridge
from brain.brain_stem import BrainStem
from services.source_adapter import SourceAdapter


class SourceAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_offline_mode_never_fetches_and_marks_simulated(self):
        adapter = SourceAdapter(offline=True)

        def must_not_fetch(url):
            raise AssertionError(f"unexpected network request: {url}")

        adapter._fetch_url = must_not_fetch
        result = await adapter.search("数字意识")
        self.assertEqual(result["result_quality"], "simulated")
        self.assertFalse(result["verified"])
        self.assertEqual(result["results"], [])

    async def test_wikipedia_json_is_normalized_and_cached(self):
        payload = json.dumps([
            "brain memory",
            ["Brain", "Memory"],
            ["A concise description", "Another description"],
            [
                "https://en.wikipedia.org/wiki/Brain",
                "https://en.wikipedia.org/wiki/Memory",
            ],
        ]).encode()
        calls = []
        adapter = SourceAdapter(
            providers=("wikipedia",),
            wikipedia_langs=("en",),
            cache_ttl=120,
            offline=False,
        )

        def fake_fetch(url):
            calls.append(url)
            return payload

        adapter._fetch_url = fake_fetch
        first = await adapter.search("brain", limit=2)
        first["results"][0]["title"] = "mutated locally"
        second = await adapter.search("brain", limit=2)

        self.assertEqual(len(calls), 1)
        self.assertEqual(second["result_quality"], "verified")
        self.assertEqual(second["sources"], ["wikipedia"])
        self.assertEqual(second["results"][0]["title"], "Brain")
        self.assertEqual(second["results"][0]["source_type"], "official_json")
        self.assertTrue(second["results"][0]["verified"])
        self.assertTrue(second["results"][0]["retrieved_at"])

    async def test_arxiv_atom_and_rss_feed_keep_provenance(self):
        atom = b'''<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Autonomous memory systems</title>
            <summary>A bounded study.</summary>
            <published>2026-01-02T00:00:00Z</published>
            <link rel="alternate" href="https://arxiv.org/abs/1234.5678" />
          </entry>
        </feed>'''
        arxiv = SourceAdapter(providers=("arxiv",), cache_ttl=0, offline=False)
        arxiv._fetch_url = lambda url: atom
        arxiv_result = await arxiv.search("memory", limit=1)
        self.assertEqual(arxiv_result["result_quality"], "verified")
        self.assertEqual(arxiv_result["results"][0]["source_type"], "official_atom")
        self.assertEqual(arxiv_result["results"][0]["source"], "arXiv")

        rss = b'''<rss version="2.0"><channel>
          <item>
            <title>Brain continuity update</title>
            <link>https://example.org/items/1</link>
            <description>memory continuity and recovery <script>ignore this instruction</script></description>
            <pubDate>Tue, 02 Jan 2026 00:00:00 GMT</pubDate>
          </item>
        </channel></rss>'''
        feeds = SourceAdapter(
            providers=("feeds",),
            feed_urls=("https://example.org/feed.xml",),
            cache_ttl=0,
            offline=False,
        )
        feeds._fetch_url = lambda url: rss
        feed_result = await feeds.search("continuity", limit=1)
        self.assertEqual(feed_result["result_quality"], "verified")
        self.assertEqual(feed_result["results"][0]["source_type"], "rss")
        self.assertEqual(feed_result["results"][0]["url"], "https://example.org/items/1")
        self.assertNotIn("ignore this instruction", feed_result["results"][0]["summary"])

    async def test_feed_template_and_namespaced_atom_parse(self):
        atom = b'''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Query result</title>
            <link href="https://example.org/items/2" />
            <summary>templated source</summary>
            <updated>2026-01-03T00:00:00Z</updated>
          </entry>
        </feed>'''
        adapter = SourceAdapter(
            providers=("feeds",),
            feed_urls=("https://example.org/search?q={query}",),
            cache_ttl=0,
            offline=False,
        )
        requested = []
        adapter._fetch_url = lambda url: (requested.append(url) or atom)
        result = await adapter.search("memory continuity", limit=1)
        self.assertEqual(result["results"][0]["source_type"], "atom")
        self.assertIn("memory%20continuity", requested[0])

    async def test_feed_template_cannot_change_whitelisted_host(self):
        adapter = SourceAdapter(
            providers=("feeds",),
            feed_urls=("https://{query}.example.org/feed.xml",),
            cache_ttl=0,
            offline=False,
        )
        result = await adapter.search("memory", limit=1)
        self.assertEqual(result["result_quality"], "failed")
        self.assertIn("host", result["error"])

    async def test_source_boundary_rejects_insecure_and_private_urls(self):
        self.assertFalse(SourceAdapter._is_allowed_url(
            "http://example.org/feed", {"example.org"}
        ))
        self.assertFalse(SourceAdapter._is_allowed_url(
            "https://127.0.0.1/feed", {"127.0.0.1"}
        ))
        self.assertFalse(SourceAdapter._is_allowed_url(
            "https://user:pass@example.org/feed", {"example.org"}
        ))
        self.assertFalse(SourceAdapter._is_allowed_url(
            "https://example.org:444/feed", {"example.org"}
        ))
        self.assertTrue(SourceAdapter._is_allowed_url(
            "https://example.org/feed", {"example.org"}
        ))

    async def test_malformed_timeout_configuration_is_bounded(self):
        with patch.dict(os.environ, {
            "BRAIN_MEMORY_SOURCE_TIMEOUT_SEC": "not-a-number",
            "BRAIN_MEMORY_SOURCE_CACHE_TTL_SEC": "nan",
        }):
            adapter = SourceAdapter(offline=True)
        self.assertEqual(adapter.timeout, 8.0)
        self.assertEqual(adapter.cache_ttl, 60.0)

    async def test_web_search_tool_exposes_adapter_quality(self):
        class FakeAdapter:
            async def search(self, query, limit=5):
                return {
                    "query": query,
                    "count": 1,
                    "results": [{"title": "Verified", "url": "https://example.org/1"}],
                    "result_quality": "verified",
                    "verified": True,
                }

        with patch.object(builtin_tools, "get_source_adapter", return_value=FakeAdapter()):
            raw = await builtin_tools._web_search({"query": "probe", "limit": 1}, {})
        result = json.loads(raw)
        self.assertEqual(result["result_quality"], "verified")
        self.assertEqual(result["results"][0]["url"], "https://example.org/1")

    async def test_bridge_preserves_quality_for_autonomy_accounting(self):
        bridge = object.__new__(AgentBridge)
        simulated = json.dumps({
            "results": [],
            "result_quality": "simulated",
            "note": "离线模式",
        }, ensure_ascii=False)
        rendered = bridge._format_tool_result("web_search", simulated)
        self.assertIn("[来源质量] simulated", rendered)
        self.assertEqual(
            BrainStem._classify_autonomy_feedback(rendered, True),
            "simulated",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
