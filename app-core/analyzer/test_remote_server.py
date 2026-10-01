import json
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from remote_server import (
    Workdir,
    _make_progress_sink,
    handle_analyze,
    is_safe_hash,
    is_safe_result_filename,
    start_http_bridge,
)

HASH = "ab12" * 8  # 32 hex chars -- song.rs truncates blake3's hex to 32 chars


class SafetyChecksTest(unittest.TestCase):
    def test_is_safe_hash_accepts_32_lowercase_hex(self):
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

    def test_forwards_gpu_memory_hooks_when_given(self):
        # Review finding (Important #3): server.py's process_song() passes
        # pre_align_cleanup/free_gpu_fn to run_pipeline so demucs/whisper
        # memory is released between phases; remote mode omitted both,
        # raising peak unified memory on the Mac versus local mode.
        self.workdir.source_path(HASH, "mp3").write_bytes(b"audio")
        calls = []

        def fake_run_pipeline(audio_path, output_dir, file_hash, device, **kwargs):
            calls.append(kwargs)

        cleanup_fn = lambda: None  # noqa: E731
        free_gpu_fn = lambda: None  # noqa: E731

        handle_analyze(
            {"hash": HASH},
            self.workdir,
            "cpu",
            run_pipeline_fn=fake_run_pipeline,
            pre_align_cleanup=cleanup_fn,
            free_gpu_fn=free_gpu_fn,
        )

        self.assertIs(calls[0]["pre_align_cleanup"], cleanup_fn)
        self.assertIs(calls[0]["free_gpu_fn"], free_gpu_fn)

    def test_omits_gpu_memory_hooks_when_not_given(self):
        self.workdir.source_path(HASH, "mp3").write_bytes(b"audio")
        calls = []

        def fake_run_pipeline(audio_path, output_dir, file_hash, device, **kwargs):
            calls.append(kwargs)

        handle_analyze({"hash": HASH}, self.workdir, "cpu", run_pipeline_fn=fake_run_pipeline)

        self.assertNotIn("pre_align_cleanup", calls[0])
        self.assertNotIn("free_gpu_fn", calls[0])


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
        file_hash = "cd34" * 8
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

    def test_lyrics_route_lands_at_expected_cache_path(self):
        # Review finding (Important #6): only the audio PUT route had
        # HTTP-level coverage; the lyrics and transcript routes did not.
        file_hash = "1a2b" * 8
        self._request("PUT", f"/sources/{file_hash}/lyrics", body=b'{"lines":[]}')
        self.assertEqual(
            self.workdir.lyrics_path(file_hash).read_bytes(), b'{"lines":[]}'
        )

    def test_transcript_route_lands_at_expected_cache_path(self):
        file_hash = "3c4d" * 8
        self._request("PUT", f"/sources/{file_hash}/transcript", body=b'{"key":"A"}')
        self.assertEqual(
            self.workdir.transcript_path(file_hash).read_bytes(), b'{"key":"A"}'
        )

    def test_missing_token_is_rejected(self):
        file_hash = "ef56" * 8
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("PUT", f"/sources/{file_hash}?ext=mp3", body=b"x", token=None)
        self.assertEqual(ctx.exception.code, 401)

    def test_wrong_token_is_rejected(self):
        file_hash = "ef56" * 8
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request(
                "PUT", f"/sources/{file_hash}?ext=mp3", body=b"x", token="wrong"
            )
        self.assertEqual(ctx.exception.code, 401)

    def test_path_traversal_hash_is_rejected(self):
        # urlparse does not collapse ".." segments, so this literal path has
        # more segments than the /sources/<hash> route expects and falls
        # through to the generic 404 rather than the hash-specific 400 --
        # either way, no file is ever written because is_safe_hash() is
        # never even reached for a path shape that doesn't match a route.
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("PUT", "/sources/../../etc?ext=mp3", body=b"x")
        self.assertEqual(ctx.exception.code, 404)

    def test_path_traversal_result_filename_is_rejected(self):
        # Same reasoning as test_path_traversal_hash_is_rejected: the extra
        # ".." segments push this past the 3-part /results/<hash>/<name>
        # shape, so it 404s instead of 400ing -- the file is still never
        # read because the handler never reaches is_safe_result_filename().
        file_hash = "ef56" * 8
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._request("GET", f"/results/{file_hash}/../../etc/passwd")
        self.assertEqual(ctx.exception.code, 404)

    def test_two_segment_traversal_shapes_reach_the_safety_guards_and_400(self):
        # Review finding (Important #6): the existing traversal tests only
        # ever hit the generic 404 (route-shape mismatch), never the actual
        # is_safe_hash()/is_safe_result_filename() checks. These five shapes
        # keep the route's segment count intact, so they really do reach --
        # and are rejected by -- the guards themselves.
        file_hash = "5e6f" * 8
        cases = [
            ("PUT", "/sources/..?ext=mp3", None),
            ("PUT", "/sources/%2e%2e%2f%2e%2e%2fetc?ext=mp3", None),
            ("DELETE", "/work/..", None),
            ("PUT", f"/sources/{file_hash}?ext=../../x", b"x"),
            ("GET", f"/results/{file_hash}/%2e%2e%2fetc", None),
        ]
        for method, path, body in cases:
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._request(method, path, body=body)
                self.assertEqual(ctx.exception.code, 400)
        # None of these ever touched the filesystem.
        self.assertIsNone(self.workdir.find_source(file_hash))

    def test_incomplete_upload_body_is_rejected(self):
        # Review Focus: a connection dropped mid-upload must not leave a
        # silently truncated file on disk. urllib always sends the full body
        # it was given, so this needs a raw socket that claims a
        # Content-Length larger than what it actually sends.
        file_hash = "9a8b" * 8
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


