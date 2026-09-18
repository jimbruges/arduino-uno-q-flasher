"""Disk-backed HTTP cache for UNO Q package repositories."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from urllib.parse import unquote, urlsplit

CACHE_PORT = 3142
ALLOWED_UPSTREAMS = frozenset(
    {
        ("http", "deb.debian.org"),
        ("https", "apt-repo.arduino.cc"),
    }
)
VOLATILE_NAMES = frozenset({"InRelease", "Release", "Release.gpg"})
VOLATILE_TTL_SECONDS = 300


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stale_hits: int = 0
    bytes_from_cache: int = 0
    bytes_from_upstream: int = 0
    errors: int = 0


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class PackageCache:
    def __init__(self, cache_dir: Path, port: int = CACHE_PORT) -> None:
        self.cache_dir = cache_dir
        self.port = port
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stats = CacheStats()
        self._stats_lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._key_locks_lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        cache = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self) -> None:
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:  # noqa: N802
                cache._handle(self, send_body=True)

            def do_HEAD(self) -> None:  # noqa: N802
                cache._handle(self, send_body=False)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = LocalThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="unoq-package-cache",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def status(self) -> dict:
        with self._stats_lock:
            stats = asdict(self._stats)
        files = 0
        size = 0
        for path in self.cache_dir.glob("*.body"):
            try:
                files += 1
                size += path.stat().st_size
            except OSError:
                pass
        return {
            "running": self._server is not None,
            "port": self.port,
            "cache_dir": str(self.cache_dir),
            "files": files,
            "size_bytes": size,
            **stats,
        }

    def cached_urls(self) -> set[str]:
        urls: set[str] = set()
        for path in self.cache_dir.glob("*.json"):
            metadata = self._read_metadata(path)
            url = metadata.get("url")
            if isinstance(url, str):
                urls.add(url)
        return urls

    def _handle(self, request: BaseHTTPRequestHandler, *, send_body: bool) -> None:
        if urlsplit(request.path).path == "/health":
            body = b'{"ok":true}'
            request.send_response(200)
            request.send_header("Content-Type", "application/json")
            request.send_header("Content-Length", str(len(body)))
            request.send_header("Connection", "close")
            request.end_headers()
            if send_body:
                request.wfile.write(body)
            return
        try:
            upstream = self._upstream_url(request.path)
        except ValueError as exc:
            request.send_error(400, str(exc))
            return

        cache_key = hashlib.sha256(upstream.encode()).hexdigest()
        body_path = self.cache_dir / f"{cache_key}.body"
        metadata_path = self.cache_dir / f"{cache_key}.json"
        lock = self._lock_for(cache_key)
        with lock:
            try:
                source, status, headers = self._resolve(
                    upstream, body_path, metadata_path
                )
            except Exception as exc:  # noqa: BLE001
                self._increment("errors")
                request.send_error(502, f"upstream fetch failed: {exc}")
                return

        size = body_path.stat().st_size
        request.send_response(status)
        request.send_header("Content-Length", str(size))
        request.send_header("Content-Type", headers.get("Content-Type", "application/octet-stream"))
        request.send_header("X-UNOQ-Cache", source)
        request.send_header("Connection", "close")
        request.end_headers()
        if send_body:
            try:
                with body_path.open("rb") as cached:
                    while chunk := cached.read(1024 * 1024):
                        request.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _resolve(
        self, upstream: str, body_path: Path, metadata_path: Path
    ) -> tuple[str, int, dict[str, str]]:
        metadata = self._read_metadata(metadata_path)
        fresh = body_path.is_file() and not self._is_stale(upstream, metadata)
        if fresh:
            self._record_cache_hit(body_path.stat().st_size)
            return "HIT", int(metadata.get("status", 200)), metadata.get("headers", {})

        temp_path = body_path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        request = urllib.request.Request(upstream, headers={"User-Agent": "unoq-package-cache/1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                headers = {
                    "Content-Type": response.headers.get(
                        "Content-Type", "application/octet-stream"
                    )
                }
                with temp_path.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                temp_path.replace(body_path)
                metadata = {
                    "url": upstream,
                    "status": response.status,
                    "fetched_at": time.time(),
                    "headers": headers,
                }
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                size = body_path.stat().st_size
                self._increment("misses")
                self._increment("bytes_from_upstream", size)
                return "MISS", response.status, headers
        except (OSError, urllib.error.URLError):
            temp_path.unlink(missing_ok=True)
            if body_path.is_file() and metadata:
                size = body_path.stat().st_size
                self._increment("stale_hits")
                self._increment("bytes_from_cache", size)
                return "STALE", int(metadata.get("status", 200)), metadata.get("headers", {})
            raise

    def _upstream_url(self, raw_path: str) -> str:
        path = unquote(urlsplit(raw_path).path)
        parts = path.split("/", 4)
        if len(parts) < 5 or parts[1] != "repository":
            raise ValueError("expected /repository/{http|https}/{host}/path")
        _, _, scheme, host, remainder = parts
        if (scheme, host) not in ALLOWED_UPSTREAMS:
            raise ValueError("repository is not allowlisted")
        return f"{scheme}://{host}/{remainder}"

    def _is_stale(self, upstream: str, metadata: dict) -> bool:
        path = urlsplit(upstream).path
        name = path.rsplit("/", 1)[-1]
        mutable_index = "/dists/" in path and "/by-hash/" not in path
        if name not in VOLATILE_NAMES and not mutable_index:
            return False
        return time.time() - float(metadata.get("fetched_at", 0)) > VOLATILE_TTL_SECONDS

    def _read_metadata(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _lock_for(self, key: str) -> threading.Lock:
        with self._key_locks_lock:
            return self._key_locks.setdefault(key, threading.Lock())

    def _record_cache_hit(self, size: int) -> None:
        self._increment("hits")
        self._increment("bytes_from_cache", size)

    def _increment(self, field: str, value: int = 1) -> None:
        with self._stats_lock:
            setattr(self._stats, field, getattr(self._stats, field) + value)