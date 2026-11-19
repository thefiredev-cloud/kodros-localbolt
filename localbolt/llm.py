"""Local model client, prompt building and the streaming file-block parser.

Only the local OpenAI-compatible endpoint is ever contacted (default
http://127.0.0.1:8080/v1, model id from LOCALBOLT_LLM_MODEL). If it is down, callers get
a ModelUnavailable error: nothing is invented and nothing goes to a cloud model.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Callable, Iterator

from . import store as st

DEFAULT_URL = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "local-model"  # set LOCALBOLT_LLM_MODEL to an id listed by the server's /models
READ_TIMEOUT_S = 180.0  # per read on the stream; the model can think for a while before the first token
MAX_TOKENS = 32000  # reasoning tokens count against this
CONTEXT_FILE_BUDGET = 90_000  # characters of current project files sent with a follow-up


class ModelUnavailable(Exception):
    pass


class ModelError(Exception):
    pass


@dataclass
class ModelConfig:
    url: str = field(default_factory=lambda: os.environ.get("LOCALBOLT_LLM_URL", DEFAULT_URL).rstrip("/"))
    model: str = field(default_factory=lambda: os.environ.get("LOCALBOLT_LLM_MODEL", DEFAULT_MODEL))
    read_timeout: float = READ_TIMEOUT_S
    max_tokens: int = MAX_TOKENS
    # Reasoning models can think for minutes before writing a whole app. Off by default; the UI can turn it on per request.
    think: bool = field(default_factory=lambda: os.environ.get("LOCALBOLT_THINK", "") == "1")


def model_status(cfg: ModelConfig, timeout: float = 3.0) -> dict:
    """Cheap liveness probe: GET /models and look for the configured model id."""
    try:
        with urllib.request.urlopen(cfg.url + "/models", timeout=timeout) as r:
            data = json.loads(r.read(1 << 20))
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        return {"available": False, "model": cfg.model, "url": cfg.url, "reason": f"{type(e).__name__}: {reason}"}
    if cfg.model not in ids:
        return {"available": False, "model": cfg.model, "url": cfg.url, "reason": f"model not served; endpoint lists {ids or 'nothing'}"}
    return {"available": True, "model": cfg.model, "url": cfg.url, "reason": None}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are LocalBolt, a senior front-end engineer who builds small, polished web apps from a short description.

STACK RULES
- Plain HTML, CSS and JavaScript only. No build step, no npm, no frameworks, no TypeScript, no CDN, no external URLs, no web fonts, no remote images. The app must work offline from its own files.
- Entry point is index.html. Use relative paths such as style.css and app.js. Keep it to a handful of files.
- The app runs in a sandboxed preview: no network access, and localStorage, sessionStorage, cookies and IndexedDB may be unavailable. Keep state in memory and wrap any storage use in try/catch.
- Allowed file types: .html .css .js .json .svg .md .txt. Use only letters, digits, '_', '-' and '.' in file names.
- Write complete, working code. No placeholders, no TODO stubs, no "rest of the file here".

QUALITY BAR
- It must look designed: a clear visual hierarchy, consistent spacing scale, a restrained palette with good contrast, system font stack, responsive layout, visible focus states, sensible empty states.
- Semantic HTML, labelled controls, keyboard operable. Handle edge cases and bad input.
- Draw icons and illustrations with inline SVG or CSS, never remote assets.

OUTPUT FORMAT (strict)
For every file you create or change, output the FULL new content of that file in a block:

=== FILE: path/to/file.ext ===
<file content, nothing else>
=== END FILE ===

To remove a file, output a line:
=== DELETE: path/to/file.ext ===

Finish with exactly one line:
SUMMARY: <one or two plain sentences saying what you built or changed>

Do not wrap file content in markdown code fences. Do not output any text between blocks other than the SUMMARY line. When editing an existing project, output only the files you changed, each in full."""

_FILE_RE = re.compile(r"^=== FILE: (.+?) ===\s*$")
_END_RE = re.compile(r"^=== END FILE ===\s*$")
_DEL_RE = re.compile(r"^=== DELETE: (.+?) ===\s*$")
_SUM_RE = re.compile(r"^SUMMARY:\s*(.*)$")


