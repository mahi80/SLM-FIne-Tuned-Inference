"""Tests for the external-backend connectivity harness.

No IdP and no provider is contacted: the harness is given an injected
`httpx.AsyncClient` whose transport is `httpx.MockTransport`, which is the same
seam an operator uses against a real cluster.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from tools import check_backend as cb

TOKEN_URL = "https://idp.example.com/oauth2/token"
STATIC_SECRET = "static-secret-value"
CLIENT_SECRET = "oidc-client-secret"
FILE_TOKEN = "projected-token-value"
IDP_TOKEN = "idp-access-token"

POLICY = """
version: "test-check"
aliases:
  burst:
    primary: gpu
    candidates:
      - name: gpu
        compute: gpu
        deployment: onprem
        pool: onprem-gpu
        backend: http://vllm-gpu:8000
        upstream_model: model-gpu
        serves_regions: [eu, us]
        sovereign: true
      - name: cloud-static
        compute: gpu
        deployment: cloud
        pool: cloud-prod
        backend: https://static.provider.example
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        access:
          key: external-prod
      - name: cloud-file
        compute: gpu
        deployment: cloud
        pool: cloud-prod
        backend: https://vllm.internal.example.com
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        access:
          key: provider-token
          source: file
  secondary:
    primary: cloud-oidc
    candidates:
      - name: cloud-oidc
        compute: gpu
        deployment: cloud
        pool: cloud-prod
        backend: https://inference.provider.example
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        access:
          key: provider-oidc
          source: oidc
          token_url: https://idp.example.com/oauth2/token
          client_id: slm-gateway
          scope: inference.read
"""

INTERNAL_ONLY = """
version: "test-check-internal"
aliases:
  slm-small:
    primary: gpu
    candidates:
      - name: gpu
        compute: gpu
        deployment: onprem
        pool: onprem-gpu
        backend: http://vllm-gpu:8000
        upstream_model: model-gpu
        serves_regions: [eu, us]
        sovereign: true
