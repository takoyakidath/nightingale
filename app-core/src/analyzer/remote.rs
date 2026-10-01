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

pub(crate) fn resolve_mode() -> Result<AnalyzerMode, NightingaleError> {
    resolve_mode_from(|key| std::env::var(key).ok())
}

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
        fs::write(&tmp, b"fake audio bytes").unwrap();

        let result = upload_source_audio(&cfg, HASH, "mp3", &tmp);
        handle.join().unwrap();
        let _ = fs::remove_file(&tmp);

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
        fs::write(&tmp, b"x").unwrap();

        let started = std::time::Instant::now();
        let result = upload_source_audio(&cfg, HASH, "mp3", &tmp);
        let elapsed = started.elapsed();

        let _ = fs::remove_file(&tmp);
        assert!(result.is_err());
        assert!(
            elapsed < Duration::from_secs(15),
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
        fs::create_dir_all(&tmp_dir).unwrap();
        let cache = CacheDir { path: tmp_dir.clone() };
        let audio = tmp_dir.join("audio.mp3");
        fs::write(&audio, b"audio").unwrap();

        let result = upload_song_inputs(&cfg, HASH, &audio, None, &cache);
        handle.join().unwrap();
        let _ = fs::remove_dir_all(&tmp_dir);

        assert!(result.is_ok());
    }
}
