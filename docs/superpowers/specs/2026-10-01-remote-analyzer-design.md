# Remote Analyzer (LAN) — Design Spec

Date: 2026-10-01
Status: Approved for planning

## 1. Goal

Nightingale's Rust side (`app-core`, used by both `src-tauri` and `src-server`)
currently spawns the bundled Python analyzer (`app-core/analyzer/*.py`) as a
**local child process** and talks to it over a loopback TCP socket
(`127.0.0.1` only). On a Windows Surface device this analyzer runs on weak
local hardware. The goal is to let the Surface-side Nightingale delegate the
*same* analysis pipeline to a Python analyzer process running on a MacBook
(Apple Silicon, MPS) elsewhere on the LAN, **without forking a new/generic
audio pipeline** — the existing `pipeline.py`/`stems.py`/`transcribe.py`/
`align.py`/`key_detect.py` are reused byte-for-byte.

Switch controlled by `NIGHTINGALE_ANALYZER_MODE=local|remote` (default
`local`), so the existing local path is completely unaffected when unset.

## 2. Non-goals

- No internet exposure. The MacBook daemon binds only to an explicit LAN
  interface IP the operator provides (not `0.0.0.0`, not loopback).
- No TLS in v1 (home LAN threat model; token auth is the only required
  control named by the user). Documented as a known limitation.
- No Surface-side Settings UI toggle in v1 — env var only, matching the
  "まずは切り替え方式" ask. UI can follow later as a separate bounded change.
- No change to `client/src-server`'s HTTP/WS layer — it already shares
  `app-core`, so the mode switch there is free.
- No change to `pipeline.py`, `stems.py`, `transcribe.py`, `align.py`,
  `key_detect.py`, `gpu.py`, `whisper_compat.py`, `analyze.py`.

## 3. Current protocol (reference — see investigation notes in conversation)

- Handshake: child's stdout line `{"event":"ready","port":N,"token":"...","device":"..."}`.
- Auth: `{"type":"hello","token":"..."}` → `{"type":"hello_ack"}` over TCP.
- Command: `{"type":"analyze","audio_path":"<local path>","cache_path":"<local dir>","hash":"...",model/beam_size/batch_size/separator/engine/align_backend/vocal_detection_threshold_pct, optional lyrics/language/skip_transcription/skip_separation}`.
- Events: `{"type":"progress","pct":N,"msg":"..."}`, `{"type":"done","hash":"..."}`, `{"type":"error","kind":"oom"|"generic","msg":"..."}`.
- `{"type":"quit"}` ends the session.
- Output files are named `<cache_path>/<hash>_*` (vocals/instrumental with
  key+tempo suffix, `_transcript.json`, `_lyrics.json`). Rust never needs to
  know exact filenames — it only checks existence/patterns via `CacheDir`.

The one blocking fact: **`audio_path`/`cache_path` are local filesystem
paths**, read/written directly by the Python process. That is the only thing
that doesn't survive a cross-machine hop.

## 4. Architecture (remote mode)

```
Surface (Rust, app-core::analyzer)                MacBook
─────────────────────────────────                 ──────────────────────────
NIGHTINGALE_ANALYZER_MODE=remote
  │
  ├─ TCP control conn ──────────────────────────▶  remote_server.py
  │   hello/hello_ack (shared static token)          (NDJSON control loop,
  │   analyze / progress / done / error               reuses server.py's
  │                                                    process_song()/pipeline)
  │
  └─ HTTP bridge (separate port, bearer token) ──▶  same process, stdlib
      PUT  /sources/<hash>?ext=mp3                   http.server.ThreadingHTTPServer
      PUT  /sources/<hash>/lyrics                    reads/writes
      GET  /results/<hash>/manifest                  <WORKDIR>/sources/*
      GET  /results/<hash>/<file>                     <WORKDIR>/cache/*
      DELETE /work/<hash>
```

Both listeners bind to the **same explicit LAN IP** the operator configures
on the MacBook (e.g. `192.168.11.50`), on two fixed ports. Both require the
same bearer token.

