#!/usr/bin/env python3
"""gptimg: GPT Image 2 from the terminal, and a local proxy that holds the key for your other tools.

    gptimg draw "A koi pond seen from above, 1:1 composition" -o koi.png
    gptimg edit "Make it night, keep the fish" -i koi.png -o koi-night.png
    gptimg models
    gptimg serve --budget 5          # http://127.0.0.1:8787/v1 for any OpenAI-style image client

Standard library only (Python 3.8+). Upstream: GPT_IMAGE_BASE_URL (default https://argolink.io/v1)
with GPT_IMAGE_API_KEY.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import mimetypes
import os
import re
import struct
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_BASE_URL = "https://argolink.io/v1"
IMAGE_MODELS = ("gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-sunburst")
PRICES = {"1K": 0.015, "2K": 0.015, "4K": 0.02}   # per returned image on the default upstream, 2026-10-07
USER_AGENT = "gptimg/1.0"   # the default "Python-urllib" agent is refused by some CDNs (HTTP 403)


def tier(longest_edge: int) -> str:
    return "1K" if longest_edge <= 1024 else "2K" if longest_edge <= 2048 else "4K"


def size_cost(size: str, count: int) -> float:
    try:
        return PRICES[tier(max(int(v) for v in size.lower().split("x")))] * count
    except (ValueError, AttributeError):
        return PRICES["4K"] * count      # unknown or "auto" size: assume the dearest tier


def png_or_jpeg_edge(data: bytes) -> int | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return max(struct.unpack(">II", data[16:24]))
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data) and data[i] == 0xFF:
            marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return max(struct.unpack(">HH", data[i + 5:i + 9]))
            i += 2 + length
    return None


# ----------------------------------------------------------------------------------------- client

class Upstream:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or os.environ.get("GPT_IMAGE_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("GPT_IMAGE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

    def send(self, method: str, path: str, body: bytes | None, content_type: str | None) -> tuple:
        """Returns (status, content type, body bytes); HTTP errors are returned, not raised."""
        request = urllib.request.Request(self.base_url + path, data=body, method=method)
        request.add_header("User-Agent", USER_AGENT)
        if self.api_key:
            request.add_header("Authorization", f"Bearer {self.api_key}")
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                return response.status, response.headers.get("Content-Type", "application/json"), response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", "application/json"), error.read()


def multipart(fields: dict, files: list) -> tuple:
    boundary = f"gptimg-{uuid.uuid4().hex}"
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, path in files:
        with open(path, "rb") as handle:
            data = handle.read()
        kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                      f'filename="{os.path.basename(path)}"\r\nContent-Type: {kind}\r\n\r\n'.encode() + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def save_images(payload: bytes, output: str) -> list:
    items = json.loads(payload)["data"]
    stem, extension = os.path.splitext(output)
    paths = []
    for index, item in enumerate(items, 1):
        path = output if len(items) == 1 else f"{stem}-{index}{extension or '.png'}"
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(item["b64_json"]))
        paths.append(path)
    return paths


# ------------------------------------------------------------------------------------------ proxy

class Ledger:
    """Daily spend and a JSONL log; thread-safe because the proxy serves requests in threads."""

    def __init__(self, path: str, budget: float | None, log_prompts: bool = False):
        self.path, self.budget, self.log_prompts = path, budget, log_prompts
        self.lock = threading.Lock()

    def spent_today(self) -> float:
        today, total = dt.date.today().isoformat(), 0.0
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    entry = json.loads(line)
                    if entry["time"].startswith(today):
                        total += entry.get("cost", 0)
        return total

    def record(self, entry: dict) -> None:
        with self.lock, open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


def request_summary(body: bytes, content_type: str) -> dict:
    """model, size, n and prompt of a generations/edits body, JSON or multipart."""
    if content_type.startswith("application/json"):
        data = json.loads(body or b"{}")
        return {k: data.get(k) for k in ("model", "size", "n", "prompt")}
    fields = {}
    for name in ("model", "size", "n", "prompt"):
        match = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n(.*?)\r\n--', body, re.S)
        fields[name] = match.group(1).decode(errors="replace") if match else None
    return fields


def make_proxy(upstream: Upstream, ledger: Ledger, allowed_models: tuple, token: str | None = None):
    class Proxy(BaseHTTPRequestHandler):
        server_version = "gptimg-proxy"

        def log_message(self, *args):        # the ledger is the log
            pass

        def reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def refuse(self, status: int, message: str) -> None:
            self.reply(status, json.dumps({"error": {"type": "proxy_error", "message": message}}).encode())

        def authorized(self) -> bool:
            return not token or self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_GET(self):
            if not self.authorized():
                return self.refuse(401, "wrong or missing proxy token")
            if self.path.rstrip("/") != "/v1/models":
                return self.refuse(404, "this proxy serves /v1/models, /v1/images/generations and /v1/images/edits")
            status, kind, body = upstream.send("GET", "/models", None, None)
            self.reply(status, body, kind)

        def do_POST(self):
            if not self.authorized():
                return self.refuse(401, "wrong or missing proxy token")
            path = self.path.rstrip("/")
            if path not in ("/v1/images/generations", "/v1/images/edits"):
                return self.refuse(404, "only /v1/images/generations and /v1/images/edits are forwarded")
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            kind = self.headers.get("Content-Type", "application/json")
            summary = request_summary(body, kind)
            if summary["model"] not in allowed_models:
                return self.refuse(403, f"model {summary['model']!r} is not allowed here; allowed: {', '.join(allowed_models)}")
            count = int(summary["n"] or 1)
            estimate = size_cost(summary["size"] or "1024x1024", count)
            if ledger.budget is not None and ledger.spent_today() + estimate > ledger.budget:
                return self.refuse(402, f"daily budget ${ledger.budget:.2f} would be exceeded "
                                        f"(spent ${ledger.spent_today():.3f}, this request up to ${estimate:.3f})")
            status, reply_kind, reply = upstream.send("POST", path[len("/v1"):], body, kind)
            cost, returned = 0.0, 0
            if status == 200:
                for item in json.loads(reply).get("data", []):
                    returned += 1
                    edge = png_or_jpeg_edge(base64.b64decode(item["b64_json"])) if item.get("b64_json") else None
                    cost += PRICES[tier(edge)] if edge else size_cost(summary["size"] or "1024x1024", 1)
            entry = {"time": dt.datetime.now().isoformat(timespec="seconds"), "endpoint": path,
                     "model": summary["model"], "size": summary["size"], "n": count, "status": status,
                     "returned": returned, "cost": round(cost, 4)}
            if ledger.log_prompts:
                entry["prompt"] = summary["prompt"]
            ledger.record(entry)
            self.reply(status, reply, reply_kind)

    return Proxy


# -------------------------------------------------------------------------------------------- CLI

def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gptimg", description="GPT Image 2 CLI and local image proxy.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("draw", "edit"):
        p = sub.add_parser(name, help="text to image" if name == "draw" else "edit with reference images")
        p.add_argument("prompt")
        p.add_argument("-o", "--output", default="image.png")
        p.add_argument("--model", default="gpt-image-2", choices=IMAGE_MODELS)
        p.add_argument("--size", default="1024x1024", help="pixels; say the ratio in the prompt too")
        p.add_argument("--quality", default="auto", choices=("auto", "low", "medium", "high"))
        p.add_argument("-n", type=int, default=1, choices=range(1, 8), metavar="1-7")
        if name == "edit":
            p.add_argument("-i", "--image", action="append", required=True, help="reference file, up to 16")
            p.add_argument("--mask", help="PNG with alpha, applied to the first image")
    sub.add_parser("models", help="list the upstream's image models")
    p = sub.add_parser("serve", help="run the local proxy")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--budget", type=float, help="USD per day; requests beyond it get 402")
    p.add_argument("--allow", default=",".join(IMAGE_MODELS), help="comma-separated model IDs to forward")
    p.add_argument("--token", help="require 'Authorization: Bearer <token>' from local clients")
    p.add_argument("--ledger", default="gptimg-ledger.jsonl")
    p.add_argument("--log-prompts", action="store_true", help="also write prompts to the ledger")
    args = parser.parse_args(argv)

    upstream = Upstream()
    if args.command == "models":
        status, _, body = upstream.send("GET", "/models", None, None)
        if status != 200:
            sys.exit(f"GET /models returned {status}")
        for model in json.loads(body)["data"]:
            if "image" in model["id"] or "banana" in model["id"]:
                print(model["id"])
        return 0
    if args.command == "serve":
        if not upstream.api_key:
            sys.exit("set GPT_IMAGE_API_KEY: the proxy adds it to every forwarded request")
        if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
            sys.exit("refusing to listen beyond loopback without --token")
        handler = make_proxy(upstream, Ledger(args.ledger, args.budget, args.log_prompts),
                             tuple(m.strip() for m in args.allow.split(",") if m.strip()), args.token)
        server = ThreadingHTTPServer((args.host, args.port), handler)
        print(f"proxy on http://{args.host}:{args.port}/v1 -> {upstream.base_url}; ledger {args.ledger}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if not upstream.api_key:
        sys.exit("set GPT_IMAGE_API_KEY")
    fields = {"model": args.model, "prompt": args.prompt, "size": args.size, "quality": args.quality,
              "n": args.n, "response_format": "b64_json"}
    if args.command == "draw":
        status, _, body = upstream.send("POST", "/images/generations", json.dumps(fields).encode(), "application/json")
    else:
        if len(args.image) > 16:
            sys.exit("an edit takes at most 16 reference images")
        files = [("image[]", path) for path in args.image] + ([("mask", args.mask)] if args.mask else [])
        data, kind = multipart(fields, files)
        status, _, body = upstream.send("POST", "/images/edits", data, kind)
    if status != 200:
        sys.exit(f"upstream returned {status}: {body.decode(errors='replace')[:400]}")
    paths = save_images(body, args.output)
    print("\n".join(paths))
    print(f"about ${size_cost(args.size, len(paths)):.3f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