def build_messages(
    prompt: str,
    files: dict[str, str],
    history: list[dict] | None = None,
    budget: int = CONTEXT_FILE_BUDGET,
) -> list[dict]:
    """System prompt, short prior turns, then the request with the current files attached."""
    msgs: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-6:]:
        if h.get("role") == "user" and h.get("text"):
            msgs.append({"role": "user", "content": h["text"][:2000]})
        elif h.get("role") == "assistant" and h.get("text"):
            msgs.append({"role": "assistant", "content": "SUMMARY: " + h["text"][:1000]})
    if not files:
        body = f"Build this app:\n\n{prompt}"
    else:
        parts = ["The project currently contains these files.\n"]
        used = 0
        omitted = []
        for path in sorted(files, key=lambda p: (p != "index.html", len(files[p]), p)):
            text = files[path]
            if used + len(text) > budget:
                omitted.append(f"{path} ({len(text)} chars)")
                continue
            used += len(text)
            parts.append(f"=== CURRENT FILE: {path} ===\n{text}\n=== END CURRENT FILE ===\n")
        if omitted:
            parts.append("Not shown because of size (do not overwrite these unless asked): " + ", ".join(omitted) + "\n")
        parts.append(f"Change request:\n\n{prompt}")
        body = "\n".join(parts)
    msgs.append({"role": "user", "content": body})
    return msgs