"""


class HarnessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.policy = self.root / "policy.yaml"
        self.policy.write_text(POLICY, encoding="utf-8")
        self.keys = self.root / "keys.yaml"
        self.write_keys()
        self.tokens = self.root / "tokens"
        self.tokens.mkdir()
        self.write_token(FILE_TOKEN)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_keys(self, *, static: bool = True, oidc: bool = True) -> None:
        entries = {}
        if static:
            entries["external-prod"] = STATIC_SECRET
        if oidc:
            entries["provider-oidc"] = CLIENT_SECRET
        text = "keys:\n" + "".join(f"  {name}: {value}\n" for name, value in entries.items())
        self.keys.write_text(text or "keys: {}\n", encoding="utf-8")

    def write_token(self, value: str) -> None:
        (self.tokens / "provider-token").write_text(value + "\n", encoding="utf-8")

    async def run_tool(self, handler, *extra: str) -> tuple[int, list[str]]:
        buffer = io.StringIO()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with mock.patch.dict(os.environ, {"SLM_BACKEND_TOKEN_DIR": str(self.tokens)}):
                with contextlib.redirect_stdout(buffer):
                    code = await cb.run(
                        [
                            "--policy",
                            str(self.policy),
                            "--keys",
                            str(self.keys),
                            *extra,
                        ],
                        client=client,
                    )
        return code, [line for line in buffer.getvalue().splitlines() if line.strip()]

    def line_for(self, lines: list[str], candidate: str) -> str:
        matches = [line for line in lines if f"candidate={candidate} " in line]
        self.assertEqual(len(matches), 1, f"expected one line for {candidate}: {lines}")
        return matches[0]

    async def test_every_configured_candidate_is_reported_and_passes(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if str(request.url) == TOKEN_URL:
                return httpx.Response(
                    200, json={"access_token": IDP_TOKEN, "expires_in": 300}
                )
            return httpx.Response(200)

        code, lines = await self.run_tool(handler)

        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 3)
        static = self.line_for(lines, "cloud-static")
        self.assertIn("alias=burst", static)
        self.assertIn("host=static.provider.example", static)
        self.assertIn("key=external-prod", static)
        self.assertIn("source=static", static)
        self.assertIn("configured", static)
        self.assertIn("probe=up", static)
        self.assertIn("latency_ms=", static)

        self.assertIn("source=file", self.line_for(lines, "cloud-file"))
        oidc = self.line_for(lines, "cloud-oidc")
        self.assertIn("alias=secondary", oidc)
        self.assertIn("source=oidc", oidc)
        self.assertIn("probe=up", oidc)

        # The internal candidate is not an external backend and has no line.
        self.assertFalse(any("candidate=gpu " in line for line in lines))

    async def test_the_output_never_carries_a_secret_or_a_header(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(200, json={"access_token": IDP_TOKEN})
            return httpx.Response(200)

        _, lines = await self.run_tool(handler)
        output = "\n".join(lines)
        for secret in (STATIC_SECRET, CLIENT_SECRET, FILE_TOKEN, IDP_TOKEN):
            self.assertNotIn(secret, output)
        self.assertNotIn("Bearer", output)
        self.assertNotIn("authorization", output.lower())

    async def test_a_missing_token_file_is_unconfigured_and_fails(self) -> None:
        (self.tokens / "provider-token").unlink()

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(200, json={"access_token": IDP_TOKEN})
            return httpx.Response(200)

        code, lines = await self.run_tool(handler)

        self.assertEqual(code, 1)
        line = self.line_for(lines, "cloud-file")
        self.assertIn("unconfigured", line)
        self.assertIn("probe=unconfigured", line)
        self.assertIn("latency_ms=0.0", line)
        self.assertIn("probe=up", self.line_for(lines, "cloud-static"))

    async def test_a_missing_key_is_unconfigured_and_fails(self) -> None:
        self.write_keys(static=False)

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(200, json={"access_token": IDP_TOKEN})
            return httpx.Response(200)

        code, lines = await self.run_tool(handler)

        self.assertEqual(code, 1)
        self.assertIn("unconfigured", self.line_for(lines, "cloud-static"))
        self.assertIn("probe=up", self.line_for(lines, "cloud-file"))

    async def test_an_authentication_failure_fails_without_quoting_the_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(
                    401, json={"error": "invalid_client", "detail": "boom-body"}
                )
            return httpx.Response(200)

        code, lines = await self.run_tool(handler)

        self.assertEqual(code, 1)
        oidc = self.line_for(lines, "cloud-oidc")
        self.assertIn("probe=auth-error", oidc)
        self.assertIn("configured", oidc)
        output = "\n".join(lines)
        self.assertNotIn("boom-body", output)
        self.assertNotIn(CLIENT_SECRET, output)

    async def test_a_down_backend_fails_the_run(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(200, json={"access_token": IDP_TOKEN})
            if str(request.url).startswith("https://static.provider.example"):
                return httpx.Response(503)
            return httpx.Response(200)

        code, lines = await self.run_tool(handler)

        self.assertEqual(code, 1)
        self.assertIn("probe=down", self.line_for(lines, "cloud-static"))
        self.assertIn("probe=up", self.line_for(lines, "cloud-file"))

    async def test_a_4xx_health_answer_still_counts_as_reachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(200, json={"access_token": IDP_TOKEN})
            return httpx.Response(401)

        code, lines = await self.run_tool(handler)

        self.assertEqual(code, 0)
        self.assertIn("probe=up", self.line_for(lines, "cloud-static"))

    async def test_a_policy_without_external_candidates_exits_zero(self) -> None:
        self.policy.write_text(INTERNAL_ONLY, encoding="utf-8")

        code, lines = await self.run_tool(lambda request: httpx.Response(200))

        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("no external candidates", lines[0])

    async def test_a_missing_ca_bundle_is_reported_without_a_traceback(self) -> None:
        code, lines = await self.run_tool(
            lambda request: httpx.Response(200), "--ca", str(self.root / "absent.crt")
        )

        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("backend CA bundle not found", lines[0])
        self.assertNotIn(STATIC_SECRET, lines[0])

    async def test_one_bad_candidate_does_not_hide_the_good_ones(self) -> None:
        (self.tokens / "provider-token").unlink()

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return httpx.Response(200, json={"access_token": IDP_TOKEN})
            return httpx.Response(200)

        code, lines = await self.run_tool(handler)

        self.assertEqual(code, 1)
        self.assertEqual(len(lines), 3)
        self.assertIn("probe=up", self.line_for(lines, "cloud-static"))
        self.assertIn("probe=up", self.line_for(lines, "cloud-oidc"))
        self.assertIn("probe=unconfigured", self.line_for(lines, "cloud-file"))


class FormatTest(unittest.TestCase):
    def test_a_failed_check_is_marked_and_a_healthy_one_is_not(self) -> None:
        healthy = cb.Check(
            "burst", "cloud", "inference.example.com", "external-prod", "static", True, cb.UP, 12.34
        )
        down = cb.Check(
            "burst", "cloud", "inference.example.com", "external-prod", "oidc", True, cb.DOWN, 0.0
        )
        auth = cb.Check(
            "burst",
            "cloud",
            "inference.example.com",
            "external-prod",
            "oidc",
            True,
            cb.AUTH_ERROR,
            0.5,
        )
        unconfigured = cb.Check(
            "burst",
            "cloud",
            "inference.example.com",
            "external-prod",
            "file",
            False,
            cb.UNCONFIGURED,
            0.0,
        )
        self.assertTrue(cb.format_line(healthy).startswith("OK"))
        self.assertIn("latency_ms=12.3", cb.format_line(healthy))
        for check in (down, auth, unconfigured):
            with self.subTest(outcome=check.outcome):
                self.assertTrue(cb.format_line(check).startswith("FAIL"))
                self.assertTrue(check.failed)
        self.assertFalse(healthy.failed)
        self.assertEqual(cb.exit_code([healthy]), 0)
        self.assertEqual(cb.exit_code([healthy, down]), 1)


if __name__ == "__main__":
    unittest.main()
