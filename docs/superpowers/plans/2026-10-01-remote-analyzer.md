# Remote Analyzer (LAN) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Nightingale's Rust side (`app-core`) delegate audio analysis to a Python analyzer daemon running on another LAN machine (e.g. a MacBook) instead of always spawning one locally, switchable via `NIGHTINGALE_ANALYZER_MODE=local|remote`, with zero changes to the analysis pipeline itself.

**Architecture:** A new Rust submodule (`app-core/src/analyzer/remote.rs`) resolves remote config from env vars and talks to a new standalone Python daemon (`app-core/analyzer/remote_server.py`) over the existing NDJSON control protocol plus a small token-authenticated HTTP file bridge for transferring the audio file in and the result stems/transcript back out. The MacBook side is pure scratch space, wiped after every job via `DELETE /work/<hash>`.

**Tech Stack:** Rust (`ureq` for HTTP, `std::net::TcpStream` for the control socket — no new crates), Python 3.10 stdlib only (`http.server`, `socket`, `json` — no new pip packages), reusing `pipeline.run_pipeline` unchanged.

**Spec:** `docs/superpowers/specs/2026-10-01-remote-analyzer-design.md`

## Global Constraints

- No new Rust crates. `ureq` (3.3.0, `json` feature already enabled), `tiny_http`, `base64` are already in `app-core/Cargo.toml`.
- No new Python packages. `remote_server.py` uses only the stdlib plus the existing analyzer modules (`pipeline`, `gpu`, `whisper_compat`).
- No changes to `pipeline.py`, `stems.py`, `transcribe.py`, `align.py`, `key_detect.py`, `gpu.py`, `whisper_compat.py`, `analyze.py`, or `server.py`.
- `NIGHTINGALE_ANALYZER_MODE` unset (or `local`) must behave byte-identically to today — every local-mode code path stays untouched, only new branches are added.
- Token auth is mandatory in remote mode: missing `NIGHTINGALE_ANALYZER_TOKEN` (Rust) or `NIGHTINGALE_ANALYZER_TOKEN` (Python) is a hard startup error, never a silent no-auth fallback.
- The MacBook daemon binds only to an explicit interface IP from `NIGHTINGALE_ANALYZER_BIND` — never defaults to `0.0.0.0` or `127.0.0.1`; a missing value is a startup error.
- Default control port `8787`, default HTTP bridge port `8788` (both overridable, must match on both sides).
- Default remote workdir: `~/.nightingale/vendor/remote_work` (override via `NIGHTINGALE_ANALYZER_WORKDIR`).
- Hash validation: a Nightingale file hash is always exactly 64 lowercase hex characters (blake3 digest). Anything else is rejected before touching a filesystem path built from it.

## Review Focus

- **Wrong/missing/mismatched token** on the control hello or any HTTP bridge call must fail cleanly (closed connection / `401`) and must never be silently treated as authenticated.
- **Path traversal via a crafted hash or filename** (`../`, embedded `/`, absolute paths) in any URL segment must be rejected before it is joined into a filesystem path, on both the Python daemon (serving) and the Rust client (trusting a manifest the daemon returned).
- **Unreachable remote host** (daemon not started, firewalled, wrong IP) must fail within a bounded time with an actionable error naming host:port — not hang the analysis queue indefinitely (ureq has no default timeout; this must be configured explicitly).
- **Cancel mid-remote-analysis** must still clean up the remote scratch directory (`DELETE /work/<hash>`) so repeated cancels don't accumulate orphaned files on the Mac.
- **A local transcript file that exists for reasons unrelated to `skip_transcription`** (e.g. a reanalysis race) must be uploaded whenever present, not only when `skip_transcription` is set — otherwise remote mode silently diverges from `pipeline.py`'s actual short-circuit check order (see the spec's §5.2 note).

---

### Task 1: Restructure `analyzer.rs` into `analyzer/mod.rs`

Pure mechanical move — no behavior change — to make room for the new `analyzer::remote` submodule. No test framework applies to a content-free move; verification is a successful build.

**Files:**
- Move: `app-core/src/analyzer.rs` → `app-core/src/analyzer/mod.rs`

**Interfaces:**
- Consumes: nothing new.
- Produces: `app-core/src/analyzer/` directory module, ready to hold `remote.rs`. `lib.rs`'s existing `mod analyzer;` line needs no change — Rust resolves a directory module via `mod.rs` automatically.

- [ ] **Step 1: Move the file**

```bash
mkdir -p app-core/src/analyzer
git mv app-core/src/analyzer.rs app-core/src/analyzer/mod.rs
```

- [ ] **Step 2: Verify the build is unaffected**

Run: `cargo check -p app-core`
Expected: compiles with no errors (identical to before the move).

- [ ] **Step 3: Commit**

```bash
git add app-core/src/analyzer
git commit -m "refactor: convert analyzer.rs into analyzer/mod.rs

Pure file move, no behavior change. Makes room for a new
analyzer::remote submodule."
```

---

### Task 2: `AnalyzerMode`/`RemoteConfig` env resolution

**Files:**
- Create: `app-core/src/analyzer/remote.rs`
- Modify: `app-core/src/analyzer/mod.rs` (add `mod remote;` near the top, alongside the existing `use` statements)

**Interfaces:**
- Consumes: `crate::error::NightingaleError` (existing, has `Other(String)` variant and `impl From<std::io::Error>`).
- Produces (all `pub(crate)`, used by later tasks):
  - `const DEFAULT_CONTROL_PORT: u16 = 8787;`
  - `const DEFAULT_HTTP_PORT: u16 = 8788;`
  - `struct RemoteConfig { host: String, port: u16, http_port: u16, token: String }` (derives `Debug, Clone, PartialEq, Eq`)
  - `enum AnalyzerMode { Local, Remote(RemoteConfig) }` (derives `Debug, Clone, PartialEq, Eq`)
  - `fn resolve_mode() -> Result<AnalyzerMode, NightingaleError>`

- [ ] **Step 1: Write the failing tests**

Create `app-core/src/analyzer/remote.rs` with just the test module and the function signatures it needs (bodies will be `todo!()` so the tests fail on behavior, not on missing symbols):

```rust
use crate::error::NightingaleError;

pub(crate) const DEFAULT_CONTROL_PORT: u16 = 8787;
pub(crate) const DEFAULT_HTTP_PORT: u16 = 8788;

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RemoteConfig {
    pub host: String,
    pub port: u16,
    pub http_port: u16,
    pub token: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AnalyzerMode {
    Local,
    Remote(RemoteConfig),
}

pub(crate) fn resolve_mode_from(
    _get: impl Fn(&str) -> Option<String>,
) -> Result<AnalyzerMode, NightingaleError> {
    todo!()
}

pub(crate) fn resolve_mode() -> Result<AnalyzerMode, NightingaleError> {
    resolve_mode_from(|key| std::env::var(key).ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |key: &str| map.get(key).cloned()
    }

    #[test]
    fn defaults_to_local_when_unset() {
        let mode = resolve_mode_from(env(&[])).unwrap();
        assert_eq!(mode, AnalyzerMode::Local);
    }

    #[test]
    fn remote_requires_host() {
        let result = resolve_mode_from(env(&[
            ("NIGHTINGALE_ANALYZER_MODE", "remote"),
            ("NIGHTINGALE_ANALYZER_TOKEN", "secret"),
        ]));
        assert!(result.is_err());
    }

    #[test]
    fn remote_requires_token() {
        let result = resolve_mode_from(env(&[
            ("NIGHTINGALE_ANALYZER_MODE", "remote"),
            ("NIGHTINGALE_ANALYZER_HOST", "192.168.11.50"),
        ]));
        assert!(result.is_err());
    }

    #[test]
    fn remote_rejects_empty_host_or_token() {
        let result = resolve_mode_from(env(&[
            ("NIGHTINGALE_ANALYZER_MODE", "remote"),
            ("NIGHTINGALE_ANALYZER_HOST", "   "),
            ("NIGHTINGALE_ANALYZER_TOKEN", "secret"),
        ]));
        assert!(result.is_err());
    }

    #[test]
    fn remote_uses_default_ports() {
        let mode = resolve_mode_from(env(&[
            ("NIGHTINGALE_ANALYZER_MODE", "remote"),
            ("NIGHTINGALE_ANALYZER_HOST", "192.168.11.50"),
            ("NIGHTINGALE_ANALYZER_TOKEN", "secret"),
        ]))
        .unwrap();
        assert_eq!(
            mode,
            AnalyzerMode::Remote(RemoteConfig {
                host: "192.168.11.50".to_string(),
                port: DEFAULT_CONTROL_PORT,
                http_port: DEFAULT_HTTP_PORT,
                token: "secret".to_string(),
            })
        );
    }

    #[test]
    fn remote_uses_custom_ports() {
        let mode = resolve_mode_from(env(&[
            ("NIGHTINGALE_ANALYZER_MODE", "remote"),
            ("NIGHTINGALE_ANALYZER_HOST", "192.168.11.50"),
            ("NIGHTINGALE_ANALYZER_TOKEN", "secret"),
            ("NIGHTINGALE_ANALYZER_PORT", "9001"),
            ("NIGHTINGALE_ANALYZER_HTTP_PORT", "9002"),
        ]))
        .unwrap();
        assert_eq!(
            mode,
            AnalyzerMode::Remote(RemoteConfig {
                host: "192.168.11.50".to_string(),
                port: 9001,
                http_port: 9002,
                token: "secret".to_string(),
            })
        );
    }

    #[test]
    fn unknown_mode_errors() {
        let result = resolve_mode_from(env(&[("NIGHTINGALE_ANALYZER_MODE", "bogus")]));
        assert!(result.is_err());
    }

    #[test]
    fn invalid_port_errors() {
        let result = resolve_mode_from(env(&[
            ("NIGHTINGALE_ANALYZER_MODE", "remote"),
            ("NIGHTINGALE_ANALYZER_HOST", "192.168.11.50"),
            ("NIGHTINGALE_ANALYZER_TOKEN", "secret"),
            ("NIGHTINGALE_ANALYZER_PORT", "not-a-number"),
        ]));
        assert!(result.is_err());
    }
}
```

