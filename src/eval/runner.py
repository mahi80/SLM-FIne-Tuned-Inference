"""Run a model alias against the golden set and collect contract scores."""

from __future__ import annotations

import asyncio
import json

import httpx

from finetune.prompt import prompt_messages

from .scoring import score, slice_of, summarise


async def _call(client, url, model, ticket, api_key, sem, max_tokens):
    async with sem:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = await client.post(
            f"{url}/v1/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": prompt_messages(ticket),
                "temperature": 0.0,
                "max_tokens": max_tokens,
            },
            timeout=180.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def evaluate_model(
    url: str,
    model: str,
    rows: list[dict],
    concurrency: int,
    api_key: str | None = None,
    max_tokens: int = 220,
) -> dict:
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    failures: list[dict] = []
    done = 0

    async with httpx.AsyncClient() as client:

        async def one(row: dict) -> None:
            nonlocal done
            try:
                raw = await _call(
                    client, url, model, row["ticket"], api_key, sem, max_tokens
                )
            except Exception as exc:  # a failed call is a failed sample, not a crash
                sample = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            else:
                sample = score(row["target"], raw)
                sample["slice"] = slice_of(row["id"])
                if not sample["schema"] and len(failures) < 5:
                    failures.append({"id": row["id"], "raw": raw[:300]})
            sample["id"] = row["id"]
            results.append(sample)
            done += 1
            if done % 10 == 0:
                print(f"  {model}: {done}/{len(rows)}", flush=True)

        await asyncio.gather(*(one(row) for row in rows))

    summary = summarise(model, results)
    summary["failures"] = failures
    return summary


def load_golden(path: str, limit: int = 0) -> list[dict]:
    rows = [
        json.loads(line)
        for line in open(path, encoding="utf-8").read().splitlines()
        if line.strip()
    ]
    for row in rows:
        for key in ("id", "ticket", "target"):
            if key not in row:
                raise ValueError(f"golden row is missing '{key}': {row.get('id')}")
    return rows[:limit] if limit else rows
