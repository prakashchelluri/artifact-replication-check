"""A tiny fake Docker Registry v2 built on http.server, run in a background thread."""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeRegistry:
    def __init__(self, repos: dict[str, dict[str, str]], page_size: int = 0,
                 delay: float = 0.0, token: str | None = None):
        self.repos = repos  # repo -> {tag: digest}
        self.page_size = page_size  # 0 = no pagination
        self.delay = delay  # seconds to stall every request (simulates a hung site)
        self.token = token  # if set, require "Bearer <token>" via a /token endpoint
        self.requests: list[str] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        reg = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _route(self):
                reg.requests.append(f"{self.command} {self.path}")
                if reg.delay:
                    time.sleep(reg.delay)
                url = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(url.query)
                if url.path == "/token":
                    return self._send(200, json.dumps({"token": reg.token}).encode())
                if reg.token and self.headers.get("Authorization") != f"Bearer {reg.token}":
                    challenge = (f'Bearer realm="{reg.url}/token",service="fake",'
                                 f'scope="repository:x:pull"')
                    return self._send(401, b"{}", {"WWW-Authenticate": challenge})
                path = url.path.removeprefix("/v2/")
                if path.endswith("/tags/list"):
                    repo = path.removesuffix("/tags/list")
                    if repo not in reg.repos:
                        return self._send(404, b'{"errors":[{"code":"NAME_UNKNOWN"}]}')
                    tags = sorted(reg.repos[repo])
                    headers = {}
                    if reg.page_size:
                        last = query.get("last", [""])[0]
                        tags = [t for t in tags if t > last]
                        if len(tags) > reg.page_size:
                            tags = tags[: reg.page_size]
                            headers["Link"] = (f'</v2/{repo}/tags/list?n={reg.page_size}'
                                               f'&last={tags[-1]}>; rel="next"')
                    body = json.dumps({"name": repo, "tags": tags}).encode()
                    return self._send(200, body, headers)
                if "/manifests/" in path:
                    repo, tag = path.split("/manifests/")
                    digest = reg.repos.get(repo, {}).get(tag)
                    if digest is None:
                        return self._send(404)
                    assert "application/vnd.oci.image.index.v1+json" in self.headers["Accept"]
                    return self._send(200, b"{}", {"Docker-Content-Digest": digest})
                return self._send(404)

            do_GET = do_HEAD = _route

        return Handler


@pytest.fixture
def registry():
    """Factory: registry({...}, page_size=..., delay=..., token=...) -> FakeRegistry."""
    started = []

    def make(repos, **kwargs):
        reg = FakeRegistry(repos, **kwargs)
        started.append(reg)
        return reg

    yield make
    for reg in started:
        reg.close()


def tags(n: int, digest_prefix: str = "sha256:") -> dict[str, str]:
    """{'v1': 'sha256:...1', ..., 'vN': ...}"""
    return {f"v{i}": f"{digest_prefix}{i:064d}" for i in range(1, n + 1)}
