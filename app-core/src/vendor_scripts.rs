use std::path::Path;

const ANALYZE_PY: &str = include_str!("../analyzer/analyze.py");
const SERVER_PY: &str = include_str!("../analyzer/server.py");
const REMOTE_SERVER_PY: &str = include_str!("../analyzer/remote_server.py");
const PIPELINE_PY: &str = include_str!("../analyzer/pipeline.py");
const KEY_DETECT_PY: &str = include_str!("../analyzer/key_detect.py");
const STEMS_PY: &str = include_str!("../analyzer/stems.py");
const TRANSCRIBE_PY: &str = include_str!("../analyzer/transcribe.py");
const ALIGN_PY: &str = include_str!("../analyzer/align.py");
const CTC_ALIGN_PY: &str = include_str!("../analyzer/ctc_align.py");
const QWEN_ALIGN_PY: &str = include_str!("../analyzer/qwen_align.py");
const AUDIO_PY: &str = include_str!("../analyzer/audio.py");
const HALLUCINATION_PY: &str = include_str!("../analyzer/hallucination.py");
const LANGUAGE_PY: &str = include_str!("../analyzer/language.py");
const WHISPER_COMPAT_PY: &str = include_str!("../analyzer/whisper_compat.py");
const PARAKEET_PY: &str = include_str!("../analyzer/parakeet.py");
const GPU_PY: &str = include_str!("../analyzer/gpu.py");
const CJK_PY: &str = include_str!("../analyzer/cjk.py");

const FILES: &[(&str, &str)] = &[
    ("analyze.py", ANALYZE_PY),
    ("server.py", SERVER_PY),
    ("remote_server.py", REMOTE_SERVER_PY),
    ("pipeline.py", PIPELINE_PY),
    ("key_detect.py", KEY_DETECT_PY),
    ("stems.py", STEMS_PY),
    ("transcribe.py", TRANSCRIBE_PY),
    ("align.py", ALIGN_PY),
    ("ctc_align.py", CTC_ALIGN_PY),
    ("qwen_align.py", QWEN_ALIGN_PY),
    ("audio.py", AUDIO_PY),
    ("hallucination.py", HALLUCINATION_PY),
    ("language.py", LANGUAGE_PY),
    ("whisper_compat.py", WHISPER_COMPAT_PY),
    ("parakeet.py", PARAKEET_PY),
    ("gpu.py", GPU_PY),
    ("cjk.py", CJK_PY),
];

pub(crate) fn write_scripts(dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;

    for (name, content) in FILES {
        std::fs::write(dir.join(name), content)?;
    }

    Ok(())
}

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
