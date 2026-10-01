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
