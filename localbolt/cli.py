"""localbolt command line: serve the UI, or drive projects from a terminal."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import llm
from . import store as st
from .server import Config, Servers


def _print_event(ev: dict) -> None:
    t = ev["type"]
    if t == "file":
        print(f"  wrote {ev['path']} ({ev['bytes']} bytes)")
    elif t == "delete":
        print(f"  deleted {ev['path']}")
    elif t == "warning":
        print(f"  warning: {ev['message']}")
    elif t == "done":
        g = ev["gen"]
        print(f"done: v{ev['version']} changed={ev['changed']} elapsed={g['elapsed_s']}s first_token={g['first_token_s']}s")
        if ev["summary"]:
            print("summary:", ev["summary"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="localbolt", description="Local prompt-to-web-app builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the UI (8877) and the preview origin (8878)")
    s.add_argument("--port", type=int, default=8877)
    s.add_argument("--preview-port", type=int, default=8878)
    sub.add_parser("status", help="is the local model answering?")
    sub.add_parser("list", help="list projects")
    n = sub.add_parser("new", help="create a project")
    n.add_argument("name")
    n.add_argument("--template", default="starter", choices=sorted(st.TEMPLATES))
    g = sub.add_parser("generate", help="generate or edit an app from a prompt")
    g.add_argument("prompt")
    g.add_argument("--project", help="project id; omitted creates a new project")
    g.add_argument("--name", help="name for the new project")
    g.add_argument("--think", action="store_true", help="let the model reason first (much slower, sometimes better)")
    f = sub.add_parser("files", help="list files of a project")
    f.add_argument("project")
    f.add_argument("--v", type=int)
    v = sub.add_parser("versions", help="list versions of a project")
    v.add_argument("project")
    z = sub.add_parser("zip", help="write a project zip")
    z.add_argument("project")
    z.add_argument("out")
    z.add_argument("--v", type=int)
    a = ap.parse_args(argv)

    store = st.Store()
    try:
        if a.cmd == "serve":
            srv = Servers(store, Config(app_port=a.port, preview_port=a.preview_port))
            print(f"localbolt UI      http://127.0.0.1:{srv.app_port}\nlocalbolt preview http://127.0.0.1:{srv.preview_port}  (separate origin)\ndata {store.root}", flush=True)
            srv.run()
        elif a.cmd == "status":
            s_ = llm.model_status(llm.ModelConfig())
            print(json.dumps(s_, indent=1))
            return 0 if s_["available"] else 3
        elif a.cmd == "list":
            for p in store.list_projects():
                print(f"{p['id']}  v{p['head']:<3} {p['updated']}  {p['name']}")
        elif a.cmd == "new":
            p = store.create_project(a.name, a.template)
            print(p["id"])
        elif a.cmd == "generate":
            pid = a.project
            if not pid:
                pid = store.create_project(a.name or a.prompt[:40], None)["id"]
                print("project", pid)
            cfg = llm.ModelConfig()
            status = llm.model_status(cfg)
            if not status["available"]:
                print(f"model unavailable: {status['reason']}. Nothing generated.", file=sys.stderr)
                return 3
            llm.generate(store, pid, a.prompt, cfg, _print_event, think=True if a.think else None)
        elif a.cmd == "files":
            for e in store.file_list(a.project, a.v):
                print(f"{e['size']:>8}  {e['path']}")
        elif a.cmd == "versions":
            for ver in store.versions(a.project):
                print(f"v{ver['n']:<3} {ver['created']}  {ver['kind']:<8} {ver['label']}")
        elif a.cmd == "zip":
            name, data = store.zip_bytes(a.project, a.v)
            Path(a.out).write_bytes(data)
            print(f"{a.out} ({name}, {len(data)} bytes)")
    except st.StoreError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 2
    except (llm.ModelUnavailable, llm.ModelError) as e:
        print(f"model error: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
