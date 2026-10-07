"""Offline tests (python -m unittest): a fake upstream on localhost stands in for the image API."""
import base64
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gptimg  # noqa: E402


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


class FakeUpstream:
    def __init__(self, width=1024, height=1024):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers), b""))
                self._answer({"object": "list", "data": [{"id": "gpt-image-2"}, {"id": "gpt-6-sol"}, {"id": "nano-banana-pro"}]})

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.requests.append(("POST", self.path, dict(self.headers), body))
                count = json.loads(body).get("n", 1) if self.headers["Content-Type"] == "application/json" else 1
                image = base64.b64encode(png(width, height)).decode()
                self._answer({"created": 0, "data": [{"b64_json": image}] * count})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProxyTests(unittest.TestCase):
    def start(self, upstream, budget=None, token=None, allow=gptimg.IMAGE_MODELS):
        self.ledger_path = os.path.join(tempfile.mkdtemp(), "ledger.jsonl")
        handler = gptimg.make_proxy(gptimg.Upstream("real-key", upstream.url), gptimg.Ledger(self.ledger_path, budget),
                                    allow, token)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(upstream.close)
        return f"http://127.0.0.1:{server.server_address[1]}/v1"

    def post(self, base, body, headers=None):
        request = urllib.request.Request(base + "/images/generations", data=json.dumps(body).encode(), method="POST")
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def ledger(self):
        with open(self.ledger_path) as handle:
            return [json.loads(line) for line in handle]

    def test_forwards_with_the_real_key_and_bills_by_returned_pixels(self):
        upstream = FakeUpstream(3840, 2160)
        base = self.start(upstream)
        status, body = self.post(base, {"model": "gpt-image-2", "prompt": "x", "size": "3840x2160", "n": 2},
                                 {"Authorization": "Bearer anything"})
        self.assertEqual((status, len(body["data"])), (200, 2))
        method, path, headers, _ = upstream.requests[0]
        self.assertEqual((method, path, headers["Authorization"]), ("POST", "/v1/images/generations", "Bearer real-key"))
        self.assertEqual(headers["User-Agent"], gptimg.USER_AGENT)
        (entry,) = self.ledger()
        self.assertEqual((entry["returned"], entry["cost"]), (2, 0.04))
        self.assertNotIn("prompt", entry)

    def test_models_outside_the_allowlist_never_reach_upstream(self):
        upstream = FakeUpstream()
        base = self.start(upstream)
        status, body = self.post(base, {"model": "nano-banana-pro", "prompt": "x"})
        self.assertEqual(status, 403)
        self.assertEqual(upstream.requests, [])

    def test_daily_budget_stops_the_next_request(self):
        upstream = FakeUpstream()
        base = self.start(upstream, budget=0.02)
        self.assertEqual(self.post(base, {"model": "gpt-image-2", "prompt": "x", "size": "1024x1024"})[0], 200)
        status, body = self.post(base, {"model": "gpt-image-2", "prompt": "y", "size": "1024x1024"})
        self.assertEqual(status, 402)
        self.assertIn("budget", body["error"]["message"])
        self.assertEqual(len(upstream.requests), 1)

    def test_token_is_required_when_set(self):
        upstream = FakeUpstream()
        base = self.start(upstream, token="local-secret")
        self.assertEqual(self.post(base, {"model": "gpt-image-2", "prompt": "x"})[0], 401)
        ok = self.post(base, {"model": "gpt-image-2", "prompt": "x"}, {"Authorization": "Bearer local-secret"})
        self.assertEqual(ok[0], 200)

    def test_multipart_edits_pass_through_untouched(self):
        upstream = FakeUpstream()
        base = self.start(upstream)
        folder = tempfile.mkdtemp()
        reference = os.path.join(folder, "ref.png")
        with open(reference, "wb") as handle:
            handle.write(png(64, 64))
        body, kind = gptimg.multipart({"model": "gpt-image-2", "prompt": "keep it", "n": 1}, [("image[]", reference)])
        request = urllib.request.Request(base + "/images/edits", data=body, method="POST")
        request.add_header("Content-Type", kind)
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(upstream.requests[0][3], body)
        self.assertEqual(self.ledger()[0]["model"], "gpt-image-2")


class CliTests(unittest.TestCase):
    def test_draw_writes_numbered_files(self):
        upstream = FakeUpstream()
        self.addCleanup(upstream.close)
        out = os.path.join(tempfile.mkdtemp(), "koi.png")
        env = {"GPT_IMAGE_API_KEY": "k", "GPT_IMAGE_BASE_URL": upstream.url}
        with mock.patch.dict(os.environ, env), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(gptimg.main(["draw", "koi pond, 1:1 composition", "-n", "2", "-o", out]), 0)
        self.assertTrue(os.path.exists(out.replace(".png", "-1.png")))
        sent = json.loads(upstream.requests[0][3])
        self.assertEqual((sent["n"], sent["response_format"]), (2, "b64_json"))

    def test_unknown_size_is_priced_at_the_top_tier(self):
        self.assertEqual(gptimg.size_cost("auto", 2), 0.04)
        self.assertEqual(gptimg.size_cost("2048x1152", 1), 0.015)


if __name__ == "__main__":
    unittest.main()
