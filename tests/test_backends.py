"""Tests for external backend credentials, TLS and health probing.

The TLS tests run a throwaway HTTPS server with a self-signed certificate for
127.0.0.1 and skip when `openssl` is unavailable; everything else needs only
httpx and PyYAML, matching the CI test environment.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from gateway.backends import (
    CredentialError,
    BackendAccess,
    auth_headers,
    load_backend_keys,
    open_authenticated,
    open_upstream,
    probe,
)
from router import load_policy
from router.policy import Access, Candidate

REPO = Path(__file__).parents[1]
TOKEN_URL = "https://idp.example.com/oauth2/token"


def candidate(backend: str = "https://inference.example.com", access=None) -> Candidate:
    return Candidate(
        name="cloud",
        compute="gpu",
        deployment="cloud",
        pool="cloud-prod",
        backend=backend,
        upstream_model="model-cloud",
        region="us",
        serves_regions=("*",),
        sovereign=False,
        priority=0,
        max_context=8192,
        cost_per_1k_out=1.0,
        model_version="model-cloud",
        access=access,
    )


def oidc_access(**overrides) -> Access:
    values = {
        "key": "provider-oidc",
        "source": "oidc",
        "token_url": TOKEN_URL,
        "client_id": "slm-gateway",
        "scope": "inference.read",
    }
    values.update(overrides)
    return Access(**values)


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Idp:
    """A token endpoint that counts exchanges and hands out a new token each
    time, so a test can tell a cached token from a fresh one."""

    def __init__(
        self,
        *,
        expires_in=None,
        status: int = 200,
        payload: bytes | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.expires_in = expires_in
        self.status = status
        self.payload = payload

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(
                self.status, json={"error": "invalid_client", "detail": "boom-body"}
            )
        if self.payload is not None:
            return httpx.Response(200, content=self.payload)
        body = {"access_token": f"token-{len(self.requests)}"}
        if self.expires_in is not None:
            body["expires_in"] = self.expires_in
        return httpx.Response(200, json=body)

    @property
    def count(self) -> int:
        return len(self.requests)

    def form(self, index: int = 0) -> dict[str, str]:
        return dict(httpx.QueryParams(self.requests[index].content.decode()))


class KeyFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, text: str) -> Path:
        path = self.root / "keys.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_means_no_keys(self) -> None:
        self.assertEqual(load_backend_keys(self.root / "absent.yaml"), {})

    def test_missing_path_means_no_keys(self) -> None:
        self.assertEqual(load_backend_keys(None), {})

    def test_keys_are_read(self) -> None:
        keys = load_backend_keys(self.write("keys:\n  external-prod: s3cret\n"))
        self.assertEqual(keys, {"external-prod": "s3cret"})

    def test_empty_file_means_no_keys(self) -> None:
        self.assertEqual(load_backend_keys(self.write("")), {})

    def test_multiline_secret_is_rejected_without_echoing_it(self) -> None:
        path = self.write('keys:\n  external-prod: "abc\\ndef"\n')
        with self.assertRaises(ValueError) as raised:
            load_backend_keys(path)
        self.assertIn("external-prod", str(raised.exception))
        self.assertNotIn("abc", str(raised.exception))

    def test_unknown_top_level_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_backend_keys(self.write("keys: {}\nextra: true\n"))

    def test_empty_secret_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_backend_keys(self.write("keys:\n  external-prod: ''\n"))


class AccessHeaderTest(unittest.TestCase):
    def test_internal_candidate_sends_no_credentials(self) -> None:
        self.assertEqual(auth_headers(None, {"external-prod": "s3cret"}), {})

    def test_missing_key_sends_no_credentials(self) -> None:
        self.assertEqual(auth_headers(Access(key="external-prod"), {}), {})

    def test_default_header_is_bearer(self) -> None:
        headers = auth_headers(Access(key="external-prod"), {"external-prod": "s3cret"})
        self.assertEqual(headers, {"authorization": "Bearer s3cret"})

    def test_custom_header_and_prefix(self) -> None:
        access = Access(key="external-prod", header="x-api-key", prefix="")
        headers = auth_headers(access, {"external-prod": "s3cret"})
        self.assertEqual(headers, {"x-api-key": "s3cret"})


class ReadinessTest(unittest.TestCase):
    def test_external_candidate_is_not_ready_without_its_key(self) -> None:
        access = BackendAccess(keys={})
        self.assertFalse(access.ready(candidate(access=Access(key="external-prod"))))

    def test_external_candidate_is_ready_with_its_key(self) -> None:
        access = BackendAccess(keys={"external-prod": "s3cret"})
        self.assertTrue(access.ready(candidate(access=Access(key="external-prod"))))

    def test_internal_candidate_is_always_ready(self) -> None:
        self.assertTrue(BackendAccess(keys={}).ready(candidate(backend="http://vllm-gpu:8000")))

    def test_unconfigured_lists_external_candidates_without_keys(self) -> None:
        policy = load_policy(str(REPO / "config" / "policy.yaml"))
        self.assertEqual(BackendAccess(keys={}).unconfigured(policy), [])

    def test_missing_ca_bundle_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                BackendAccess.load(Path(tmp) / "none.yaml", Path(tmp) / "ca.crt")

    def test_file_candidate_is_ready_when_its_token_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "provider-token").write_text("token-value\n", encoding="utf-8")
            access = BackendAccess(keys={}, token_dir=tmp)
            self.assertTrue(
                access.ready(candidate(access=Access(key="provider-token", source="file")))
            )

    def test_file_candidate_is_not_ready_without_a_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A key of the same name in the keys file must not satisfy a file
            # source: the credential is the file, not the entry.
            access = BackendAccess(keys={"provider-token": "s3cret"}, token_dir=tmp)
            self.assertFalse(
                access.ready(candidate(access=Access(key="provider-token", source="file")))
            )

    def test_file_candidate_is_not_ready_when_the_token_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "provider-token").write_text(" \n", encoding="utf-8")
            access = BackendAccess(keys={}, token_dir=tmp)
            self.assertFalse(
                access.ready(candidate(access=Access(key="provider-token", source="file")))
            )

    def test_oidc_readiness_follows_the_keys_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                BackendAccess(keys={}, token_dir=tmp).ready(candidate(access=oidc_access()))
            )
            self.assertTrue(
                BackendAccess(keys={"provider-oidc": "client-secret"}, token_dir=tmp).ready(
                    candidate(access=oidc_access())
                )
            )

    def test_token_directory_falls_back_to_the_environment_then_the_default(self) -> None:
        with mock.patch.dict(os.environ, {"SLM_BACKEND_TOKEN_DIR": "/tmp/tokens"}):
            self.assertEqual(BackendAccess.load(None, None).token_dir, "/tmp/tokens")
            self.assertEqual(BackendAccess.load(None, None, "/tmp/other").token_dir, "/tmp/other")
        with mock.patch.dict(os.environ, {"SLM_BACKEND_TOKEN_DIR": ""}):
            self.assertEqual(BackendAccess.load(None, None).token_dir, "/app/backend-tokens")

    def test_unconfigured_reports_every_unready_source(self) -> None:
        policy_text = """
