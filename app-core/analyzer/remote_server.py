#!/usr/bin/env python3
"""Standalone LAN-accessible analyzer daemon for Nightingale remote mode.

Unlike server.py (spawned as a local child process per Nightingale session,
with a per-spawn ephemeral token, loopback only), this script is a
long-running daemon meant to be started once on a machine with better
hardware (e.g. an Apple Silicon Mac) and reused across many Surface-side
Nightingale sessions over the LAN.

It reuses pipeline.run_pipeline exactly as server.py does -- no change to
the analysis pipeline itself, only the transport around it. See the design
spec (docs/superpowers/specs/2026-10-01-remote-analyzer-design.md) for the
full protocol.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def is_safe_hash(value):
    """True when `value` is a bare 64-char lowercase hex blake3 digest --
    the only shape a real Nightingale file hash ever takes. Rejecting
    anything else is what keeps every filesystem path built from a URL
    segment inside the workdir (no `/`, no `..`, no empty string matches)."""
    return bool(HASH_RE.fullmatch(value or ""))


def is_safe_result_filename(file_hash, name):
    """A result filename must start with the hash, contain no path
    separators, and never reference a parent directory."""
    return (
        bool(name)
        and name.startswith(file_hash)
        and "/" not in name
        and "\\" not in name
        and ".." not in name
    )


class Workdir:
    """Resolves paths under the daemon's scratch directory. Pure path math --
    no filesystem access in the constructor, so it is cheap to construct in
    tests; call `.ensure()` once before using it for real."""

    def __init__(self, root):
        from pathlib import Path

        self.root = Path(root)
        self.sources = self.root / "sources"
        self.cache = self.root / "cache"

    def ensure(self):
        self.sources.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)

    def source_path(self, file_hash, ext):
        return self.sources / f"{file_hash}.{ext}"

    def find_source(self, file_hash):
        """Returns the uploaded source audio path for `file_hash`, whatever
        extension it was uploaded with, or None if nothing was uploaded."""
        matches = sorted(self.sources.glob(f"{file_hash}.*"))
        return matches[0] if matches else None

    def lyrics_path(self, file_hash):
        return self.cache / f"{file_hash}_lyrics.json"

    def transcript_path(self, file_hash):
        return self.cache / f"{file_hash}_transcript.json"

    def result_files(self, file_hash):
        if not self.cache.is_dir():
            return []
        return sorted(
            p.name for p in self.cache.iterdir() if p.name.startswith(file_hash)
        )

    def delete_work(self, file_hash):
        for base in (self.sources, self.cache):
            if not base.is_dir():
                continue
            for p in list(base.iterdir()):
                if p.name.startswith(file_hash):
                    p.unlink()


def handle_analyze(cmd, workdir, device, run_pipeline_fn):
    """Resolve the client's analyze command against the workdir and run the
    pipeline. Split out from the socket-handling loop (Task 7) so it is
    testable with a fake `run_pipeline_fn` -- no torch/whisper import
    required here."""
    file_hash = cmd["hash"]
    audio_path = workdir.find_source(file_hash)
    if audio_path is None:
        raise RuntimeError(f"no uploaded source audio for hash {file_hash}")

    kwargs = dict(
        model_name=cmd.get("model", "large-v3"),
        beam_size=cmd.get("beam_size", 8),
        batch_size=cmd.get("batch_size", 8),
        separator=cmd.get("separator", "karaoke"),
        engine=cmd.get("engine", "whisper"),
        language_override=cmd.get("language"),
        whisper_model=None,
        skip_transcription=bool(cmd.get("skip_transcription", False)),
        skip_separation=bool(cmd.get("skip_separation", False)),
    )
    if cmd.get("lyrics"):
        lyrics_path = workdir.lyrics_path(file_hash)
        if lyrics_path.is_file():
            kwargs["lyrics_path"] = str(lyrics_path)

    run_pipeline_fn(str(audio_path), str(workdir.cache), file_hash, device, **kwargs)


import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def make_http_handler(workdir, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[remote-bridge] {self.address_string()} {fmt % args}", flush=True)

        def _authorized(self):
            return self.headers.get("Authorization") == f"Bearer {token}"

        def _reject(self, code, message):
            body = json.dumps({"error": message}).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload, code=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, data, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_PUT(self):
            if not self._authorized():
                return self._reject(401, "unauthorized")
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                # The connection was dropped mid-upload. `rfile.read(n)`
                # returns whatever arrived before EOF instead of raising, so
                # this check is what stops a truncated file from ever being
                # written to disk.
                return self._reject(400, "incomplete request body")

            if len(parts) == 2 and parts[0] == "sources":
                file_hash = parts[1]
                if not is_safe_hash(file_hash):
                    return self._reject(400, "invalid hash")
                ext = parse_qs(parsed.query).get("ext", ["bin"])[0]
                if not re.fullmatch(r"[A-Za-z0-9]{1,8}", ext):
                    return self._reject(400, "invalid ext")
                workdir.ensure()
                workdir.source_path(file_hash, ext).write_bytes(body)
                return self._send_json({"ok": True})

            if len(parts) == 3 and parts[0] == "sources" and parts[2] == "lyrics":
                file_hash = parts[1]
                if not is_safe_hash(file_hash):
                    return self._reject(400, "invalid hash")
                workdir.ensure()
                workdir.lyrics_path(file_hash).write_bytes(body)
                return self._send_json({"ok": True})

            if len(parts) == 3 and parts[0] == "sources" and parts[2] == "transcript":
                file_hash = parts[1]
                if not is_safe_hash(file_hash):
                    return self._reject(400, "invalid hash")
                workdir.ensure()
                workdir.transcript_path(file_hash).write_bytes(body)
                return self._send_json({"ok": True})

            self._reject(404, "not found")

        def do_GET(self):
            if not self._authorized():
                return self._reject(401, "unauthorized")
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]

            if len(parts) == 3 and parts[0] == "results" and parts[2] == "manifest":
                file_hash = parts[1]
                if not is_safe_hash(file_hash):
                    return self._reject(400, "invalid hash")
                return self._send_json({"files": workdir.result_files(file_hash)})

            if len(parts) == 3 and parts[0] == "results":
                file_hash, name = parts[1], parts[2]
                if not is_safe_hash(file_hash) or not is_safe_result_filename(
                    file_hash, name
                ):
                    return self._reject(400, "invalid request")
                path = workdir.cache / name
                if not path.is_file():
                    return self._reject(404, "not found")
                return self._send_bytes(path.read_bytes())

            self._reject(404, "not found")

        def do_DELETE(self):
            if not self._authorized():
                return self._reject(401, "unauthorized")
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 2 and parts[0] == "work":
                file_hash = parts[1]
                if not is_safe_hash(file_hash):
                    return self._reject(400, "invalid hash")
                workdir.delete_work(file_hash)
                return self._send_json({"ok": True})
            self._reject(404, "not found")

    return Handler


def start_http_bridge(bind, port, workdir, token):
    server = ThreadingHTTPServer((bind, port), make_http_handler(workdir, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