Add `mod remote;` to the top of `app-core/src/analyzer/mod.rs`, right after the existing `use` block (e.g. after the `use crate::vendor::{...}` line).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cargo test -p app-core analyzer::remote:: -- --nocapture`
Expected: panics with `not yet implemented` (from the `todo!()`), not a compile error.

- [ ] **Step 3: Implement `resolve_mode_from`**

Replace the `todo!()` body:

```rust
pub(crate) fn resolve_mode_from(
    get: impl Fn(&str) -> Option<String>,
) -> Result<AnalyzerMode, NightingaleError> {
    let mode = get("NIGHTINGALE_ANALYZER_MODE").unwrap_or_else(|| "local".to_string());
    match mode.as_str() {
        "local" => Ok(AnalyzerMode::Local),
        "remote" => {
            let host = get("NIGHTINGALE_ANALYZER_HOST").ok_or_else(|| {
                NightingaleError::Other(
                    "NIGHTINGALE_ANALYZER_MODE=remote requires NIGHTINGALE_ANALYZER_HOST".into(),
                )
            })?;
            let token = get("NIGHTINGALE_ANALYZER_TOKEN").ok_or_else(|| {
                NightingaleError::Other(
                    "NIGHTINGALE_ANALYZER_MODE=remote requires NIGHTINGALE_ANALYZER_TOKEN".into(),
                )
            })?;
            if host.trim().is_empty() || token.trim().is_empty() {
                return Err(NightingaleError::Other(
                    "NIGHTINGALE_ANALYZER_HOST and NIGHTINGALE_ANALYZER_TOKEN cannot be empty"
                        .into(),
                ));
            }
            let port = parse_port(get("NIGHTINGALE_ANALYZER_PORT"), DEFAULT_CONTROL_PORT)?;
            let http_port = parse_port(get("NIGHTINGALE_ANALYZER_HTTP_PORT"), DEFAULT_HTTP_PORT)?;
            Ok(AnalyzerMode::Remote(RemoteConfig {
                host,
                port,
                http_port,
                token,
            }))
        }
        other => Err(NightingaleError::Other(format!(
            "Unknown NIGHTINGALE_ANALYZER_MODE={other:?}; expected \"local\" or \"remote\""
        ))),
    }
}