`<WORKDIR>` (default `~/.nightingale/vendor/remote_work`, override via
`NIGHTINGALE_ANALYZER_WORKDIR`) is **pure scratch space**, not a persistent
cache: Rust uploads, triggers analyze, downloads everything produced, then
issues `DELETE /work/<hash>` to wipe it. This sidesteps any need to mirror
Surface's cache-invalidation logic (reanalyze/delete-cache/realign) onto the
remote side — every remote analyze starts from a clean slate, which is
simpler and avoids state-leak bugs, at the cost of never short-circuiting
"already analyzed" remotely (acceptable: that shortcut only ever helped
local repeated calls within one pipeline run, which doesn't happen across
the network boundary anyway).

## 5. Protocol additions

### 5.1 Control channel (NDJSON, same socket semantics as today)

No change to message *shapes* for `progress`/`done`/`error`. Changes:

- `hello` token is **static/configured**, not per-spawn-random. Same field
  name/shape.
- `analyze` gains no new required fields. `audio_path`/`cache_path` sent by
  Rust are **ignored** by `remote_server.py`, which always resolves them to
  `<WORKDIR>/sources/<hash>.<ext>` and `<WORKDIR>/cache` itself. (Rust still
  sends them, populated with the Surface-local values, purely so the JSON
  shape matches `server.py`'s existing `process_song()` for code reuse; the
  remote wrapper overwrites the two fields before calling `run_pipeline`.)
- `quit` means "close this client session," **not** "stop the daemon" — the
  daemon loops back to `accept()` for the next Surface connection/reconnect.
  (Local mode's `server.py` behavior for `quit` is unchanged: it still exits
  the one-shot process, since that path is driven by `shutdown_server()`
  killing the child.)

### 5.2 File bridge (new, plain HTTP, separate port)

All requests require `Authorization: Bearer <token>` (same token as the
control channel). All paths validate `hash` is a bare hex/alnum token (no
`/`, no `..`) before touching the filesystem.

There are three distinct local files `process_song()` may need to hand over,
each with its own endpoint (confirmed against `lyrics.rs`/`pipeline.py`, not
a generic "same file, different name" assumption):

- `PUT /sources/<hash>?ext=<ext>` — body = raw **audio** bytes. Written to
  `<WORKDIR>/sources/<hash>.<ext>`. Always sent. At `analyze` time,
  `remote_server.py` resolves `audio_path` itself by globbing
  `<WORKDIR>/sources/<hash>.*` — Rust's `audio_path` field in the `analyze`
  JSON is sent for shape-compatibility with `server.py` but is otherwise
  ignored.
- `PUT /sources/<hash>/lyrics` — only when `cmd_json["lyrics"]` is set (the
  `fetch_lrclib_lyrics()` align path). Body = raw bytes of
  `cache.lyrics_path(hash)` (i.e. `<hash>_lyrics.json`, which is what that
  function actually produces — see `lyrics.rs::fetch_lrclib_lyrics`).
  Written to `<WORKDIR>/cache/<hash>_lyrics.json`. `remote_server.py`
  rewrites `cmd["lyrics"]` to that path before calling `run_pipeline`
  whenever the field was present, regardless of Rust's literal value.
- `PUT /sources/<hash>/transcript` — whenever the local
  `cache.transcript_path(hash)` file already exists, **regardless of
  `skip_transcription`**. Body = its raw bytes. Written directly to
  `<WORKDIR>/cache/<hash>_transcript.json` — this is not referenced by any
  `analyze` JSON field; `pipeline.py`'s very first check in `run_pipeline`
  is `if transcript_exists and not skip_transcription: ... return` (the
  "already analyzed, skip" short-circuit), evaluated **before** it even
  looks at `skip_transcription` for the stems-only branch. Gating the
  upload on `skip_transcription` would make the remote side see a *missing*
  transcript in cases where local mode would have seen one and treated it
  identically either way (short-circuit for a normal reanalysis-race case,
  or patch-in-place for the stems-only case) — so the upload must mirror
  local's unconditional existence check, not the flag.
- `GET /results/<hash>/manifest` — returns `{"files":["<hash>_vocals_...mp3", ...]}`,
  every file in `<WORKDIR>/cache` whose name starts with `<hash>`.
- `GET /results/<hash>/<file>` — raw bytes of that one file (must start with
  `<hash>`, else 404).