version: "test-sources"
aliases:
  burst:
    primary: cloud
    candidates:
      - name: cloud
        compute: gpu
        deployment: cloud
        pool: cloud-prod
        backend: https://inference.example.com
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        access:
          key: provider-token
          source: file
      - name: cloud-oidc
        compute: gpu
        deployment: cloud
        pool: cloud-prod
        backend: https://inference.example.com
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        access:
          key: provider-oidc
          source: oidc
          token_url: https://idp.example.com/oauth2/token
          client_id: slm-gateway
      - name: cloud-static
        compute: gpu
        deployment: cloud
        pool: cloud-prod
        backend: https://inference.example.com
        upstream_model: model-cloud
        serves_regions: [us]
        sovereign: false
        access:
          key: external-prod
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "policy.yaml"
            path.write_text(policy_text, encoding="utf-8")
            policy = load_policy(str(path))
            access = BackendAccess(
                keys={"external-prod": "s3cret"}, token_dir=Path(tmp) / "tokens"
            )
            self.assertEqual(access.unconfigured(policy), ["cloud", "cloud-oidc"])


class UpstreamRequestTest(unittest.IsolatedAsyncioTestCase):
    async def test_auth_header_and_model_are_sent_to_the_backend(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await open_upstream(
                client,
                candidate(access=Access(key="external-prod")),
                {"model": "model-cloud", "messages": [], "stream": False},
                stream=False,
                headers=auth_headers(Access(key="external-prod"), {"external-prod": "s3cret"}),
            )
            await response.aread()

        self.assertEqual(seen["url"], "https://inference.example.com/v1/chat/completions")
        self.assertEqual(seen["headers"]["authorization"], "Bearer s3cret")
        self.assertEqual(seen["body"]["model"], "model-cloud")

    async def test_internal_request_carries_no_authorization(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await open_upstream(
                client, candidate(backend="http://vllm-gpu:8000"), {"model": "m"}, False
            )
            await response.aread()

        self.assertNotIn("authorization", seen["headers"])


class ProbeTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_5xx_is_unhealthy(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertFalse(await probe(client, candidate()))

    async def test_a_4xx_still_means_reachable(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(401))
        async with httpx.AsyncClient(transport=transport) as client:
            self.assertTrue(await probe(client, candidate()))

    async def test_a_transport_error_is_unhealthy(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertFalse(await probe(client, candidate()))

    async def test_probe_hits_the_health_path(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["headers"] = dict(request.headers)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await probe(
                client,
                candidate(),
                headers={"authorization": "Bearer s3cret"},
            )
        self.assertEqual(seen["url"], "https://inference.example.com/health")
        self.assertEqual(seen["headers"]["authorization"], "Bearer s3cret")


class FileTokenTest(unittest.IsolatedAsyncioTestCase):
    """A file credential is read on every request, so rotation needs no restart."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.access = BackendAccess(keys={}, token_dir=self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def token_file(self, value: str) -> Path:
        path = self.root / "provider-token"
        path.write_text(value, encoding="utf-8")
        return path

    async def headers(self, client: httpx.AsyncClient, key: str = "provider-token"):
        return await self.access.headers(
            candidate(access=Access(key=key, source="file")), client
        )

    async def test_the_token_file_supplies_the_header(self) -> None:
        self.token_file("token-one\n")
        async with httpx.AsyncClient() as client:
            self.assertEqual(
                await self.headers(client), {"authorization": "Bearer token-one"}
            )

    async def test_a_rotated_token_is_used_without_a_restart(self) -> None:
        self.token_file("token-one\n")
        async with httpx.AsyncClient() as client:
            self.assertEqual(
                await self.headers(client), {"authorization": "Bearer token-one"}
            )
            self.token_file("token-two\n")
            self.assertEqual(
                await self.headers(client), {"authorization": "Bearer token-two"}
            )

    async def test_a_projected_token_newline_is_stripped(self) -> None:
        self.token_file("token-one\r\n")
        async with httpx.AsyncClient() as client:
            self.assertEqual(
                await self.headers(client), {"authorization": "Bearer token-one"}
            )

    async def test_a_missing_or_empty_file_sends_nothing(self) -> None:
        async with httpx.AsyncClient() as client:
            self.assertEqual(await self.headers(client), {})
            self.token_file("\n")
            self.assertEqual(await self.headers(client), {})

    async def test_a_key_outside_the_token_directory_sends_nothing(self) -> None:
        (self.root.parent / "escaped-token").write_text("leak\n", encoding="utf-8")
        async with httpx.AsyncClient() as client:
            self.assertEqual(await self.headers(client, key="../escaped-token"), {})
            self.assertEqual(await self.headers(client, key="."), {})

    async def test_custom_header_and_prefix_apply_to_a_file_token(self) -> None:
        self.token_file("token-one\n")
        access = Access(key="provider-token", source="file", header="x-api-key", prefix="")
        async with httpx.AsyncClient() as client:
            self.assertEqual(
                await self.access.headers(candidate(access=access), client),
                {"x-api-key": "token-one"},
            )


class OidcTokenTest(unittest.IsolatedAsyncioTestCase):
    """The exchange, its cache and its failure modes. No IdP is contacted: the
    transport is mocked and the clock is injected."""

    def setUp(self) -> None:
        self.clock = Clock()
        self.access = BackendAccess(
            keys={"provider-oidc": "client-secret"}, clock=self.clock
        )

    async def test_the_exchange_sends_client_credentials_and_returns_a_bearer(self) -> None:
        idp = Idp(expires_in=300)
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            headers = await self.access.headers(candidate(access=oidc_access()), client)

        self.assertEqual(headers, {"authorization": "Bearer token-1"})
        self.assertEqual(idp.count, 1)
        self.assertEqual(str(idp.requests[0].url), TOKEN_URL)
        self.assertEqual(idp.requests[0].method, "POST")
        self.assertIn(
            "application/x-www-form-urlencoded", idp.requests[0].headers["content-type"]
        )
        self.assertEqual(
            idp.form(),
            {
                "grant_type": "client_credentials",
                "client_id": "slm-gateway",
                "client_secret": "client-secret",
                "scope": "inference.read",
            },
        )

    async def test_scope_is_omitted_when_not_declared(self) -> None:
        idp = Idp()
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            await self.access.headers(candidate(access=oidc_access(scope=None)), client)
        self.assertNotIn("scope", idp.form())

    async def test_a_token_is_cached_until_shortly_before_it_expires(self) -> None:
        idp = Idp(expires_in=300)  # cached for 300 - 60 = 240 seconds
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            first = await self.access.headers(candidate(access=oidc_access()), client)
            self.clock.now = 239
            second = await self.access.headers(candidate(access=oidc_access()), client)
            self.clock.now = 241
            third = await self.access.headers(candidate(access=oidc_access()), client)

        self.assertEqual(first, second)
        self.assertEqual(first, {"authorization": "Bearer token-1"})
        self.assertEqual(third, {"authorization": "Bearer token-2"})
        self.assertEqual(idp.count, 2)

    async def test_a_short_lived_token_is_cached_for_the_five_second_floor(self) -> None:
        idp = Idp(expires_in=20)  # 20 - 60 < 0, so the floor applies
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            header = await self.access.headers(candidate(access=oidc_access()), client)
            self.clock.now = 4
            self.assertEqual(
                await self.access.headers(candidate(access=oidc_access()), client), header
            )
            self.clock.now = 6
            await self.access.headers(candidate(access=oidc_access()), client)
        self.assertEqual(idp.count, 2)

    async def test_a_missing_expires_in_defaults_to_five_minutes(self) -> None:
        idp = Idp()  # no expires_in
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            first = await self.access.headers(candidate(access=oidc_access()), client)
            self.clock.now = 239
            self.assertEqual(
                await self.access.headers(candidate(access=oidc_access()), client), first
            )
            self.clock.now = 241
            await self.access.headers(candidate(access=oidc_access()), client)
        self.assertEqual(idp.count, 2)

    async def test_refresh_drops_the_cached_token(self) -> None:
        idp = Idp()
        cloud = candidate(access=oidc_access())
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            first = await self.access.headers(cloud, client)
            await self.access.refresh(cloud)
            second = await self.access.headers(cloud, client)
        self.assertEqual(first, {"authorization": "Bearer token-1"})
        self.assertEqual(second, {"authorization": "Bearer token-2"})
        self.assertEqual(idp.count, 2)

    async def test_refresh_is_a_no_op_for_other_sources(self) -> None:
        idp = Idp()
        file_candidate = candidate(access=Access(key="provider-token", source="file"))
        static_candidate = candidate(access=Access(key="external-prod"))
        await self.access.refresh(file_candidate)
        await self.access.refresh(static_candidate)
        await self.access.refresh(candidate())
        self.assertEqual(idp.count, 0)

    async def test_an_unconfigured_key_sends_nothing_and_calls_nothing(self) -> None:
        idp = Idp()
        empty = BackendAccess(keys={}, clock=self.clock)
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            self.assertEqual(await empty.headers(candidate(access=oidc_access()), client), {})
        self.assertEqual(idp.count, 0)

    async def test_an_http_error_names_the_key_and_status_only(self) -> None:
        idp = Idp(status=500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            with self.assertRaises(CredentialError) as raised:
                await self.access.headers(candidate(access=oidc_access()), client)
        message = str(raised.exception)
        self.assertIn("provider-oidc", message)
        self.assertIn("HTTP 500", message)
        self.assertNotIn("client-secret", message)
        self.assertNotIn("boom-body", message)
        self.assertNotIn("token-1", message)
        self.assertIsNone(raised.exception.__cause__)

    async def test_a_transport_error_is_a_credential_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(CredentialError) as raised:
                await self.access.headers(candidate(access=oidc_access()), client)
        message = str(raised.exception)
        self.assertIn("provider-oidc", message)
        self.assertNotIn("client-secret", message)
        self.assertIsNone(raised.exception.__cause__)

    async def test_a_response_without_an_access_token_is_rejected(self) -> None:
        idp = Idp(payload=json.dumps({"token_type": "bearer"}).encode())
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            with self.assertRaises(CredentialError) as raised:
                await self.access.headers(candidate(access=oidc_access()), client)
        self.assertIn("access_token", str(raised.exception))

    async def test_a_blank_access_token_is_rejected(self) -> None:
        idp = Idp(payload=json.dumps({"access_token": "  "}).encode())
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            with self.assertRaises(CredentialError) as raised:
                await self.access.headers(candidate(access=oidc_access()), client)
        self.assertIn("access_token", str(raised.exception))

    async def test_a_non_json_response_is_rejected_without_quoting_it(self) -> None:
        idp = Idp(payload=b"<html>not a token</html>")
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            with self.assertRaises(CredentialError) as raised:
                await self.access.headers(candidate(access=oidc_access()), client)
        message = str(raised.exception)
        self.assertIn("not JSON", message)
        self.assertNotIn("html", message)

    async def test_a_cached_token_stays_out_of_the_object_repr(self) -> None:
        idp = Idp()
        async with httpx.AsyncClient(transport=httpx.MockTransport(idp)) as client:
            await self.access.headers(candidate(access=oidc_access()), client)
        self.assertNotIn("token-1", repr(self.access))
        self.assertNotIn("client-secret", repr(self.access))


class OpenAuthenticatedTest(unittest.IsolatedAsyncioTestCase):
    """One 401 refreshes the credential and retries; a second is a failure."""

    async def test_a_401_refreshes_an_oidc_token_and_retries_once(self) -> None:
        idp = Idp()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return idp(request)
            seen.append(request.headers["authorization"])
            if len(seen) == 1:
                return httpx.Response(401, json={"error": "token expired"})
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await open_authenticated(
                client, candidate(access=oidc_access()), self.access(), {"model": "m"}, False
            )
            await response.aread()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, ["Bearer token-1", "Bearer token-2"])
        self.assertEqual(idp.count, 2)

    async def test_a_401_refreshes_a_rotated_file_token_without_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "provider-token"
            token_file.write_text("old-token\n", encoding="utf-8")
            access = BackendAccess(keys={}, token_dir=tmp)
            seen: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request.headers["authorization"])
                if len(seen) == 1:
                    token_file.write_text("new-token\n", encoding="utf-8")
                    return httpx.Response(401, json={"error": "token expired"})
                return httpx.Response(200, json={"ok": True})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                response = await open_authenticated(
                    client,
                    candidate(access=Access(key="provider-token", source="file")),
                    access,
                    {"model": "m"},
                    False,
                )
                await response.aread()

        self.assertEqual(seen, ["Bearer old-token", "Bearer new-token"])
        self.assertEqual(response.status_code, 200)

    async def test_a_static_key_is_not_retried(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["authorization"])
            return httpx.Response(401, json={"error": "invalid key"})

        access = BackendAccess(keys={"external-prod": "s3cret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await open_authenticated(
                client,
                candidate(access=Access(key="external-prod")),
                access,
                {"model": "m"},
                False,
            )
            await response.aread()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(seen, ["Bearer s3cret"])

    async def test_a_second_401_is_returned_not_retried_again(self) -> None:
        idp = Idp()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return idp(request)
            seen.append(request.headers["authorization"])
            return httpx.Response(401, json={"error": "still bad"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = await open_authenticated(
                client, candidate(access=oidc_access()), self.access(), {"model": "m"}, False
            )
            await response.aread()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(len(seen), 2)
        self.assertEqual(idp.count, 2)

    async def test_a_failed_exchange_propagates_and_sends_nothing_upstream(self) -> None:
        idp = Idp(status=503)
        upstream: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return idp(request)
            upstream.append(request)
            return httpx.Response(200, json={"ok": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(CredentialError) as raised:
                await open_authenticated(
                    client, candidate(access=oidc_access()), self.access(), {"model": "m"}, False
                )

        self.assertIn("provider-oidc", str(raised.exception))
        self.assertNotIn("client-secret", str(raised.exception))
        self.assertEqual(upstream, [])

    @staticmethod
    def access() -> BackendAccess:
        return BackendAccess(keys={"provider-oidc": "client-secret"})


class ProbeCredentialTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_fetches_headers_through_the_same_async_path(self) -> None:
        idp = Idp()
        health: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return idp(request)
            health.append(request.headers["authorization"])
            return httpx.Response(200)

        access = BackendAccess(keys={"provider-oidc": "client-secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertTrue(
                await probe(client, candidate(access=oidc_access()), access=access)
            )

        self.assertEqual(health, ["Bearer token-1"])
        self.assertEqual(idp.count, 1)

    async def test_a_failed_exchange_makes_the_probe_down(self) -> None:
        idp = Idp(status=500)
        health: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return idp(request)
            health.append(request)
            return httpx.Response(200)

        access = BackendAccess(keys={"provider-oidc": "client-secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertFalse(
                await probe(client, candidate(access=oidc_access()), access=access)
            )
        self.assertEqual(health, [])

    async def test_an_explicit_header_wins_over_the_access_object(self) -> None:
        idp = Idp()
        health: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == TOKEN_URL:
                return idp(request)
            health.append(request.headers["authorization"])
            return httpx.Response(200)

        access = BackendAccess(keys={"provider-oidc": "client-secret"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertTrue(
                await probe(
                    client,
                    candidate(access=oidc_access()),
                    {"authorization": "Bearer given"},
                    access=access,
                )
            )
        self.assertEqual(health, ["Bearer given"])
        self.assertEqual(idp.count, 0)


@unittest.skipUnless(shutil.which("openssl"), "openssl is required to mint a test certificate")
class ProbeTlsTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cert = root / "cert.pem"
        key = root / "key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cls.cert), "-days", "1",
                "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        handler = type("Handler", (_HealthHandler,), {"status": 200})
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(cls.cert), keyfile=str(key))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"https://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def set_status(self, status: int) -> None:
        self.server.RequestHandlerClass.status = status

    async def test_verified_tls_backend_is_healthy(self) -> None:
        self.set_status(200)
        context = ssl.create_default_context(cafile=str(self.cert))
        async with httpx.AsyncClient(verify=context) as client:
            self.assertTrue(await probe(client, candidate(backend=self.url)))

    async def test_verified_tls_backend_with_a_5xx_is_unhealthy(self) -> None:
        self.set_status(500)
        context = ssl.create_default_context(cafile=str(self.cert))
        async with httpx.AsyncClient(verify=context) as client:
            self.assertFalse(await probe(client, candidate(backend=self.url)))

    async def test_untrusted_certificate_is_unhealthy(self) -> None:
        self.set_status(200)
        async with httpx.AsyncClient(verify=True) as client:
            self.assertFalse(await probe(client, candidate(backend=self.url)))


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        body = json.dumps({"status": "ok", "path": self.path}).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