# ---------------------------------------------------------------------------
# Streaming parser for the FILE block format
# ---------------------------------------------------------------------------
class BlockParser:
    """Incremental line parser. feed() takes arbitrary text chunks and yields events.

    Events: ("file_start", path), ("file", path, content), ("delete", path),
    ("summary", text), ("incomplete", path). Prose outside blocks is ignored.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._path: str | None = None
        self._lines: list[str] = []

    def feed(self, chunk: str) -> Iterator[tuple]:
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            yield from self._line(line.rstrip("\r"))

    def finish(self) -> Iterator[tuple]:
        if self._buf:
            line, self._buf = self._buf, ""
            yield from self._line(line.rstrip("\r"))
        if self._path is not None:
            path, self._path, self._lines = self._path, None, []
            yield ("incomplete", path)

    def _line(self, line: str) -> Iterator[tuple]:
        if self._path is None:
            if m := _FILE_RE.match(line):
                self._path, self._lines = m.group(1).strip(), []
                yield ("file_start", self._path)
            elif m := _DEL_RE.match(line):
                yield ("delete", m.group(1).strip())
            elif m := _SUM_RE.match(line):
                yield ("summary", m.group(1).strip())
            return
        if _END_RE.match(line):
            path, lines, self._path, self._lines = self._path, self._lines, None, []
            yield ("file", path, _finish_content(lines))
        else:
            self._lines.append(line)


def _finish_content(lines: list[str]) -> str:
    # Models sometimes fence the whole file despite instructions: unwrap one outer fence.
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# Chat streaming
# ---------------------------------------------------------------------------
def stream_chat(cfg: ModelConfig, messages: list[dict], temperature: float = 0.4) -> Iterator[tuple[str, str]]:
    """Yield ("reasoning"|"content", text) and finally ("finish", reason)."""
    body = json.dumps({
        "model": cfg.model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "max_tokens": cfg.max_tokens,
        "chat_template_kwargs": {"enable_thinking": cfg.think},
    }).encode()
    req = urllib.request.Request(cfg.url + "/chat/completions", data=body, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=cfg.read_timeout)
    except urllib.error.HTTPError as e:
        detail = e.read(2000).decode("utf-8", "replace")
        e.close()
        if e.code in (502, 503, 504):
            raise ModelUnavailable(f"HTTP {e.code} from model endpoint: {detail[:200]}") from e
        raise ModelError(f"HTTP {e.code} from model endpoint: {detail[:200]}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ModelUnavailable(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e
    finish = None
    complete = False
    try:
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    complete = True
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if "error" in obj:
                    raise ModelError(str(obj["error"])[:300])
                for ch in obj.get("choices", []):
                    delta = ch.get("delta") or {}
                    if delta.get("reasoning_content"):
                        yield "reasoning", delta["reasoning_content"]
                    if delta.get("content"):
                        yield "content", delta["content"]
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
                        complete = True
    except (OSError, TimeoutError, ValueError, http.client.HTTPException) as e:
        raise ModelUnavailable(f"stream broke: {type(e).__name__}: {e}") from e
    if not complete:
        raise ModelUnavailable("the model connection closed before it finished")
    yield "finish", finish or "stop"


# ---------------------------------------------------------------------------
# Generation: model -> parsed file ops -> one new version
# ---------------------------------------------------------------------------
Emit = Callable[[dict], None]


def generate(
    store: st.Store,
    pid: str,
    prompt: str,
    cfg: ModelConfig,
    emit: Emit,
    think: bool | None = None,
    chunks: Callable[[ModelConfig, list[dict]], Iterator[tuple[str, str]]] | None = None,
) -> dict:
    """Run one generation and commit it as a single version. Raises on failure, commits nothing."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise st.StoreError("bad_prompt", "prompt is empty")
    if len(prompt) > 8000:
        raise st.StoreError("bad_prompt", "prompt is longer than 8000 characters", 413)
    chunks = chunks or stream_chat
    if think is not None:
        cfg = replace(cfg, think=think)
    head = store.get_project(pid)["head"]
    current = {p: store.read_file(pid, p, head) for p in store.files(pid, head)} if head else {}
    history = store.chat(pid, 12)
    messages = build_messages(prompt, current, history)
    store.append_chat(pid, "user", prompt, base=head)

    t0 = time.monotonic()
    first = None
    reasoning_chars = content_chars = 0
    parser = BlockParser()
    writes: dict[str, str] = {}
    deletes: list[str] = []
    skipped: list[str] = []
    summary = ""
    finish = "stop"
    emit({"type": "status", "state": "waiting", "base": head})

    def handle(events: Iterator[tuple]) -> None:
        nonlocal summary
        for ev in events:
            kind = ev[0]
            if kind == "file_start":
                emit({"type": "file_start", "path": ev[1]})
            elif kind == "file":
                try:
                    path = st.normalize_path(ev[1])
                except st.StoreError as e:
                    skipped.append(f"{ev[1]}: {e.message}")
                    emit({"type": "warning", "message": f"skipped {ev[1]}: {e.message}"})
                    continue
                writes[path] = ev[2]
                emit({"type": "file", "path": path, "bytes": len(ev[2].encode())})
            elif kind == "delete":
                try:
                    deletes.append(st.normalize_path(ev[1]))
                    emit({"type": "delete", "path": deletes[-1]})
                except st.StoreError as e:
                    skipped.append(f"{ev[1]}: {e.message}")
            elif kind == "summary":
                summary = ev[1]
            elif kind == "incomplete":
                skipped.append(f"{ev[1]}: cut off before END FILE")
                emit({"type": "warning", "message": f"{ev[1]} was cut off and was not saved"})

    it = chunks(cfg, messages)
    try:
        last_tick = 0.0
        for kind, text in it:
            now = time.monotonic()
            if kind == "finish":
                finish = text
                continue
            if first is None:
                first = now - t0
            if kind == "reasoning":
                reasoning_chars += len(text)
            else:
                content_chars += len(text)
                handle(parser.feed(text))
            if now - last_tick > 0.5:
                last_tick = now
                emit({"type": "progress", "elapsed": round(now - t0, 1), "reasoning_chars": reasoning_chars, "content_chars": content_chars})
        handle(parser.finish())
    except (ModelUnavailable, ModelError) as e:
        msg = f"{type(e).__name__}: {e}"
        store.append_chat(pid, "error", msg)
        raise
    finally:
        close = getattr(it, "close", None)
        if close:
            close()

    elapsed = time.monotonic() - t0
    if finish == "length":
        msg = "model hit its token limit before finishing; nothing was saved. Try a smaller request."
        store.append_chat(pid, "error", msg)
        raise ModelError(msg)
    if not writes and not deletes:
        msg = "model returned no files; nothing was saved."
        store.append_chat(pid, "error", msg)
        raise ModelError(msg)
    gen = {
        "model": cfg.model,
        "think": cfg.think,
        "elapsed_s": round(elapsed, 2),
        "first_token_s": round(first, 2) if first is not None else None,
        "reasoning_chars": reasoning_chars,
        "content_chars": content_chars,
        "finish": finish,
        "skipped": skipped,
    }
    label = (summary or f"Updated {len(writes)} file(s)")[:200]
    try:
        res = store.write_files(
            pid, writes, deletes=deletes, label=label, kind="generate" if not head else "edit",
            prompt=prompt, summary=summary or None, gen=gen,
        )
    except st.StoreError as e:
        store.append_chat(pid, "error", e.message)
        raise
    store.append_chat(pid, "assistant", summary or label, version=res["version"], changed=res["changed"])
    out = {"type": "done", "version": res["version"], "changed": res["changed"], "summary": summary, "unchanged": res["unchanged"], "gen": gen}
    emit(out)
    return out