- `DELETE /work/<hash>` — removes every file under `<WORKDIR>/sources/` and
  `<WORKDIR>/cache/` starting with `<hash>`. Called by Rust after a
  successful download, and also as a best-effort cleanup on failure/cancel.

No new Rust crates needed — `ureq`, `tiny_http`-style patterns, and `base64`
are already dependencies. Python side uses only the stdlib
(`http.server`, `secrets`, `pathlib`) — no new pip packages.

## 6. Rust-side changes (`app-core/src/analyzer.rs`)

- New small `enum AnalyzerMode { Local, Remote(RemoteConfig) }` resolved once
  from env (`NIGHTINGALE_ANALYZER_MODE`, `NIGHTINGALE_ANALYZER_HOST`,
  `NIGHTINGALE_ANALYZER_PORT`, `NIGHTINGALE_ANALYZER_HTTP_PORT`,
  `NIGHTINGALE_ANALYZER_TOKEN`). Remote requires host+token; missing token in
  remote mode is a hard startup error (token auth is mandatory, never
  silently falls back to no-auth).
- `ServerProcess.child` becomes `Option<Child>` — `None` in remote mode, so
  `Drop` just shuts down the socket and skips `kill()`/`wait()`.
- `spawn_server()` branches:
  - Local (today's code, unchanged): spawn child, read stdout handshake,
    `connect_and_authenticate(127.0.0.1, ephemeral_port, ephemeral_token)`.
  - Remote (new): no process spawn; directly
    `connect_and_authenticate(configured_host, configured_port, configured_token)`,
    with a clear error if the connection/handshake fails (names the
    host:port so the failure is actionable).
- `process_song()` gains, only under remote mode, a pre-step before
  `send_and_monitor`: upload the local audio file always, the
  `cache.lyrics_path(hash)` file when `cmd_json["lyrics"]` is set, and the
  pre-existing `cache.transcript_path(hash)` file whenever it exists locally
  (independent of `skip_transcription` — see §5.2's three `PUT` cases) — via
  the HTTP bridge;
  and a post-step after `Done`: fetch the manifest, download every listed
  file into the local `CacheDir`, then `DELETE /work/<hash>`. Upload and
  download failures fail the job immediately (`Failed(String)` queue status,
  with `DELETE /work/<hash>` still attempted best-effort on a download
  failure) rather than being folded into the existing crash/OOM
  retry-once loop — that loop exists specifically to recover a crashed
  *control connection*, a different failure mode from a file-transfer error,
  and conflating the two would add retry-safety reasoning (is it safe to
  re-upload a partially-written file? re-download a partially-fetched one?)
  for a problem a manual re-trigger from the UI already solves.
- `run_key_pass()` (LRC key-only pass) gets the same upload/download wrapper
  since it also calls `send_and_monitor`.
- No changes to `cache.rs`, `library_db/*`, `playback.rs`, or any Tauri
  command surface — they only ever see files appear in the local `CacheDir`,
  exactly as today.

## 7. Python-side changes

- **New file** `app-core/analyzer/remote_server.py` (not a modification of
  `server.py`). Imports `pipeline.run_pipeline` exactly like `server.py`
  does — zero duplication of analysis logic, only the transport differs.
  Responsibilities:
  - Parse env config (bind IP, two ports, token, workdir).
  - Run the NDJSON control loop (accept-loop instead of one-shot, static
    token instead of generated, `audio_path`/`cache_path` override).
  - Run the HTTP bridge (`http.server.ThreadingHTTPServer` in a background
    thread) for the endpoints in §5.2.
  - Log to stdout (operator runs it in a terminal / tmux / launchd later).
- **One-line addition** to `app-core/src/vendor_scripts.rs`: add
  `REMOTE_SERVER_PY`/`("remote_server.py", REMOTE_SERVER_PY)` so it gets
  extracted into `vendor/analyzer/` by the existing, unmodified setup flow
  (`step_extract_scripts`). This means provisioning the MacBook is just:
  run Nightingale's normal first-run setup once (or `xtask`/CLI equivalent)
  to get ffmpeg + venv + packages + scripts, then run
  `vendor/venv/bin/python vendor/analyzer/remote_server.py` with env vars.

## 8. Configuration reference

Surface (`app-core`, read at analyzer-start time):

| Env var | Required (remote) | Default | Notes |
|---|---|---|---|
| `NIGHTINGALE_ANALYZER_MODE` | no | `local` | `local` or `remote` |
| `NIGHTINGALE_ANALYZER_HOST` | yes | — | MacBook's LAN IP |
| `NIGHTINGALE_ANALYZER_PORT` | no | `8787` | control port |
| `NIGHTINGALE_ANALYZER_HTTP_PORT` | no | `8788` | file bridge port |
| `NIGHTINGALE_ANALYZER_TOKEN` | yes | — | shared secret |

MacBook (`remote_server.py`):

| Env var | Required | Default | Notes |
|---|---|---|---|
| `NIGHTINGALE_ANALYZER_BIND` | yes | — | explicit LAN interface IP, never `0.0.0.0` |
| `NIGHTINGALE_ANALYZER_PORT` | no | `8787` | must match Surface |
| `NIGHTINGALE_ANALYZER_HTTP_PORT` | no | `8788` | must match Surface |
| `NIGHTINGALE_ANALYZER_TOKEN` | yes | — | must match Surface |
| `NIGHTINGALE_ANALYZER_WORKDIR` | no | `~/.nightingale/vendor/remote_work` | scratch dir |

Token: operator-generated, e.g. `python3 -c "import secrets; print(secrets.token_hex(32))"`.

## 9. Error handling & cancellation

- Connection refused / handshake failure in remote mode → `NightingaleError`
  naming host:port, surfaced through the existing `Failed(String)` queue
  status path (no new UI needed).
- Existing cancel-by-socket-shutdown mechanism (`SERVER_INTERRUPT`) works
  unchanged — shutting down the TCP stream still interrupts the blocking
  `read_line` on Rust's side; the MacBook's in-flight Python computation is
  best-effort-interrupted exactly as it is today for local mode (no new
  limitation introduced).
- OOM detection (`kind:"oom"`) is unaffected — `whisper_compat.is_oom()` and
  MPS are already exercised by the existing bootstrap's `detect_gpu()`
  macOS-arm64 branch.
- Upload/download failures surface as `Failed` immediately (no automatic
  retry — see §6's note on why this is intentionally separate from the
  crash/OOM retry loop). The user can re-trigger analysis from the UI,
  which re-uploads from scratch.

## 10. Testing plan

1. **Unit-level (Rust)**: new tests for `AnalyzerMode` env parsing (missing
   token in remote mode errors; local mode ignores remote env vars).
2. **Manual end-to-end, same-machine loopback first**: run
   `remote_server.py` bound to `127.0.0.1` on the dev Mac, point a local
   Nightingale build at it via the env vars, analyze one short local song,
   confirm vocals/instrumental/transcript land in the local cache and the
   song plays with stems — proves the upload/analyze/download/cleanup cycle
   before involving a second physical machine.
3. **Manual end-to-end, real LAN**: MacBook on its real LAN IP, Surface
   Nightingale pointed at it; analyze one song, confirm `GET /results/.../manifest`
   is empty after completion (cleanup worked) and the queue shows `done`.
4. **Failure-path checks**: wrong token (expect clean auth failure, not a
   hang); MacBook daemon not running (expect a clear "unreachable" failure,
   not a crash); cancel mid-analysis (expect queue entry removed, no orphan
   files left in `<WORKDIR>`).
5. **Regression**: existing local-mode flow (`NIGHTINGALE_ANALYZER_MODE`
   unset) must behave byte-identically to before — covered by not touching
   any of today's local code paths structurally, only adding a branch.

## 11. Operational runbook (delivered once implementation lands)

This section will be filled in with the exact commands for:
- MacBook側: 実行するコマンド (setup once, then `remote_server.py` launch command with env vars)
- Surface側: 実行するコマンド (env vars / launch command for the existing app)
- Nightingale設定: 変更する内容 (none beyond env vars in v1)
- テスト: 確認する内容 (the checklist in §10)

per the user's requested deliverable format, after the implementation plan
is executed.
