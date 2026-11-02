#!/usr/bin/env python3
"""Real local HTTP and filesystem qualification of central tool preparation."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import platform
import tarfile
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("prepare", Path(__file__).with_name("prepare-cache-tools.py"))
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class CentralToolPreparation(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.root = Path(self.work.name)
        self.original_lock = prepare.LOCK
        data = b"qualified fixture executable bytes"
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            entry = tarfile.TarInfo("bin/uv")
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        self.archive = stream.getvalue()
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.route = "/github-releases/sha256/" + self.digest + "/example/tool/releases/download/v1/tool.tar.gz"
        selected = {"uv": {"url": "https://github.com/example/tool/releases/download/v1/tool.tar.gz",
                           "sha256": self.digest, "members": {"uv": "bin/uv"},
                           "binary_sha256": {"uv": hashlib.sha256(data).hexdigest()}}}
        system = {"Linux": "linux", "Darwin": "darwin"}[platform.system()]
        machine = {"x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine()]
        self.lock = {"schema": 1, "versions": {"uv": "fixture"},
                     "platforms": {system + "-" + machine: selected}}
        prepare.LOCK = self.root / "lock.json"
        self.status = 200
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append(self.path)
                status = owner.status if self.path == owner.route else 404
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "https://github.com/example/tool/releases/download/v1/tool.tar.gz")
                body = owner.archive if status == 200 else b""
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        prepare.LOCK = self.original_lock
        self.work.cleanup()

    def run_prepare(self):
        prepare.LOCK.write_text(json.dumps(self.lock))
        return prepare.prepare(self.root / "tools", self.base)

    def test_central_fill_and_existing_verified_install(self):
        receipt = self.run_prepare()
        self.assertEqual(self.requests, [self.route])
        self.assertEqual(receipt, prepare.prepare(self.root / "tools", self.base))
        self.assertEqual(self.requests, [self.route])
        self.assertEqual((self.root / "tools/bin/uv").read_bytes(), b"qualified fixture executable bytes")

    def test_digest_failure_leaves_no_install(self):
        self.archive += b"corruption"
        with self.assertRaisesRegex(ValueError, "digest"):
            self.run_prepare()
        self.assertFalse((self.root / "tools").exists())

    def test_central_failure_is_not_retried_directly(self):
        self.status = 503
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.run_prepare()
        self.assertEqual(result.exception.code, 503)
        result.exception.close()
        self.assertEqual(self.requests, [self.route])
        self.assertFalse((self.root / "tools").exists())

    def test_redirect_cannot_escape_central_listener(self):
        self.status = 302
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.run_prepare()
        self.assertEqual(result.exception.code, 302)
        result.exception.close()
        self.assertEqual(self.requests, [self.route])
        self.assertFalse((self.root / "tools").exists())

    def test_selected_routes_and_base_boundaries(self):
        self.assertEqual(prepare.download_url("https://static.rust-lang.org/dist/tool.tar.xz", self.digest, self.base),
                         self.base + "/rustup/dist/tool.tar.xz")
        original = "https://github.com/example/tool/releases/download/v1/tool.tar.gz"
        self.assertEqual(prepare.download_url(original, self.digest, None), original)
        for invalid in ["https://127.0.0.1:8740", "http://localhost:8740", "http://127.0.0.1",
                        "http://127.0.0.1:8740/path", "http://user@127.0.0.1:8740", "http://127.0.0.1:8740?x=1"]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                prepare.central_base(invalid)
        with self.assertRaisesRegex(ValueError, "no admitted"):
            prepare.download_url("https://example.invalid/tool.tar.gz", self.digest, self.base)


if __name__ == "__main__":
    unittest.main()
