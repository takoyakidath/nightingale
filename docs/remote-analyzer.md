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