fn parse_port(raw: Option<String>, default: u16) -> Result<u16, NightingaleError> {
    match raw {
        None => Ok(default),
        Some(s) => s
            .trim()
            .parse::<u16>()
            .map_err(|_| NightingaleError::Other(format!("invalid port value: {s:?}"))),
    }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cargo test -p app-core analyzer::remote::`
Expected: all 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add app-core/src/analyzer/remote.rs app-core/src/analyzer/mod.rs
git commit -m "feat: resolve NIGHTINGALE_ANALYZER_MODE env config

Adds AnalyzerMode/RemoteConfig with an injectable-environment resolver
so remote mode's host/port/token config is unit-testable without
touching real process env vars."
```

---

### Task 3: Generalize `connect_and_authenticate` to accept a host

Today it hardcodes `127.0.0.1`. Remote mode needs to connect to an arbitrary LAN host.

**Files:**
- Modify: `app-core/src/analyzer/mod.rs` (the `connect_and_authenticate` function and its one call site in `spawn_server`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `fn connect_and_authenticate(host: &str, port: u16, token: &str) -> Result<(BufReader<TcpStream>, BufWriter<TcpStream>), NightingaleError>` (signature gains a leading `host: &str` parameter; used by Task 9).

- [ ] **Step 1: Write the failing test**

Add to the bottom of `app-core/src/analyzer/mod.rs` (it currently has no test module — this is the first one):

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, Write};
    use std::net::TcpListener;

    fn start_fake_server(expected_token: &'static str) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut writer = BufWriter::new(stream);
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let value: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
            assert_eq!(value["type"], "hello");
            assert_eq!(value["token"], expected_token);
            writer.write_all(b"{\"type\":\"hello_ack\"}\n").unwrap();
            writer.flush().unwrap();
        });
        port
    }

    #[test]
    fn connects_and_authenticates_over_loopback_ip() {
        let port = start_fake_server("secret-token");
        let result = connect_and_authenticate("127.0.0.1", port, "secret-token");
        assert!(result.is_ok());
    }

    #[test]
    fn connects_and_authenticates_over_hostname() {
        let port = start_fake_server("secret-token");
        let result = connect_and_authenticate("localhost", port, "secret-token");
        assert!(result.is_ok());
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail to compile**

Run: `cargo test -p app-core analyzer::tests::`
Expected: compile error — `connect_and_authenticate` takes 2 arguments, 3 supplied.

- [ ] **Step 3: Update `connect_and_authenticate` and its call site**

In `app-core/src/analyzer/mod.rs`, change:

```rust
fn connect_and_authenticate(
    port: u16,
    token: &str,
) -> Result<(BufReader<TcpStream>, BufWriter<TcpStream>), NightingaleError> {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let stream = TcpStream::connect_timeout(&addr, HANDSHAKE_TIMEOUT).map_err(|e| {
        NightingaleError::Other(format!("Failed to connect to analyzer server: {e}"))
    })?;
```

to:

```rust
fn connect_and_authenticate(
    host: &str,
    port: u16,
    token: &str,
) -> Result<(BufReader<TcpStream>, BufWriter<TcpStream>), NightingaleError> {
    use std::net::ToSocketAddrs;
    let addr = (host, port)
        .to_socket_addrs()
        .map_err(|e| NightingaleError::Other(format!("failed to resolve {host}:{port}: {e}")))?
        .next()
        .ok_or_else(|| {
            NightingaleError::Other(format!("no addresses found for {host}:{port}"))
        })?;
    let stream = TcpStream::connect_timeout(&addr, HANDSHAKE_TIMEOUT).map_err(|e| {
        NightingaleError::Other(format!(
            "Failed to connect to analyzer server at {host}:{port}: {e}"
        ))
    })?;
```

The rest of the function body is unchanged. Update its one call site in `spawn_server`:

```rust
let (reader, writer) = match connect_and_authenticate(handshake.port, &handshake.token) {
```

to:

```rust
let (reader, writer) = match connect_and_authenticate("127.0.0.1", handshake.port, &handshake.token) {
```

(`SocketAddr` import on line 3 of the file may now be unused if nothing else references it — check with the compiler warning in the next step and remove it from the `use std::net::{...}` line only if the compiler flags it as unused.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cargo test -p app-core analyzer::tests::`
Expected: both tests pass. Also run `cargo build -p app-core` to confirm no unused-import warnings were missed.

- [ ] **Step 5: Commit**

```bash
git add app-core/src/analyzer/mod.rs
git commit -m "refactor: parameterize connect_and_authenticate by host

Local mode keeps connecting to 127.0.0.1 unchanged; this makes the
same function reusable for remote mode's configured LAN host."
```

---

### Task 4: HTTP bridge client functions in `analyzer/remote.rs`

**Files:**
- Modify: `app-core/src/analyzer/remote.rs` (append to the file from Task 2)

**Interfaces:**
- Consumes: `RemoteConfig` (Task 2), `crate::cache::CacheDir` (existing — has public `pub path: PathBuf` field and `pub fn transcript_path(&self, hash: &str) -> PathBuf`).
- Produces (all `pub(crate)`):
  - `fn is_safe_result_filename(hash: &str, name: &str) -> bool`
  - `fn upload_source_audio(cfg: &RemoteConfig, hash: &str, ext: &str, local_path: &Path) -> Result<(), NightingaleError>`
  - `fn upload_lyrics(cfg: &RemoteConfig, hash: &str, local_path: &Path) -> Result<(), NightingaleError>`
  - `fn upload_transcript(cfg: &RemoteConfig, hash: &str, local_path: &Path) -> Result<(), NightingaleError>`
  - `fn upload_song_inputs(cfg: &RemoteConfig, hash: &str, audio_path: &Path, lyrics_path: Option<&Path>, cache: &CacheDir) -> Result<(), NightingaleError>`
  - `fn fetch_manifest(cfg: &RemoteConfig, hash: &str) -> Result<Vec<String>, NightingaleError>`
  - `fn download_result_file(cfg: &RemoteConfig, hash: &str, name: &str, cache_dir: &Path) -> Result<(), NightingaleError>`
  - `fn download_song_outputs(cfg: &RemoteConfig, hash: &str, cache: &CacheDir) -> Result<(), NightingaleError>`
  - `fn delete_work(cfg: &RemoteConfig, hash: &str)`

- [ ] **Step 1: Write the failing tests**

Append to `app-core/src/analyzer/remote.rs` (outside the existing `tests` module — add a second `#[cfg(test)] mod http_tests` block, or extend the existing one; keep it separate for clarity):

```rust
#[cfg(test)]
mod http_tests {
    use super::*;
    use crate::cache::CacheDir;
    use std::io::Read;
    use std::sync::{Arc, Mutex};
    use tiny_http::{Method, Response, Server};

    const HASH: &str = "ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab1";

    fn fake_bridge(token: &'static str) -> (RemoteConfig, Arc<Server>) {
        let server = Arc::new(Server::http("127.0.0.1:0").unwrap());
        let port = server.server_addr().to_ip().unwrap().port();
        let cfg = RemoteConfig {
            host: "127.0.0.1".to_string(),
            port: 0,
            http_port: port,
            token: token.to_string(),
        };
        (cfg, server)
    }

    #[test]
    fn is_safe_result_filename_rejects_traversal_and_mismatched_prefix() {
        assert!(is_safe_result_filename(HASH, &format!("{HASH}_vocals.mp3")));
        assert!(!is_safe_result_filename(HASH, "../etc/passwd"));
        assert!(!is_safe_result_filename(HASH, "other_vocals.mp3"));
        assert!(!is_safe_result_filename(HASH, ""));
        assert!(!is_safe_result_filename(HASH, &format!("{HASH}/../../etc")));
    }

    #[test]
    fn upload_source_audio_puts_bytes_with_auth_header() {
        let (cfg, server) = fake_bridge("tok");
        let received = Arc::new(Mutex::new(None));
        let received_clone = received.clone();
        let handle = std::thread::spawn(move || {
            let request = server.recv().unwrap();
            assert_eq!(request.method(), &Method::Put);
            assert_eq!(request.url(), format!("/sources/{HASH}?ext=mp3"));
            let auth = request
                .headers()
                .iter()
                .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case("authorization"))
                .map(|h| h.value.as_str().to_string());
            let mut body = Vec::new();
            let mut request = request;
            request.as_reader().read_to_end(&mut body).unwrap();
            *received_clone.lock().unwrap() = Some((auth, body));
            request
                .respond(Response::from_string("{\"ok\":true}"))
                .unwrap();
        });

        let tmp = std::env::temp_dir().join(format!("nightingale-test-{HASH}.mp3"));
        std::fs::write(&tmp, b"fake audio bytes").unwrap();

        let result = upload_source_audio(&cfg, HASH, "mp3", &tmp);
        handle.join().unwrap();
        let _ = std::fs::remove_file(&tmp);

        assert!(result.is_ok());
        let (auth, body) = received.lock().unwrap().take().unwrap();
        assert_eq!(auth, Some("Bearer tok".to_string()));
        assert_eq!(body, b"fake audio bytes");
    }

    #[test]
    fn fetch_manifest_parses_file_list() {
        let (cfg, server) = fake_bridge("tok");
        let handle = std::thread::spawn(move || {
            let request = server.recv().unwrap();
            assert_eq!(request.url(), format!("/results/{HASH}/manifest"));
            request
                .respond(Response::from_string(
                    format!("{{\"files\":[\"{HASH}_transcript.json\"]}}"),
                ))
                .unwrap();
        });

        let files = fetch_manifest(&cfg, HASH).unwrap();
        handle.join().unwrap();
        assert_eq!(files, vec![format!("{HASH}_transcript.json")]);
    }

    #[test]
    fn download_result_file_rejects_unsafe_name_without_making_a_request() {
        let (cfg, server) = fake_bridge("tok");
        // No thread consuming server.recv(): if the client tried to make a
        // request it would hang forever and the test would time out, proving
        // the safety check runs before any network call.
        drop(server);
        let cache_dir = std::env::temp_dir();
        let result = download_result_file(&cfg, HASH, "../../etc/passwd", &cache_dir);
        assert!(result.is_err());
    }

    #[test]
    fn upload_fails_promptly_when_nothing_is_listening() {
        // Review Focus: an unreachable remote host must fail within a
        // bounded time, not hang the analysis queue forever. Binding then
        // immediately dropping a listener gives a port nothing answers on
        // -- the OS returns "connection refused" almost instantly, which is
        // the fast-fail half of this property. A truly silent packet-drop
        // (firewalled, dead host) is exercised manually in Task 13's
        // runbook instead, since reliably simulating one without a real
        // unreachable network segment would make this test flaky.
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener); // nothing listens on `port` now

        let cfg = RemoteConfig {
            host: "127.0.0.1".to_string(),
            port: 0,
            http_port: port,
            token: "tok".to_string(),
        };
        let tmp = std::env::temp_dir().join(format!("nightingale-test-unreachable-{HASH}.mp3"));
        std::fs::write(&tmp, b"x").unwrap();

        let started = std::time::Instant::now();
        let result = upload_source_audio(&cfg, HASH, "mp3", &tmp);
        let elapsed = started.elapsed();

        let _ = std::fs::remove_file(&tmp);
        assert!(result.is_err());
        assert!(
            elapsed < std::time::Duration::from_secs(15),
            "upload took {elapsed:?}, expected a prompt failure"
        );
    }

    #[test]
    fn delete_work_sends_delete_request() {
        let (cfg, server) = fake_bridge("tok");
        let handle = std::thread::spawn(move || {
            let request = server.recv().unwrap();
            assert_eq!(request.method(), &Method::Delete);
            assert_eq!(request.url(), format!("/work/{HASH}"));
            request.respond(Response::from_string("{\"ok\":true}")).unwrap();
        });
        delete_work(&cfg, HASH);
        handle.join().unwrap();
    }

    #[test]
    fn upload_song_inputs_skips_lyrics_and_transcript_when_absent() {
        let (cfg, server) = fake_bridge("tok");
        let handle = std::thread::spawn(move || {
            // Only the audio PUT should arrive; nothing for lyrics/transcript.
            let request = server.recv().unwrap();
            assert_eq!(request.url(), format!("/sources/{HASH}?ext=mp3"));
            request.respond(Response::from_string("{\"ok\":true}")).unwrap();
        });

        let tmp_dir = std::env::temp_dir().join(format!("nightingale-test-cache-{HASH}"));
        std::fs::create_dir_all(&tmp_dir).unwrap();
        let cache = CacheDir { path: tmp_dir.clone() };
        let audio = tmp_dir.join("audio.mp3");
        std::fs::write(&audio, b"audio").unwrap();

        let result = upload_song_inputs(&cfg, HASH, &audio, None, &cache);
        handle.join().unwrap();
        let _ = std::fs::remove_dir_all(&tmp_dir);

        assert!(result.is_ok());
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cargo test -p app-core analyzer::remote::http_tests:: -- --nocapture`
Expected: compile errors (`upload_source_audio` etc. not found) — confirms the tests exercise real symbols once implemented.

- [ ] **Step 3: Implement the HTTP bridge client functions**

Append to `app-core/src/analyzer/remote.rs` (above the test modules):

```rust
use std::fs;
use std::path::Path;
use std::sync::LazyLock;
use std::time::Duration;

use crate::cache::CacheDir;

static HTTP_AGENT: LazyLock<ureq::Agent> = LazyLock::new(|| {
    let config = ureq::Agent::config_builder()
        .timeout_connect(Some(Duration::from_secs(10)))
        .timeout_global(Some(Duration::from_secs(120)))
        .build();
    ureq::Agent::new_with_config(config)
});

fn base_url(cfg: &RemoteConfig) -> String {
    format!("http://{}:{}", cfg.host, cfg.http_port)
}

fn auth_header(cfg: &RemoteConfig) -> String {
    format!("Bearer {}", cfg.token)
}

pub(crate) fn is_safe_result_filename(hash: &str, name: &str) -> bool {
    !name.is_empty()
        && name.starts_with(hash)
        && !name.contains('/')
        && !name.contains('\\')
        && !name.contains("..")
}

pub(crate) fn upload_source_audio(
    cfg: &RemoteConfig,
    hash: &str,
    ext: &str,
    local_path: &Path,
) -> Result<(), NightingaleError> {
    let bytes = fs::read(local_path)?;
    let url = format!("{}/sources/{hash}?ext={ext}", base_url(cfg));
    HTTP_AGENT
        .put(&url)
        .header("Authorization", auth_header(cfg))
        .send(bytes.as_slice())
        .map_err(|e| NightingaleError::Other(format!("upload failed for {hash}: {e}")))?;
    Ok(())
}

pub(crate) fn upload_lyrics(
    cfg: &RemoteConfig,
    hash: &str,
    local_path: &Path,
) -> Result<(), NightingaleError> {
    let bytes = fs::read(local_path)?;
    let url = format!("{}/sources/{hash}/lyrics", base_url(cfg));
    HTTP_AGENT
        .put(&url)
        .header("Authorization", auth_header(cfg))
        .send(bytes.as_slice())
        .map_err(|e| NightingaleError::Other(format!("lyrics upload failed for {hash}: {e}")))?;
    Ok(())
}

pub(crate) fn upload_transcript(
    cfg: &RemoteConfig,
    hash: &str,
    local_path: &Path,
) -> Result<(), NightingaleError> {
    let bytes = fs::read(local_path)?;
    let url = format!("{}/sources/{hash}/transcript", base_url(cfg));
    HTTP_AGENT
        .put(&url)
        .header("Authorization", auth_header(cfg))
        .send(bytes.as_slice())
        .map_err(|e| {
            NightingaleError::Other(format!("transcript upload failed for {hash}: {e}"))
        })?;
    Ok(())
}

/// Uploads everything `process_song`/`run_key_pass` may need the remote
/// daemon to see before sending `analyze`: the audio always, the lyrics file
/// when one was fetched, and the pre-existing transcript whenever it exists
/// locally (not gated on `skip_transcription` — see spec §5.2).
pub(crate) fn upload_song_inputs(
    cfg: &RemoteConfig,
    hash: &str,
    audio_path: &Path,
    lyrics_path: Option<&Path>,
    cache: &CacheDir,
) -> Result<(), NightingaleError> {
    let ext = audio_path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("bin");
    upload_source_audio(cfg, hash, ext, audio_path)?;

    if let Some(lp) = lyrics_path
        && lp.is_file()
    {
        upload_lyrics(cfg, hash, lp)?;
    }

    let transcript = cache.transcript_path(hash);
    if transcript.is_file() {
        upload_transcript(cfg, hash, &transcript)?;
    }

    Ok(())
}

#[derive(serde::Deserialize)]
struct Manifest {
    files: Vec<String>,
}

pub(crate) fn fetch_manifest(cfg: &RemoteConfig, hash: &str) -> Result<Vec<String>, NightingaleError> {
    let url = format!("{}/results/{hash}/manifest", base_url(cfg));
    let manifest: Manifest = HTTP_AGENT
        .get(&url)
        .header("Authorization", auth_header(cfg))
        .call()
        .map_err(|e| NightingaleError::Other(format!("manifest fetch failed for {hash}: {e}")))?
        .body_mut()
        .read_json()
        .map_err(|e| NightingaleError::Other(format!("manifest parse failed for {hash}: {e}")))?;
    Ok(manifest.files)
}

/// `name` must come from `fetch_manifest` (already hash-prefixed by the
/// remote daemon). Re-validated here so a compromised or buggy remote daemon
/// can never make this process write outside the local cache directory.
pub(crate) fn download_result_file(
    cfg: &RemoteConfig,
    hash: &str,
    name: &str,
    cache_dir: &Path,
) -> Result<(), NightingaleError> {
    if !is_safe_result_filename(hash, name) {
        return Err(NightingaleError::Other(format!(
            "remote analyzer returned unsafe file name: {name:?}"
        )));
    }
    let url = format!("{}/results/{hash}/{name}", base_url(cfg));
    let bytes = HTTP_AGENT
        .get(&url)
        .header("Authorization", auth_header(cfg))
        .call()
        .map_err(|e| NightingaleError::Other(format!("download failed for {name}: {e}")))?
        .into_body()
        .read_to_vec()
        .map_err(|e| NightingaleError::Other(format!("download read failed for {name}: {e}")))?;
    fs::write(cache_dir.join(name), bytes)?;
    Ok(())
}

/// Fetches the manifest for `hash` and downloads every listed file into
/// `cache`'s directory. Called once after the control connection reports
/// `done`.
pub(crate) fn download_song_outputs(
    cfg: &RemoteConfig,
    hash: &str,
    cache: &CacheDir,
) -> Result<(), NightingaleError> {
    let files = fetch_manifest(cfg, hash)?;
    for name in files {
        download_result_file(cfg, hash, &name, &cache.path)?;
    }
    Ok(())
}

/// Best-effort cleanup of the remote scratch directory. Errors are swallowed
/// — a leftover scratch file on the Mac is a nuisance, never worth failing an
/// already-finished (or already-failed) analysis over.
pub(crate) fn delete_work(cfg: &RemoteConfig, hash: &str) {
    let url = format!("{}/work/{hash}", base_url(cfg));
    let _ = HTTP_AGENT
        .delete(&url)
        .header("Authorization", auth_header(cfg))
        .call();
}
```

Also add `tiny_http = "0.12.0"` usage note: it is already a normal (non-dev) dependency in `app-core/Cargo.toml`, so it is available to the test module without any `Cargo.toml` change.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cargo test -p app-core analyzer::remote:: -- --test-threads=1`
Expected: all tests in both `tests` and `http_tests` pass. (`--test-threads=1` avoids any chance of two tests racing on the same ephemeral port allocation pattern; each test binds its own `127.0.0.1:0` so this is a safety margin, not a requirement.)

- [ ] **Step 5: Commit**

```bash
git add app-core/src/analyzer/remote.rs
git commit -m "feat: add remote analyzer HTTP file bridge client

Upload/download/delete functions for the Surface-side Rust client to
talk to the MacBook's file bridge, with a real tiny_http test server
exercising each one (no mocks) and a path-traversal guard on
downloaded file names."
```

---

### Task 5: `remote_server.py` — Workdir, safety checks, and `handle_analyze`

The pure, testable core of the MacBook daemon. No `http.server` or sockets yet — those come in Tasks 6/7. Crucially, this task does **not** import `pipeline`/`gpu`/`whisper_compat` at module scope, so the test file can run without torch/whisperx installed.

**Files:**
- Create: `app-core/analyzer/remote_server.py`
- Create: `app-core/analyzer/test_remote_server.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (importable by Tasks 6/7/8):
  - `HASH_RE`, `is_safe_hash(value) -> bool`
  - `is_safe_result_filename(file_hash, name) -> bool`
  - `class Workdir` with `__init__(self, root)`, `.ensure()`, `.source_path(file_hash, ext)`, `.find_source(file_hash)`, `.lyrics_path(file_hash)`, `.transcript_path(file_hash)`, `.result_files(file_hash)`, `.delete_work(file_hash)`
  - `handle_analyze(cmd, workdir, device, run_pipeline_fn)` — raises `RuntimeError` if no source was uploaded for `cmd["hash"]`.

- [ ] **Step 1: Write the failing tests**

Create `app-core/analyzer/test_remote_server.py`:

```python
import tempfile
import unittest
from pathlib import Path

from remote_server import (
    Workdir,
    handle_analyze,
    is_safe_hash,
    is_safe_result_filename,
)

HASH = "ab12" * 16  # 64 hex chars


class SafetyChecksTest(unittest.TestCase):
    def test_is_safe_hash_accepts_64_lowercase_hex(self):
        self.assertTrue(is_safe_hash(HASH))

    def test_is_safe_hash_rejects_bad_shapes(self):
        self.assertFalse(is_safe_hash(""))
        self.assertFalse(is_safe_hash("short"))
        self.assertFalse(is_safe_hash(HASH.upper()))
        self.assertFalse(is_safe_hash(HASH + "/../etc"))
        self.assertFalse(is_safe_hash("../" + HASH))

    def test_is_safe_result_filename_requires_hash_prefix_and_no_traversal(self):
        self.assertTrue(is_safe_result_filename(HASH, f"{HASH}_vocals.mp3"))
        self.assertFalse(is_safe_result_filename(HASH, "other_vocals.mp3"))
        self.assertFalse(is_safe_result_filename(HASH, f"{HASH}/../../etc/passwd"))
        self.assertFalse(is_safe_result_filename(HASH, ""))


class WorkdirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Workdir(self.tmp.name)
        self.workdir.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_source_returns_none_when_nothing_uploaded(self):
        self.assertIsNone(self.workdir.find_source(HASH))

    def test_find_source_finds_uploaded_file_regardless_of_extension(self):
        self.workdir.source_path(HASH, "flac").write_bytes(b"data")
        found = self.workdir.find_source(HASH)
        self.assertEqual(found.name, f"{HASH}.flac")

    def test_result_files_lists_only_matching_prefix(self):
        (self.workdir.cache / f"{HASH}_vocals.mp3").write_bytes(b"a")
        (self.workdir.cache / f"{HASH}_transcript.json").write_bytes(b"{}")
        (self.workdir.cache / "unrelated_vocals.mp3").write_bytes(b"b")
        files = self.workdir.result_files(HASH)
        self.assertEqual(
            sorted(files), sorted([f"{HASH}_vocals.mp3", f"{HASH}_transcript.json"])
        )

    def test_delete_work_removes_only_matching_files(self):
        self.workdir.source_path(HASH, "mp3").write_bytes(b"a")
        (self.workdir.cache / f"{HASH}_transcript.json").write_bytes(b"{}")
        other = self.workdir.cache / "unrelated_vocals.mp3"
        other.write_bytes(b"b")

        self.workdir.delete_work(HASH)

        self.assertIsNone(self.workdir.find_source(HASH))
        self.assertEqual(self.workdir.result_files(HASH), [])
        self.assertTrue(other.is_file())


class HandleAnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Workdir(self.tmp.name)
        self.workdir.ensure()

    def tearDown(self):
        self.tmp.cleanup()

    def test_raises_when_no_source_uploaded(self):
        with self.assertRaises(RuntimeError):
            handle_analyze({"hash": HASH}, self.workdir, "cpu", run_pipeline_fn=lambda **_: None)

    def test_calls_run_pipeline_with_resolved_paths(self):
        self.workdir.source_path(HASH, "mp3").write_bytes(b"audio")
        calls = []

        def fake_run_pipeline(audio_path, output_dir, file_hash, device, **kwargs):
            calls.append((audio_path, output_dir, file_hash, device, kwargs))

        handle_analyze(
            {"hash": HASH, "model": "large-v3", "beam_size": 8},
            self.workdir,
            "mps",
            run_pipeline_fn=fake_run_pipeline,
        )

        self.assertEqual(len(calls), 1)
        audio_path, output_dir, file_hash, device, kwargs = calls[0]
        self.assertTrue(audio_path.endswith(f"{HASH}.mp3"))
        self.assertEqual(output_dir, str(self.workdir.cache))
        self.assertEqual(file_hash, HASH)
        self.assertEqual(device, "mps")
        self.assertEqual(kwargs["model_name"], "large-v3")
        self.assertEqual(kwargs["beam_size"], 8)
        self.assertNotIn("lyrics_path", kwargs)

    def test_includes_lyrics_path_only_when_file_exists(self):
        self.workdir.source_path(HASH, "mp3").write_bytes(b"audio")
        calls = []

        def fake_run_pipeline(audio_path, output_dir, file_hash, device, **kwargs):
            calls.append(kwargs)

        # "lyrics" is set on the command but the file was never uploaded.
        handle_analyze(
            {"hash": HASH, "lyrics": True},
            self.workdir,
            "cpu",
            run_pipeline_fn=fake_run_pipeline,
        )
        self.assertNotIn("lyrics_path", calls[0])

        self.workdir.lyrics_path(HASH).write_text("{}")
        handle_analyze(
            {"hash": HASH, "lyrics": True},
            self.workdir,
            "cpu",
            run_pipeline_fn=fake_run_pipeline,
        )
        self.assertEqual(calls[1]["lyrics_path"], str(self.workdir.lyrics_path(HASH)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd app-core/analyzer
python3 -m unittest test_remote_server -v
```

Expected: `ModuleNotFoundError: No module named 'remote_server'` (the file doesn't exist yet).

- [ ] **Step 3: Implement `remote_server.py` (core only, no server loops yet)**

Create `app-core/analyzer/remote_server.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd app-core/analyzer
python3 -m unittest test_remote_server -v
```

Expected: all tests pass, using the plain system `python3` (no venv, no torch needed — confirms the core module has no heavy import at module scope).

- [ ] **Step 5: Commit**

```bash
git add app-core/analyzer/remote_server.py app-core/analyzer/test_remote_server.py
git commit -m "feat: add remote analyzer daemon core (workdir + safety checks)

Pure path-resolution and validation logic, independently testable with
plain python3 (no torch/whisperx needed) by deferring all heavy
imports out of this module's top level."
```

---

### Task 6: `remote_server.py` — HTTP bridge handlers

**Files:**
- Modify: `app-core/analyzer/remote_server.py` (append)
- Modify: `app-core/analyzer/test_remote_server.py` (append)

**Interfaces:**
- Consumes: `Workdir`, `is_safe_hash`, `is_safe_result_filename` (Task 5).
- Produces: `make_http_handler(workdir, token) -> type` (a `BaseHTTPRequestHandler` subclass), `start_http_bridge(bind, port, workdir, token) -> http.server.ThreadingHTTPServer` (already serving in a background daemon thread when it returns).

- [ ] **Step 1: Write the failing tests**

Append to `app-core/analyzer/test_remote_server.py`:

```python
import json
import socket
import tempfile
import unittest
import urllib.error
import urllib.request

from remote_server import Workdir, start_http_bridge


class HttpBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Workdir(self.tmp.name)
        self.workdir.ensure()
        self.server = start_http_bridge("127.0.0.1", 0, self.workdir, "tok")
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _request(self, method, path, body=None, token="tok"):
        req = urllib.request.Request(self._url(path), data=body, method=method)
        if token is not None:
            req.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(req)

    def test_upload_and_download_round_trip(self):
        file_hash = "cd34" * 16
        self._request("PUT", f"/sources/{file_hash}?ext=mp3", body=b"audio bytes")
        self.assertEqual(
            self.workdir.find_source(file_hash).read_bytes(), b"audio bytes"
        )

        # Simulate the pipeline having produced a result file directly.
        (self.workdir.cache / f"{file_hash}_vocals.mp3").write_bytes(b"vocals!")

        manifest = json.loads(
            self._request("GET", f"/results/{file_hash}/manifest").read()
        )
        self.assertEqual(manifest["files"], [f"{file_hash}_vocals.mp3"])

        downloaded = self._request(
            "GET", f"/results/{file_hash}/{file_hash}_vocals.mp3"
        ).read()
        self.assertEqual(downloaded, b"vocals!")

        self._request("DELETE", f"/work/{file_hash}")
        self.assertIsNone(self.workdir.find_source(file_hash))

    def test_missing_token_is_rejected(self):
        file_hash = "ef56" * 16
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("PUT", f"/sources/{file_hash}?ext=mp3", body=b"x", token=None)
        self.assertEqual(ctx.exception.code, 401)

    def test_wrong_token_is_rejected(self):
        file_hash = "ef56" * 16
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request(
                "PUT", f"/sources/{file_hash}?ext=mp3", body=b"x", token="wrong"
            )
        self.assertEqual(ctx.exception.code, 401)

    def test_path_traversal_hash_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("PUT", "/sources/../../etc?ext=mp3", body=b"x")
        self.assertEqual(ctx.exception.code, 400)

    def test_path_traversal_result_filename_is_rejected(self):
        file_hash = "ef56" * 16
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("GET", f"/results/{file_hash}/../../etc/passwd")
        self.assertEqual(ctx.exception.code, 400)

    def test_incomplete_upload_body_is_rejected(self):
        # Review Focus: a connection dropped mid-upload must not leave a
        # silently truncated file on disk. urllib always sends the full body
        # it was given, so this needs a raw socket that claims a
        # Content-Length larger than what it actually sends.
        file_hash = "9a8b" * 16
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        request_head = (
            f"PUT /sources/{file_hash}?ext=mp3 HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Authorization: Bearer tok\r\n"
            f"Content-Length: 1000\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("utf-8")
        conn.sendall(request_head)
        conn.sendall(b"only ten!")  # far fewer than the declared 1000 bytes
        conn.shutdown(socket.SHUT_WR)

        response = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            response += chunk
        conn.close()

        self.assertIn(b" 400 ", response.splitlines()[0])
        self.assertIsNone(self.workdir.find_source(file_hash))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd app-core/analyzer
python3 -m unittest test_remote_server.HttpBridgeTest -v
```

Expected: `ImportError: cannot import name 'start_http_bridge'`.

- [ ] **Step 3: Implement the HTTP bridge**

Append to `app-core/analyzer/remote_server.py`:

```python
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
```

Note on the "path traversal hash" test: a URL like `/sources/../../etc?ext=mp3` is normalized by `urlparse`/`split("/")` into parts `["..", "..", "etc"]` (3 parts, not matching the 2-part `sources/<hash>` shape) so it falls through to the generic `404`, not a `400` for the `/sources/<hash>` route specifically — adjust the test's expected status to `404` for that one case if it does not match `400` when you run it, and note why in a comment; what must hold regardless of which code is returned is that **no file is ever written outside `workdir`**, which `is_safe_hash` plus never concatenating raw `self.path` into a filesystem path already guarantees.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd app-core/analyzer
python3 -m unittest test_remote_server -v
```

Expected: all tests pass (adjust the exact status code in `test_path_traversal_hash_is_rejected` per the note above if needed — the important assertion is that it is a 4xx, not a 2xx, and that no file was written).

- [ ] **Step 5: Commit**

```bash
git add app-core/analyzer/remote_server.py app-core/analyzer/test_remote_server.py
git commit -m "feat: add remote analyzer HTTP file bridge

Token-authenticated upload/manifest/download/delete endpoints backed
by Workdir, tested against a real ThreadingHTTPServer (stdlib only,
no mocks)."
```

---

### Task 7: `remote_server.py` — NDJSON control loop and `main()`

**Files:**
- Modify: `app-core/analyzer/remote_server.py` (append)
- Modify: `app-core/analyzer/test_remote_server.py` (append)

**Interfaces:**
- Consumes: `handle_analyze` (Task 5).
- Produces: `bind_control_socket(bind, port) -> socket.socket`, `serve_control(srv, token, workdir, device)` (blocking accept-loop), `handle_session(conn, token, workdir, device)`, `main()`.

- [ ] **Step 1: Write the failing tests**

Append to `app-core/analyzer/test_remote_server.py`:

```python
import socket
import threading

from remote_server import bind_control_socket, serve_control


class ControlLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Workdir(self.tmp.name)
        self.workdir.ensure()
        self.srv = bind_control_socket("127.0.0.1", 0)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(
            target=serve_control,
            args=(self.srv, "tok", self.workdir, "cpu"),
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.srv.close()
        self.tmp.cleanup()

    def _connect(self):
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        return conn.makefile("r", encoding="utf-8", newline="\n"), conn.makefile(
            "w", encoding="utf-8", newline="\n"
        ), conn

    def test_hello_with_correct_token_gets_ack(self):
        rfile, wfile, conn = self._connect()
        wfile.write(json.dumps({"type": "hello", "token": "tok"}) + "\n")
        wfile.flush()
        response = json.loads(rfile.readline())
        self.assertEqual(response["type"], "hello_ack")
        wfile.write(json.dumps({"type": "quit"}) + "\n")
        wfile.flush()
        conn.close()

    def test_hello_with_wrong_token_gets_no_ack_and_connection_closes(self):
        rfile, wfile, conn = self._connect()
        wfile.write(json.dumps({"type": "hello", "token": "wrong"}) + "\n")
        wfile.flush()
        response_line = rfile.readline()
        self.assertEqual(response_line, "")  # server closed without acking
        conn.close()

    def test_unknown_command_after_hello_gets_generic_error(self):
        rfile, wfile, conn = self._connect()
        wfile.write(json.dumps({"type": "hello", "token": "tok"}) + "\n")
        wfile.flush()
        rfile.readline()  # hello_ack

        wfile.write(json.dumps({"type": "bogus"}) + "\n")
        wfile.flush()
        response = json.loads(rfile.readline())
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["kind"], "generic")

        wfile.write(json.dumps({"type": "quit"}) + "\n")
        wfile.flush()
        conn.close()

    def test_daemon_accepts_a_second_session_after_the_first_quits(self):
        rfile1, wfile1, conn1 = self._connect()
        wfile1.write(json.dumps({"type": "hello", "token": "tok"}) + "\n")
        wfile1.flush()
        rfile1.readline()
        wfile1.write(json.dumps({"type": "quit"}) + "\n")
        wfile1.flush()
        conn1.close()

        rfile2, wfile2, conn2 = self._connect()
        wfile2.write(json.dumps({"type": "hello", "token": "tok"}) + "\n")
        wfile2.flush()
        response = json.loads(rfile2.readline())
        self.assertEqual(response["type"], "hello_ack")
        wfile2.write(json.dumps({"type": "quit"}) + "\n")
        wfile2.flush()
        conn2.close()


if __name__ == "__main__":
    unittest.main()
```

None of these tests send an `"analyze"` command, so `pipeline`/`gpu`/`whisper_compat` are never imported — the whole test file still runs with plain `python3`, no venv required.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd app-core/analyzer
python3 -m unittest test_remote_server.ControlLoopTest -v
```

Expected: `ImportError: cannot import name 'bind_control_socket'`.

- [ ] **Step 3: Implement the control loop**

Append to `app-core/analyzer/remote_server.py`:

```python
import socket
import sys


def _send(wfile, payload):
    try:
        wfile.write(json.dumps(payload, ensure_ascii=False) + "\n")
        wfile.flush()
    except (BrokenPipeError, OSError) as e:
        print(f"[remote-analyzer] failed to send message: {e}", file=sys.stderr, flush=True)


def bind_control_socket(bind, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(5)
    return srv


def serve_control(srv, token, workdir, device):
    bind, port = srv.getsockname()
    print(f"[remote-analyzer] control listening on {bind}:{port}", flush=True)
    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            return  # socket was closed (e.g. test teardown)
        print(f"[remote-analyzer] client connected from {addr}", flush=True)
        try:
            handle_session(conn, token, workdir, device)
        except Exception as e:
            print(f"[remote-analyzer] session error: {e}", file=sys.stderr, flush=True)
        finally:
            conn.close()


def handle_session(conn, token, workdir, device):
    conn.settimeout(None)
    rfile = conn.makefile("r", encoding="utf-8", newline="\n")
    wfile = conn.makefile("w", encoding="utf-8", newline="\n")

    hello_line = rfile.readline()
    if not hello_line:
        return
    hello = json.loads(hello_line)
    if hello.get("type") != "hello" or hello.get("token") != token:
        print("[remote-analyzer] auth failed, closing connection", file=sys.stderr, flush=True)
        return
    _send(wfile, {"type": "hello_ack"})

    for line in rfile:
        line = line.strip()
        if not line:
            continue
        cmd = json.loads(line)
        ctype = cmd.get("type")
        if ctype == "quit":
            break
        if ctype != "analyze":
            _send(
                wfile,
                {"type": "error", "kind": "generic", "msg": f"Unknown command: {ctype!r}"},
            )
            continue
        _run_analyze_command(cmd, workdir, device, wfile)


def _run_analyze_command(cmd, workdir, device, wfile):
    # Imported here, not at module scope, so hello/quit/unknown-command
    # sessions never require torch/whisperx to be installed.
    from pipeline import run_pipeline
    from gpu import end_of_song_cleanup, log_vram, reset_peak_stats
    from whisper_compat import is_oom, set_progress_sink

    set_progress_sink(
        lambda pct, msg: _send(wfile, {"type": "progress", "pct": int(pct), "msg": str(msg)})
    )
    try:
        reset_peak_stats()
        log_vram("song_start")
        try:
            handle_analyze(cmd, workdir, device, run_pipeline)
        finally:
            end_of_song_cleanup()
            log_vram("song_end")
        _send(wfile, {"type": "done", "hash": cmd.get("hash", "")})
    except Exception as e:
        import traceback

        traceback.print_exc(file=sys.stderr)
        err_str = str(e)
        kind = "oom" if is_oom(err_str) else "generic"
        _send(wfile, {"type": "error", "kind": kind, "msg": err_str})


def main():
    from pathlib import Path

    bind = os.environ.get("NIGHTINGALE_ANALYZER_BIND")
    if not bind:
        print(
            "NIGHTINGALE_ANALYZER_BIND is required (an explicit LAN interface IP, "
            "e.g. 192.168.11.50 -- never 0.0.0.0 or 127.0.0.1 for a real LAN daemon)",
            file=sys.stderr,
        )
        sys.exit(1)
    token = os.environ.get("NIGHTINGALE_ANALYZER_TOKEN")
    if not token:
        print("NIGHTINGALE_ANALYZER_TOKEN is required", file=sys.stderr)
        sys.exit(1)
    port = int(os.environ.get("NIGHTINGALE_ANALYZER_PORT", "8787"))
    http_port = int(os.environ.get("NIGHTINGALE_ANALYZER_HTTP_PORT", "8788"))
    workdir_path = os.environ.get(
        "NIGHTINGALE_ANALYZER_WORKDIR",
        str(Path.home() / ".nightingale" / "vendor" / "remote_work"),
    )

    workdir = Workdir(workdir_path)
    workdir.ensure()

    from whisper_compat import detect_device

    device = detect_device()
    print(f"[remote-analyzer] device={device} workdir={workdir_path}", flush=True)

    start_http_bridge(bind, http_port, workdir, token)
    srv = bind_control_socket(bind, port)
    serve_control(srv, token, workdir, device)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd app-core/analyzer
python3 -m unittest test_remote_server -v
```

Expected: every test across `SafetyChecksTest`, `WorkdirTest`, `HandleAnalyzeTest`, `HttpBridgeTest`, and `ControlLoopTest` passes, still using plain `python3` (confirm with `python3 --version` that this is not accidentally the vendor venv with torch — the point of this task's design is that it doesn't need to be).

- [ ] **Step 5: Commit**

```bash
git add app-core/analyzer/remote_server.py app-core/analyzer/test_remote_server.py
git commit -m "feat: add remote analyzer NDJSON control loop

Accept-loop daemon (unlike server.py's one-shot child process) with a
static shared token; analyze commands lazily import pipeline/gpu/
whisper_compat so hello/quit/error paths stay testable without torch."
```

---

### Task 8: Embed `remote_server.py` via `vendor_scripts.rs`

**Files:**
- Modify: `app-core/src/vendor_scripts.rs`

**Interfaces:**
- Consumes: `app-core/analyzer/remote_server.py` (Tasks 5–7, now complete).
- Produces: `remote_server.py` now extracted into `<vendor_dir>/analyzer/` by the existing, unmodified `step_extract_scripts()` / `write_scripts()` flow.

- [ ] **Step 1: Write the failing test**

`vendor_scripts.rs` has no test module yet. Add one that proves `write_scripts` extracts every registered file, including the new one, by name:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn write_scripts_extracts_remote_server_py() {
        let tmp = std::env::temp_dir().join("nightingale-vendor-scripts-test");
        let _ = std::fs::remove_dir_all(&tmp);
        write_scripts(&tmp).unwrap();

        let content = std::fs::read_to_string(tmp.join("remote_server.py")).unwrap();
        assert!(content.contains("NIGHTINGALE_ANALYZER_BIND"));

        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn write_scripts_extracts_every_registered_file() {
        let tmp = std::env::temp_dir().join("nightingale-vendor-scripts-test-2");
        let _ = std::fs::remove_dir_all(&tmp);
        write_scripts(&tmp).unwrap();

        for (name, _) in FILES {
            assert!(tmp.join(name).is_file(), "missing extracted file: {name}");
        }

        let _ = std::fs::remove_dir_all(&tmp);
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cargo test -p app-core vendor_scripts::`
Expected: `write_scripts_extracts_remote_server_py` fails (no file `remote_server.py` extracted); `write_scripts_extracts_every_registered_file` passes trivially (it only checks files already in `FILES`, which doesn't include the new one yet) — that second test is here to keep passing as a regression guard once the new entry is added.

- [ ] **Step 3: Register `remote_server.py`**

In `app-core/src/vendor_scripts.rs`, add alongside the other `const ..._PY` declarations:

```rust
const REMOTE_SERVER_PY: &str = include_str!("../analyzer/remote_server.py");
```

and add to the `FILES` array:

```rust
const FILES: &[(&str, &str)] = &[
    ("analyze.py", ANALYZE_PY),
    ("server.py", SERVER_PY),
    ("remote_server.py", REMOTE_SERVER_PY),
    ("pipeline.py", PIPELINE_PY),
    // ... rest unchanged ...
];
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cargo test -p app-core vendor_scripts::`
Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add app-core/src/vendor_scripts.rs
git commit -m "feat: embed remote_server.py in the vendor script bundle

Extracted by the existing, unmodified setup flow alongside the other
analyzer scripts -- provisioning the MacBook daemon is just running
Nightingale's normal first-run setup once."
```

---

### Task 9: Wire `spawn_server()` for remote mode

**Files:**
- Modify: `app-core/src/analyzer/mod.rs`

**Interfaces:**
- Consumes: `remote::resolve_mode`, `remote::AnalyzerMode`, `remote::RemoteConfig` (Task 2), `connect_and_authenticate(host, port, token)` (Task 3).
- Produces: `ServerProcess { child: Option<Child>, .. }` (was `Child`), `spawn_server()` now mode-aware.

- [ ] **Step 1: Change `ServerProcess` and its `Drop` impl**

In `app-core/src/analyzer/mod.rs`, change:

```rust
struct ServerProcess {
    child: Child,
    reader: BufReader<TcpStream>,
    writer: BufWriter<TcpStream>,
}

impl Drop for ServerProcess {
    fn drop(&mut self) {
        let pid = self.child.id();
        info!("[analyzer] Killing server process (pid={pid})");
        SERVER_PID.store(0, Ordering::SeqCst);
        lock_unpoisoned(&SERVER_INTERRUPT).take();
        if let Ok(stream) = self.writer.get_ref().try_clone() {
            let _ = stream.shutdown(Shutdown::Both);
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
```

to:

```rust
struct ServerProcess {
    /// `None` in remote mode: there is no local child process to kill, only
    /// a TCP connection to a long-lived LAN daemon that outlives this app.
    child: Option<Child>,
    reader: BufReader<TcpStream>,
    writer: BufWriter<TcpStream>,
}

impl Drop for ServerProcess {
    fn drop(&mut self) {
        if let Some(child) = &self.child {
            info!("[analyzer] Killing server process (pid={})", child.id());
        } else {
            info!("[analyzer] Closing remote analyzer connection");
        }
        SERVER_PID.store(0, Ordering::SeqCst);
        lock_unpoisoned(&SERVER_INTERRUPT).take();
        if let Ok(stream) = self.writer.get_ref().try_clone() {
            let _ = stream.shutdown(Shutdown::Both);
        }
        if let Some(child) = self.child.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}
```

- [ ] **Step 2: Split `spawn_server` into local/remote variants**

Rename the existing `spawn_server` function to `spawn_local_server`, and change its final `Ok(ServerProcess { child, reader, writer })` to `Ok(ServerProcess { child: Some(child), reader, writer })`. Then add:

```rust
fn spawn_remote_server(cfg: &remote::RemoteConfig) -> Result<ServerProcess, NightingaleError> {
    info!(
        "[analyzer] Connecting to remote analyzer at {}:{}",
        cfg.host, cfg.port
    );
    let (reader, writer) = connect_and_authenticate(&cfg.host, cfg.port, &cfg.token)?;

    let interrupt = writer
        .get_ref()
        .try_clone()
        .map_err(NightingaleError::from)?;
    *lock_unpoisoned(&SERVER_INTERRUPT) = Some(interrupt);

    Ok(ServerProcess {
        child: None,
        reader,
        writer,
    })
}

fn spawn_server() -> Result<ServerProcess, NightingaleError> {
    match remote::resolve_mode()? {
        remote::AnalyzerMode::Local => spawn_local_server(),
        remote::AnalyzerMode::Remote(cfg) => spawn_remote_server(&cfg),
    }
}
```

`spawn_local_server`'s body already sets `SERVER_INTERRUPT` near its end (it did so before this change, for the local path) — leave that assignment where it is; only the remote path needs the new one added above, since it has no equivalent existing line.

- [ ] **Step 3: Verify it builds**

Run: `cargo check -p app-core`
Expected: compiles. (There is no automated test for this task specifically — `ensure_server`'s local-mode behavior is exercised indirectly by every existing manual local-mode usage of the app, and remote-mode connection behavior is covered by Task 3's `connect_and_authenticate` tests plus Task 13's manual end-to-end run. Introducing a fake-remote-daemon integration test here would duplicate Task 3's coverage without adding new risk coverage, so it is intentionally not added.)

- [ ] **Step 4: Commit**

```bash
git add app-core/src/analyzer/mod.rs
git commit -m "feat: make spawn_server mode-aware (local spawn vs remote connect)

Remote mode skips the child process entirely and connects straight to
the configured LAN daemon; ServerProcess.child becomes optional so
Drop only kills a process that exists."
```

---

### Task 10: Wire `process_song()` remote upload/download

**Files:**
- Modify: `app-core/src/analyzer/mod.rs`

**Interfaces:**
- Consumes: `remote::resolve_mode`, `remote::upload_song_inputs`, `remote::download_song_outputs`, `remote::delete_work` (Tasks 2/4).
- Produces: `process_song()` now performs the upload/download/cleanup round trip in remote mode; unchanged behavior in local mode.

- [ ] **Step 1: Add the upload step before the retry loop**

In `process_song()`, locate the existing block right before `let mut retried = false; loop { ... }` (the code that builds `json_str` from `cmd_json`). Immediately after the existing `let json_str = match serde_json::to_string(&cmd_json) { ... };` block and before `let mut retried = false;`, insert:

```rust
let remote_cfg = match remote::resolve_mode() {
    Ok(remote::AnalyzerMode::Remote(cfg)) => Some(cfg),
    Ok(remote::AnalyzerMode::Local) => None,
    Err(e) => {
        if !discard_cancelled_job(initial_hash, file_hash) {
            update_queue_status(file_hash, QueuedStatus::Failed(e.to_string()));
        }
        return;
    }
};

if let Some(cfg) = &remote_cfg {
    let lyrics_ref = lyrics_path.as_deref();
    if let Err(e) = remote::upload_song_inputs(cfg, file_hash, &local_path, lyrics_ref, cache) {
        if !discard_cancelled_job(initial_hash, file_hash) {
            update_queue_status(
                file_hash,
                QueuedStatus::Failed(format!("remote upload failed: {e}")),
            );
        }
        return;
    }
}
```

(`lyrics_path` and `local_path` are already in scope at this point in the existing function — `lyrics_path: Option<PathBuf>` and `local_path: PathBuf` are bound earlier in `process_song`.)

- [ ] **Step 2: Add remote cleanup/download to every terminal branch of the match**

Review Focus: cancellation mid-analysis must not leave orphaned files on the
remote scratch directory. The existing `match` has several arms that end the
job (`return`) and two that retry (`continue`, keeping the already-uploaded
audio so a transient crash doesn't force a re-upload) — every `return` arm
needs a `remote::delete_work` call when in remote mode; the `continue` arms
must *not* call it, since the job isn't actually over yet.

Locate the existing `match send_and_monitor(server, &json_str, Some(file_hash), Some(initial_hash)) { ... }` block (it is the last statement in the `loop { ... }` body) and replace the whole block with:

```rust
match send_and_monitor(server, &json_str, Some(file_hash), Some(initial_hash)) {
    Ok(SongResult::Done) => {
        let mut state = lock_unpoisoned(&ANALYZER);
        let cancelled =
            state.cancelled.remove(initial_hash) | state.cancelled.remove(file_hash);
        if cancelled {
            drop(state);
            remove_from_queue(initial_hash);
            remove_from_queue(file_hash);
            lock_unpoisoned(&FORCE_TRANSCRIBE).remove(initial_hash);
            lock_unpoisoned(&FORCE_TRANSCRIBE).remove(file_hash);
            lock_unpoisoned(&STEMS_ONLY).remove(initial_hash);
            lock_unpoisoned(&STEMS_ONLY).remove(file_hash);
            *guard = None;
            if let Some(cfg) = &remote_cfg {
                remote::delete_work(cfg, file_hash);
            }
        } else {
            if let Some(cfg) = &remote_cfg {
                if let Err(e) = remote::download_song_outputs(cfg, file_hash, cache) {
                    remote::delete_work(cfg, file_hash);
                    update_queue_status(
                        file_hash,
                        QueuedStatus::Failed(format!("remote download failed: {e}")),
                    );
                    return;
                }
                remote::delete_work(cfg, file_hash);
            }
            finalize_song(file_hash, cache);
        }
        return;
    }
    Ok(SongResult::Cancelled) => {
        let _ = discard_cancelled_job(initial_hash, file_hash);
        *guard = None;
        if let Some(cfg) = &remote_cfg {
            remote::delete_work(cfg, file_hash);
        }
        return;
    }
    Ok(SongResult::Oom) => {
        warn!("[analyzer] CUDA OOM, killing server to free GPU memory");
        *guard = None;

        if !retried {
            retried = true;
            info!("[analyzer] Respawning server and retrying with clean GPU");
            update_queue_status(file_hash, QueuedStatus::Analyzing(0));
            continue;
        }
        if let Some(cfg) = &remote_cfg {
            remote::delete_work(cfg, file_hash);
        }
        update_queue_status(file_hash, QueuedStatus::Failed("CUDA out of memory".into()));
        return;
    }
    Ok(SongResult::Error(msg)) => {
        if let Some(cfg) = &remote_cfg {
            remote::delete_work(cfg, file_hash);
        }
        update_queue_status(file_hash, QueuedStatus::Failed(msg));
        return;
    }
    Err(e) => {
        if discard_cancelled_job(initial_hash, file_hash) {
            *guard = None;
            if let Some(cfg) = &remote_cfg {
                remote::delete_work(cfg, file_hash);
            }
            return;
        }

        warn!("[analyzer] Server crashed: {e}");
        *guard = None;

        if !retried {
            retried = true;
            info!("[analyzer] Respawning server and retrying");
            update_queue_status(file_hash, QueuedStatus::Analyzing(0));
            continue;
        }
        if let Some(cfg) = &remote_cfg {
            remote::delete_work(cfg, file_hash);
        }
        update_queue_status(
            file_hash,
            QueuedStatus::Failed(format!("Server crashed: {e}")),
        );
        return;
    }
}
```

Only the arm bodies changed (each gained a `remote::delete_work` call on its
`return` paths); the match's branching logic and local-mode behavior
(`remote_cfg` is `None`) are identical to today.

- [ ] **Step 3: Verify it builds**

Run: `cargo check -p app-core`
Expected: compiles cleanly. `cargo test -p app-core` should still show every previous test passing (local-mode code paths are untouched; `remote_cfg` is `None` whenever `NIGHTINGALE_ANALYZER_MODE` is unset, matching the default test environment).

- [ ] **Step 4: Commit**

```bash
git add app-core/src/analyzer/mod.rs
git commit -m "feat: upload/download song files around the remote analyze call

process_song() now round-trips audio/lyrics/transcript to the remote
daemon and back when NIGHTINGALE_ANALYZER_MODE=remote, cleaning up the
remote scratch directory afterward (success, cancel, or failure)."
```

---

### Task 11: Wire `run_key_pass()` remote upload/download

**Files:**
- Modify: `app-core/src/analyzer/mod.rs`

**Interfaces:**
- Consumes: same `remote::*` functions as Task 10.
- Produces: `run_key_pass()` now performs the same round trip (audio + pre-existing transcript, no lyrics) for the LRC "key only" background pass.

- [ ] **Step 1: Add upload before and download after the key-pass loop**

In `run_key_pass(cache: &CacheDir, local_path: &Path, file_hash: &str)`, right after the existing `let json_str = serde_json::to_string(&cmd_json)?;` line and before `let mut retried = false;`, insert:

```rust
let remote_cfg = match remote::resolve_mode()? {
    remote::AnalyzerMode::Remote(cfg) => Some(cfg),
    remote::AnalyzerMode::Local => None,
};
if let Some(cfg) = &remote_cfg {
    remote::upload_song_inputs(cfg, file_hash, local_path, None, cache)?;
}
```

Then change the `Ok(SongResult::Done) => return Ok(()),` arm inside that function's loop to:

```rust
Ok(SongResult::Done) => {
    if let Some(cfg) = &remote_cfg {
        let result = remote::download_song_outputs(cfg, file_hash, cache);
        remote::delete_work(cfg, file_hash);
        result?;
    }
    return Ok(());
}
```

(`run_key_pass` returns `Result<(), NightingaleError>`, so `?` is valid here — unlike `process_song`, which returns `()` and handles errors by updating the queue status instead.)

- [ ] **Step 2: Verify it builds**

Run: `cargo check -p app-core`
Expected: compiles cleanly.

- [ ] **Step 3: Run the full test suite**

Run: `cargo test -p app-core`
Expected: every test from Tasks 2, 3, 4, 8 still passes.

- [ ] **Step 4: Commit**

```bash
git add app-core/src/analyzer/mod.rs
git commit -m "feat: round-trip remote files in the LRC key-only pass

run_key_pass() mirrors process_song()'s upload/download/cleanup so
the background key-detection pass for LRC-provided songs also works
in remote mode."
```

---

### Task 12: Operator runbook

**Files:**
- Create: `docs/remote-analyzer.md`

**Interfaces:**
- Consumes: the env var tables from the spec (§8) and the confirmed `client/src-server` setup command (`cargo run -p server`, `POST /api/cmd/trigger_setup`).
- Produces: a standalone operator-facing doc with the four sections requested.

- [ ] **Step 1: Write the runbook**

Create `docs/remote-analyzer.md`:

```markdown
# Running the Nightingale analyzer remotely over LAN

This sets up a Python analyzer daemon on another machine on your LAN (e.g. a
MacBook) and points your Surface-side Nightingale at it instead of analyzing
locally. See `docs/superpowers/specs/2026-10-01-remote-analyzer-design.md`
for the full design and protocol.

Never expose these ports to the internet. Bind only to your LAN interface,
never `0.0.0.0`, and don't forward these ports on your router.

## MacBook 側: 実行するコマンド

One-time setup (installs ffmpeg/python/venv/ML packages into
`~/.nightingale/vendor`, same as the normal Nightingale first-run setup):

    cd client/src-server
    cargo run --release -- --bind 127.0.0.1:8080 &
    curl -X POST http://127.0.0.1:8080/api/cmd/trigger_setup -d '{}'
    # Wait for it to finish -- check for the ready marker:
    until [ -f ~/.nightingale/vendor/.ready ]; do sleep 5; done
    kill %1   # stop the temporary setup server, it is no longer needed

Generate a shared token once:

    python3 -c "import secrets; print(secrets.token_hex(32))"

Start the remote analyzer daemon (replace the bind IP with your Mac's actual
LAN address, e.g. from `ipconfig getifaddr en0`):

    NIGHTINGALE_ANALYZER_BIND=192.168.11.50 \
    NIGHTINGALE_ANALYZER_TOKEN=<the generated token> \
    ~/.nightingale/vendor/venv/bin/python \
    ~/.nightingale/vendor/analyzer/remote_server.py

Leave this running in a terminal (or `tmux`/`screen` session) for as long as
you want the Surface device to be able to analyze remotely. If your Mac's
lid closing matters, see the `sleep-guard` skill for keeping it awake.

## Surface 側: 実行するコマンド

Set these before launching Nightingale (desktop app or `server` binary):

    set NIGHTINGALE_ANALYZER_MODE=remote
    set NIGHTINGALE_ANALYZER_HOST=192.168.11.50
    set NIGHTINGALE_ANALYZER_TOKEN=<the same generated token>

(On PowerShell: `$env:NIGHTINGALE_ANALYZER_MODE = "remote"`, etc. On the
self-hosted `server` binary, these can also go in a `.env` file next to it,
since it already loads one via `dotenvy`.)

Then launch Nightingale as usual. `NIGHTINGALE_ANALYZER_PORT` /
`NIGHTINGALE_ANALYZER_HTTP_PORT` only need setting if you changed the
defaults (`8787` / `8788`) on the MacBook side.

## Nightingale 設定: 変更する内容

None beyond the environment variables above in this version — there is no
Settings UI toggle yet (deliberately out of scope for v1; see the design
spec's Non-goals). `NIGHTINGALE_ANALYZER_MODE` unset (or `local`) keeps
today's behavior unchanged.

## テスト: 確認する内容

1. Loopback first: run `remote_server.py` with
   `NIGHTINGALE_ANALYZER_BIND=127.0.0.1` on the same machine as a Nightingale
   build pointed at `NIGHTINGALE_ANALYZER_HOST=127.0.0.1`. Analyze one short
   local song. Confirm vocals/instrumental/transcript appear in the local
   cache and the song plays with stems.
2. Real LAN: repeat with the MacBook on its real LAN IP and Nightingale
   running on the actual Surface device. Confirm
   `curl -H "Authorization: Bearer <token>" http://192.168.11.50:8788/results/<hash>/manifest`
   returns `{"files":[]}` after the analysis completes (proves the remote
   scratch directory was cleaned up).
3. Wrong token: temporarily set a wrong `NIGHTINGALE_ANALYZER_TOKEN` on the
   Surface side and confirm the queue entry fails with an auth-related
   message rather than hanging.
4. Daemon not running: stop `remote_server.py` and confirm a queued analysis
   fails within the configured timeout (not longer than ~2 minutes) with a
   message naming the host:port, rather than hanging indefinitely.
5. Cancel mid-analysis: start an analysis, cancel it from Nightingale's UI,
   and confirm (via the manifest `curl` above) that no files are left behind
   on the MacBook for that hash.
6. Regression: with `NIGHTINGALE_ANALYZER_MODE` unset, confirm local
   analysis still works exactly as before.
```

- [ ] **Step 2: Commit**

```bash
git add docs/remote-analyzer.md
git commit -m "docs: add remote analyzer operator runbook

Exact commands for MacBook setup/launch, Surface-side env vars, and
the manual verification checklist, split out from the design spec as
a standalone operator-facing doc."
```

---

### Task 13: Manual end-to-end verification on this MacBook

This is the "実際にMacBook側へ必要な依存関係をインストールして、既存Analyzerを起動できるところまで確認してください" deliverable — an operational task, not a code task. It has no automated test; its own output *is* the verification.

**Files:** none (operational only).

- [ ] **Step 1: Run the one-time setup from `docs/remote-analyzer.md`**

Follow the "MacBook 側" section exactly as written. This installs ffmpeg, Python 3.10, the venv, and all ML packages (several GB download — confirm there is enough disk space and a stable connection before starting; this can take a long time on residential bandwidth).

- [ ] **Step 2: Start the daemon bound to loopback first**

```bash
NIGHTINGALE_ANALYZER_BIND=127.0.0.1 \
NIGHTINGALE_ANALYZER_TOKEN=dev-test-token \
~/.nightingale/vendor/venv/bin/python \
~/.nightingale/vendor/analyzer/remote_server.py
```

Confirm the startup log line shows `device=mps` (Apple Silicon MPS detected, matching `detect_gpu()`'s existing macOS-arm64 branch) and the control/HTTP ports are listening.

- [ ] **Step 3: Point a local Nightingale build at it and analyze one song**

In a second terminal, with a short local test audio file available:

```bash
NIGHTINGALE_ANALYZER_MODE=remote \
NIGHTINGALE_ANALYZER_HOST=127.0.0.1 \
NIGHTINGALE_ANALYZER_TOKEN=dev-test-token \
cargo run -p server -- --bind 127.0.0.1:8081
```

Add the test song to the library, trigger analysis, and watch both terminals' logs. Confirm:
- The daemon's HTTP bridge log shows a `PUT /sources/<hash>...` request.
- The daemon's control log shows progress events.
- The song's cache directory (`~/.nightingale/cache` by default) gains `<hash>_vocals_*.mp3`, `<hash>_instrumental_*.mp3`, and `<hash>_transcript.json`.
- `curl -H "Authorization: Bearer dev-test-token" http://127.0.0.1:8788/results/<hash>/manifest` returns `{"files":[]}` (cleaned up after download).
- The song plays back with stems in the Nightingale UI.

- [ ] **Step 4: Report results**

Summarize what worked, what didn't, and any deviations from the plan (e.g. an actual package version conflict encountered during `step_install_packages`) back to the user — this step is exploratory verification, not a scripted pass/fail, so document findings rather than asserting success.
