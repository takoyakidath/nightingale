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