import threading

from remote_server import bind_control_socket, handle_session, serve_control


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

    def test_session_times_out_on_an_idle_connection_instead_of_hanging_forever(self):
        # Review finding (Important #8): conn.settimeout(None) meant a
        # Surface that sleeps or drops off Wi-Fi without a clean TCP close
        # left the single-session daemon blocked indefinitely, needing a
        # manual restart. handle_session takes idle_timeout directly (not
        # through serve_control) so this test doesn't have to wait out the
        # real multi-minute production default.
        client_sock, server_sock = socket.socketpair()
        try:
            client_sock.sendall(
                (json.dumps({"type": "hello", "token": "tok"}) + "\n").encode("utf-8")
            )
            started = time.monotonic()
            with self.assertRaises(OSError):
                handle_session(server_sock, "tok", self.workdir, "cpu", idle_timeout=0.2)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2.0, "handle_session should time out quickly, not hang")
        finally:
            client_sock.close()
            server_sock.close()


class ProgressSinkTest(unittest.TestCase):
    # Review finding (Critical #2): on cancel, Rust shuts down its end of the
    # control socket, but nothing previously noticed on the daemon side --
    # _send() swallowed the resulting BrokenPipeError, so the pipeline ran to
    # completion unobserved, writing files after the client had already
    # deleted the remote scratch directory, and leaving the single-session
    # daemon's next accept() blocked for a full HANDSHAKE_TIMEOUT. The
    # progress sink must raise instead, so run_pipeline unwinds promptly.

    def test_raises_when_connection_is_closed(self):
        class DeadFile:
            def write(self, _data):
                raise BrokenPipeError("simulated dead connection")

            def flush(self):
                pass

        sink = _make_progress_sink(DeadFile())
        with self.assertRaises(BrokenPipeError):
            sink(50, "halfway")

    def test_sends_when_connection_is_open(self):
        import io

        buf = io.StringIO()
        sink = _make_progress_sink(buf)
        sink(42, "working")
        self.assertEqual(
            json.loads(buf.getvalue()), {"type": "progress", "pct": 42, "msg": "working"}
        )


if __name__ == "__main__":
    unittest.main()
