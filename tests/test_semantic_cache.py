import asyncio
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from gateway.semantic_cache import (
    HashingEmbedder,
    HttpEmbedder,
    SemanticCache,
    as_sse,
    completion_from_stream,
)

REPO = Path(__file__).resolve().parent.parent


def body(question: str, **overrides) -> dict:
    data = {
        "model": "slm-small",
        "messages": [
            {"role": "system", "content": "You are a maintenance assistant."},
            {"role": "user", "content": question},
        ],
        "max_tokens": 64,
        "temperature": 0,
    }
    data.update(overrides)
    return data


def completion(text: str) -> dict:
    return {
        "id": "chatcmpl-x",
        "object": "chat.completion",
        "model": "slm-small",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class HashingEmbedderTest(unittest.TestCase):
    def test_near_duplicates_score_high_and_unrelated_low(self) -> None:
        e = HashingEmbedder()
        a = e.embed_sync("Why does vibration monitoring matter on a pump?")
        b = e.embed_sync("why does vibration monitoring matter on a pump")
        c = e.embed_sync("Why does vibration monitoring matter on pumps?")
        d = e.embed_sync("How do I reset the conveyor PLC after an e-stop?")
        cos = lambda x, y: sum(i * j for i, j in zip(x, y))
        self.assertAlmostEqual(cos(a, b), 1.0, places=6)
        self.assertGreater(cos(a, c), 0.92)
        self.assertLess(cos(a, d), 0.5)

    def test_deterministic(self) -> None:
        self.assertEqual(HashingEmbedder().embed_sync("x y"), HashingEmbedder().embed_sync("x y"))


class PrepareTest(unittest.TestCase):
    def test_cacheable_request(self) -> None:
        self.assertIsNotNone(SemanticCache.prepare(body("q"), "acme", "slm-small", "v1:gpu"))

    def test_non_deterministic_requests_are_skipped(self) -> None:
        for overrides in (
            {"temperature": 0.7},
            {"temperature": None},
            {"n": 2},
            {"seed": 7},
            {"tools": [{"type": "function"}]},
            {"logprobs": True},
        ):
            with self.subTest(overrides=overrides):
                data = body("q", **overrides)
                if overrides == {"temperature": None}:
                    del data["temperature"]  # default temperature is 1
                self.assertIsNone(SemanticCache.prepare(data, "acme", "slm-small", "v1"))

    def test_last_turn_must_be_plain_user_text(self) -> None:
        data = body("q")
        data["messages"].append({"role": "assistant", "content": "a"})
        self.assertIsNone(SemanticCache.prepare(data, "acme", "slm-small", "v1"))
        data = body("q")
        data["messages"][-1]["content"] = [{"type": "text", "text": "q"}]
        self.assertIsNone(SemanticCache.prepare(data, "acme", "slm-small", "v1"))

    def test_namespace_isolates_tenant_route_history_and_params(self) -> None:
        base = SemanticCache.prepare(body("q"), "acme", "slm-small", "v1:gpu").namespace
        other_system = body("q")
        other_system["messages"][0]["content"] = "You are a lawyer."
        variants = [
            SemanticCache.prepare(body("q"), "globex", "slm-small", "v1:gpu"),
            SemanticCache.prepare(body("q"), "acme", "slm-triage", "v1:gpu"),
            SemanticCache.prepare(body("q"), "acme", "slm-small", "v2:gpu"),
            SemanticCache.prepare(body("q"), "acme", "slm-small", "v1:gpu-canary"),
            SemanticCache.prepare(body("q", max_tokens=8), "acme", "slm-small", "v1:gpu"),
            SemanticCache.prepare(other_system, "acme", "slm-small", "v1:gpu"),
        ]
        for variant in variants:
            self.assertNotEqual(variant.namespace, base)
        # Stream flags do not change the answer, so they share the namespace.
        streamed = SemanticCache.prepare(
            body("q", stream=True, stream_options={"include_usage": True}), "acme", "slm-small", "v1:gpu"
        )
        self.assertEqual(streamed.namespace, base)


class StoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.cache = SemanticCache(HashingEmbedder(), threshold=0.92, ttl_s=60, max_entries=3, clock=self.clock)

    def lookup(self, question: str, tenant: str = "acme"):
        return SemanticCache.prepare(body(question), tenant, "slm-small", "v1:gpu")

    async def test_hit_on_near_duplicate_miss_on_different_question(self) -> None:
        first = self.lookup("Why does vibration monitoring matter on a pump?")
        self.assertIsNone(await self.cache.get(first))
        await self.cache.put(first, completion("because"))
        hit = await self.cache.get(self.lookup("why does vibration monitoring matter on a pump"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.response["choices"][0]["message"]["content"], "because")
        self.assertGreaterEqual(hit.similarity, 0.92)
        self.assertIsNone(await self.cache.get(self.lookup("How do I reset the conveyor PLC?")))

    async def test_other_tenant_never_hits(self) -> None:
        await self.cache.put(self.lookup("q about pumps"), completion("a"))
        self.assertIsNone(await self.cache.get(self.lookup("q about pumps", tenant="globex")))

    async def test_entries_expire(self) -> None:
        await self.cache.put(self.lookup("q about pumps"), completion("a"))
        self.clock.now += 61
        self.assertIsNone(await self.cache.get(self.lookup("q about pumps")))
        self.assertEqual(len(self.cache), 0)

    async def test_lru_bound(self) -> None:
        for i in range(5):
            await self.cache.put(self.lookup(f"distinct question number {i} zz{i}"), completion(str(i)))
        self.assertEqual(len(self.cache), 3)
        self.assertIsNone(await self.cache.get(self.lookup("distinct question number 0 zz0")))

    def test_rejects_bad_settings(self) -> None:
        with self.assertRaises(ValueError):
            SemanticCache(HashingEmbedder(), threshold=0)
        with self.assertRaises(ValueError):
            SemanticCache(HashingEmbedder(), ttl_s=0)


class HttpEmbedderTest(unittest.IsolatedAsyncioTestCase):
    async def test_reads_openai_embeddings(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v1/embeddings")
            self.assertEqual(json.loads(request.content)["model"], "embed-small")
            return httpx.Response(200, json={"data": [{"embedding": [3.0, 4.0]}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            vector = await HttpEmbedder("http://embed", "embed-small", client).embed("x")
        self.assertEqual(vector, [0.6, 0.8])

    async def test_failure_is_a_miss_not_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertIsNone(await HttpEmbedder("http://embed", "m", client).embed("x"))


class ReplayTest(unittest.TestCase):
    def test_stream_round_trip(self) -> None:
        stored = completion_from_stream("r1", "slm-small", "hello world", "stop", {"prompt_tokens": 3, "completion_tokens": 2})
        events = [e.decode() for e in as_sse(stored)]
        self.assertEqual(events[-1], "data: [DONE]\n\n")
        first = json.loads(events[0][6:])
        self.assertEqual(first["choices"][0]["delta"]["content"], "hello world")
        self.assertEqual(json.loads(events[2][6:])["usage"]["completion_tokens"], 2)


class GatewayIntegrationTest(unittest.TestCase):
    """Drive the real FastAPI app with the upstream call stubbed out."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception as exc:  # pragma: no cover - CI installs fastapi
            raise unittest.SkipTest(f"fastapi test client unavailable: {exc}")
        cls.tmp = tempfile.TemporaryDirectory()
        tenants = Path(cls.tmp.name) / "tenants.yaml"
        source = (REPO / "config/tenants.example.yaml").read_text(encoding="utf-8")
        tenants.write_text(
            source.replace("    admin: true\n", "    admin: true\n    semantic_cache: true\n", 1),
            encoding="utf-8",
        )
        env = {
            "POLICY_FILE": str(REPO / "config/policy.yaml"),
            "TENANTS_FILE": str(tenants),
            "AUDIT_FILE": str(Path(cls.tmp.name) / "audit.jsonl"),
            "SLM_BACKEND_KEYS": str(Path(cls.tmp.name) / "missing-keys.yaml"),
            "SLM_PROBE_INTERVAL_S": "3600",
        }
        cls.env = mock.patch.dict(os.environ, env)
        cls.env.start()
        import gateway.app as app_module

        cls.app_module = importlib.reload(app_module)
        from fastapi.testclient import TestClient

        # One client for the class: the app's lifespan closes the audit log.
        cls.client = TestClient(cls.app_module.app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls.env.stop()
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.calls = 0
        self.app_module.semantic_cache._entries.clear()

        async def fake_upstream(client, candidate, access, payload, stream):
            self.calls += 1
            if stream:
                chunks = [
                    {"choices": [{"index": 0, "delta": {"content": "Vibration "}}]},
                    {"choices": [{"index": 0, "delta": {"content": "warns early."}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 2}},
                ]
                text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
                return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json=completion("Vibration warns early."))

        self.patch = mock.patch.object(self.app_module, "open_authenticated", fake_upstream)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()

    def post(self, client, question, key="dev-acme-key", **overrides):
        return client.post(
            "/v1/chat/completions",
            json=body(question, **overrides),
            headers={"Authorization": f"Bearer {key}"},
        )

    def test_second_near_duplicate_is_served_from_cache(self) -> None:
        client = self.client
        first = self.post(client, "Why does vibration monitoring matter on a pump?")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers.get("x-cache"), "miss")
        second = self.post(client, "why does vibration monitoring matter on a pump")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.headers["x-cache"], "hit")
        self.assertEqual(second.headers["x-tier"], "cache")
        self.assertEqual(second.json()["choices"][0]["message"]["content"], "Vibration warns early.")
        self.assertEqual(self.calls, 1)

    def test_stream_is_stored_and_replayed(self) -> None:
        client = self.client
        first = self.post(client, "Explain bearing faults", stream=True)
        self.assertIn("warns early", first.text)
        second = self.post(client, "explain bearing faults", stream=True)
        self.assertEqual(second.headers["x-cache"], "hit")
        self.assertIn("Vibration warns early.", second.text)
        self.assertTrue(second.text.rstrip().endswith("data: [DONE]"))
        self.assertEqual(self.calls, 1)

    def test_tenant_without_opt_in_and_sampling_requests_bypass(self) -> None:
        client = self.client
        for _ in range(2):
            response = self.post(client, "same question", key="dev-globex-key")
            self.assertNotIn("x-cache", response.headers)
        for _ in range(2):
            self.post(client, "another question", temperature=0.8)
        self.assertEqual(self.calls, 4)


if __name__ == "__main__":
    unittest.main()
