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
