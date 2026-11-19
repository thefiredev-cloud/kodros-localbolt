# Build guide: kodros-localbolt

## Requirements

- Linux and Python 3. Nothing to install.
- A model server with an OpenAI-compatible `/v1` API on this machine (or reachable on your network). Check that `GET <url>/models` lists the model id you will use.

## 1. Test

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -t .
```

80 tests. They cover path rules, versioning, restore, conflicts, atomic commits, concurrency, zip contents, the block parser at every chunk size, the HTTP API, Host and Origin rejection on both servers, preview isolation headers and traversal attempts, generation against a fake streaming model, truncated streams and the model-down state. No real model runs.

`tests/browser/` holds a manual probe (`isolation-probe.html`, `data.json`) that shows the preview sandbox in a real browser: `window.origin` is `null`, parent document, cookies and top navigation are blocked, `localStorage` is an in-memory shim, fetches to the UI origin and the internet are blocked by CSP.

## 2. Point it at a model

```bash
export LOCALBOLT_LLM_URL=http://127.0.0.1:8080/v1     # default
export LOCALBOLT_LLM_MODEL=<model id from GET /models>
export LOCALBOLT_THINK=1                              # optional, see below
```

Thinking is off by default (`chat_template_kwargs.enable_thinking=false` in the request). On the author's DeepSeek model the same prompt took 60 s with thinking off and 350 s with it on, of which 265 s was reasoning before the first file. The "Think first" box in the UI, `generate --think` and `LOCALBOLT_THINK=1` turn it on. Other model families may ignore that request field.

## 3. Run

```bash
bin/localbolt serve                                   # UI 8877, preview 8878
bin/localbolt status                                  # is the model answering? exit 3 if not
bin/localbolt generate "a pomodoro timer" --name Pomodoro
bin/localbolt generate "make it red" --project p-1234abcd
bin/localbolt list | files ID | versions ID | zip ID out.zip [--v N]
```

`contrib/localbolt.service` is a systemd user unit. It is staged, not installed: adjust `ExecStart` to your checkout, copy it to `~/.config/systemd/user/` and start it by hand.

Behind a gateway or reverse proxy, set `LOCALBOLT_GATEWAY_PREVIEW_PORT=<port where the gateway publishes the preview server>`. The UI then frames `http://<its own host>:<that port>` instead of `127.0.0.1:8878`, and its CSP `frame-src` allows `http://*:<that port>`. The gateway must list the UI origin in `frame-ancestors`. Unset means direct use only.

## How a generation works

1. You type a prompt. The server sends the model a system prompt plus, for follow-ups, every current file.
2. The model answers in blocks (`=== FILE: path ===` ... `=== END FILE ===`). A streaming parser writes each file as its block finishes, and the UI shows `wrote index.html (4.0 KB)` live.
3. All writes of one prompt commit as one version. A failed, cut-off or file-less answer commits nothing.
4. Every change (prompt, editor save, rename, delete, restore) is a new immutable version. Versions are content-addressed manifests. Restore creates a new version.
5. The preview iframe loads `http://127.0.0.1:8878/<project>/<head|vN>/index.html`. Any version can be previewed, zipped or restored.

## Security model

- Both servers bind 127.0.0.1 and answer 403 to any `Host` other than `127.0.0.1:<port>` or `localhost:<port>`. Mutating requests also need a matching `Origin` and no cross-site `Sec-Fetch-Site`.
- Project files are served only by the preview origin, read-only (GET and HEAD), never from the UI origin.
- Preview responses carry `Content-Security-Policy: default-src 'none'; connect-src 'self'; img-src 'self' data: blob:; ...; sandbox allow-scripts allow-forms allow-modals; frame-ancestors <UI origin>`. There is no `allow-same-origin`, no top navigation and no popups.
- The sandbox gives pages an opaque origin, so `localStorage` and `sessionStorage` would throw. The preview injects a small in-memory stand-in into served HTML. Stored files are untouched and state does not survive a reload.
- Model output is untrusted: paths outside the rules are skipped with a warning and nothing is executed.

## Model down

`/api/status` probes `GET /models` (3 s). When the model is unreachable or not serving the configured id, the badge says "Model unavailable", a banner gives the reason, Build is disabled and `POST .../generate` returns 503 `model_unavailable`. Nothing is invented and nothing goes to a cloud model. Projects, editor, versions, preview, zip and the starter template keep working.

## Layout

```text
localbolt/       server, store, llm client and parser, cli, static/ UI
tests/           80 tests, tests/browser manual sandbox probe
contrib/         systemd user unit
bin/localbolt    launcher
docs/architecture.svg
```
