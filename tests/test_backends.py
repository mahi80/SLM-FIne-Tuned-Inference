"""Tests for external backend credentials, TLS and health probing.

The TLS tests run a throwaway HTTPS server with a self-signed certificate for
127.0.0.1 and skip when `openssl` is unavailable; everything else needs only
httpx and PyYAML, matching the CI test environment.
"""

from __future__ import annotations

import http.server
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from gateway.backends import (
    BackendAccess,
    auth_headers,
    load_backend_keys,
    open_upstream,
    probe,
)
from router import load_policy
from router.policy import Access, Candidate

REPO = Path(__file__).parents[1]


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
