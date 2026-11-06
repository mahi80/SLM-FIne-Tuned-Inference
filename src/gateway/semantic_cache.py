"""Per-tenant semantic response cache for the gateway.

A hit returns a stored completion for a request whose *last user message* is
close enough to one already answered, without touching a GPU. Everything else
in the request has to match exactly, so a cache entry is only reused where the
answer would have been the same:

* the namespace is `tenant + alias + policy version + every message except the
  last user turn + max_tokens + any other sampling field`. A different system
  prompt, a different conversation history, a policy reload (new primary, new
  canary) or a different tenant can never share an entry;
* only deterministic requests are cached: `temperature` must be 0 and `n`
  absent or 1, and requests with tools, `logprobs` or `seed` are never cached;
* similarity is cosine over an embedding of the last user message, compared to
  a threshold (default 0.92) inside the namespace only.

Tenants opt in with `semantic_cache: true` in tenants.yaml. It is off by default
because a cache trades a small chance of a near-miss answer for latency and
cost, and that trade belongs to the tenant.

Two embedders ship:

* `HashingEmbedder` (default) -- character trigrams hashed into a fixed vector.
  No model, no network, deterministic. It catches casing, punctuation,
  whitespace, word-order and small-edit duplicates, which is most repeat
  traffic from forms and agents. It does not understand paraphrase.
* `HttpEmbedder` -- any OpenAI-compatible `/v1/embeddings` endpoint
  (`SLM_EMBEDDINGS_URL`), e.g. a small embedding model next to vLLM. Use it
  when paraphrase hits matter; a failure falls back to a cache miss.

The store is in-process and per replica, like the rate limiter: a second
gateway replica has its own cache. Entries expire after `ttl_s` and the store is
bounded by `max_entries` with least-recently-used eviction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

# Fields that change what the model would produce. Anything else in the body
# (user, metadata, stream, stream_options) does not affect the answer.
_ANSWER_FIELDS = (
    "max_tokens",
    "max_completion_tokens",
    "top_p",
    "top_k",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "repetition_penalty",
    "response_format",
)
# Requests carrying any of these are never cached.
_UNCACHEABLE_FIELDS = ("tools", "tool_choice", "functions", "logprobs", "top_logprobs", "seed")


class Embedder(Protocol):
    name: str

    async def embed(self, text: str) -> list[float] | None: ...


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


class HashingEmbedder:
    """Character-trigram feature hashing. Cheap, local, paraphrase-blind."""

    name = "hashing-trigram"

    def __init__(self, dims: int = 512) -> None:
        self.dims = dims

    async def embed(self, text: str) -> list[float] | None:
        return self.embed_sync(text)

    def embed_sync(self, text: str) -> list[float]:
        vector = [0.0] * self.dims
        for word in _normalise(text).split():
            padded = f" {word} "
            for i in range(len(padded) - 2):
                digest = hashlib.blake2b(padded[i : i + 3].encode(), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                sign = 1.0 if value & 1 else -1.0
                vector[(value >> 1) % self.dims] += sign
        return _unit(vector)


class HttpEmbedder:
    """OpenAI-compatible embeddings endpoint. Errors become cache misses."""

    def __init__(self, url: str, model: str, client, api_key: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.client = client
        self.api_key = api_key
        self.name = f"http:{model}"

    async def embed(self, text: str) -> list[float] | None:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            response = await self.client.post(
                f"{self.url}/v1/embeddings",
                json={"model": self.model, "input": text},
                headers=headers,
                timeout=2.0,
            )
            if response.status_code != 200:
                return None
            data = response.json()["data"][0]["embedding"]
            return _unit([float(v) for v in data])
        except Exception:  # an embedding outage must not fail inference
            return None


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


@dataclass
class CacheEntry:
    vector: list[float]
    query: str
    response: dict[str, Any]
    created: float
    hits: int = 0


@dataclass
class Lookup:
    namespace: str
    query: str
    vector: list[float] | None = None


@dataclass
class Hit:
    response: dict[str, Any]
    similarity: float
    age_s: float


@dataclass
class SemanticCache:
    embedder: Embedder
    threshold: float = 0.92
    ttl_s: float = 600.0
    max_entries: int = 2048
    clock: Any = time.monotonic
    _entries: OrderedDict = field(default_factory=OrderedDict)

    def __post_init__(self) -> None:
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError("semantic cache threshold must be in (0, 1]")
        if self.ttl_s <= 0 or self.max_entries <= 0:
            raise ValueError("semantic cache ttl_s and max_entries must be positive")

    # -- request shaping ---------------------------------------------------

    @staticmethod
    def prepare(body: dict, tenant_id: str, alias: str, policy_version: str) -> Lookup | None:
        """Return a lookup for a cacheable request, or None if it must not be cached."""
        if any(body.get(name) not in (None, [], {}) for name in _UNCACHEABLE_FIELDS):
            return None
        temperature = body.get("temperature", 1.0)
        try:
            if float(temperature) != 0.0:
                return None
            if int(body.get("n") or 1) != 1:
                return None
        except (TypeError, ValueError):
            return None
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            return None
        content = last.get("content")
        if not isinstance(content, str) or not content.strip():
            return None  # multimodal or empty turns are not cached
        context = {
            "tenant": tenant_id,
            "alias": alias,
            "policy": policy_version,
            "history": messages[:-1],
            "params": {name: body.get(name) for name in _ANSWER_FIELDS},
        }
        namespace = hashlib.sha256(
            json.dumps(context, sort_keys=True, default=str).encode()
        ).hexdigest()
        return Lookup(namespace=namespace, query=content)

    # -- store -------------------------------------------------------------

    def _expire(self) -> None:
        now = self.clock()
        stale = [key for key, entry in self._entries.items() if now - entry.created > self.ttl_s]
        for key in stale:
            del self._entries[key]

    async def get(self, lookup: Lookup) -> Hit | None:
        self._expire()
        lookup.vector = await self.embedder.embed(lookup.query)
        if lookup.vector is None:
            return None
        best_key, best_score = None, -1.0
        for key, entry in self._entries.items():
            if key[0] != lookup.namespace:
                continue
            score = cosine(lookup.vector, entry.vector)
            if score > best_score:
                best_key, best_score = key, score
        if best_key is None or best_score < self.threshold:
            return None
        entry = self._entries[best_key]
        self._entries.move_to_end(best_key)
        entry.hits += 1
        return Hit(
            response=entry.response,
            similarity=round(best_score, 4),
            age_s=round(self.clock() - entry.created, 1),
        )

    async def put(self, lookup: Lookup, response: dict[str, Any]) -> None:
        vector = lookup.vector or await self.embedder.embed(lookup.query)
        if vector is None:
            return
        key = (lookup.namespace, hashlib.sha256(lookup.query.encode()).hexdigest())
        self._entries[key] = CacheEntry(
            vector=vector, query=lookup.query, response=response, created=self.clock()
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)


def completion_from_stream(
    request_id: str, model: str, content: str, finish_reason: str | None, usage: dict
) -> dict[str, Any]:
    """Rebuild a non-streaming completion from relayed stream chunks for storage."""
    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason or "stop",
            }
        ],
        "usage": usage,
    }


def as_sse(response: dict[str, Any]) -> list[bytes]:
    """Replay a stored completion as an OpenAI SSE stream (one content chunk)."""
    choice = (response.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    base = {
        "id": response.get("id"),
        "object": "chat.completion.chunk",
        "created": response.get("created"),
        "model": response.get("model"),
    }
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}]},
    ]
    events = [f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks]
    if response.get("usage"):
        events.append(
            f"data: {json.dumps({**base, 'choices': [], 'usage': response['usage']})}\n\n".encode()
        )
    events.append(b"data: [DONE]\n\n")
    return events
