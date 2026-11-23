"""rga: command line for ai-regression-agent.

    rga run      [-c pipeline_config.json] [--auto-approve] [--offline]   start or resume the full pipeline
    rga diff     [-c ...] [--auto-approve] [--offline]                    diff-runner after the docs change
    rga approve  [-c ...] [--note "..."]                                  approve the gate the run is paused at
    rga redo     [-c ...]                                                 re-run the stage before the paused gate
    rga status   [-c ...]                                                 per-stage status (state.json)
    rga reset    [-c ...]                                                 forget the current run (keeps artifacts)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")  # pytest runs in a subprocess
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

from langgraph.types import Command  # noqa: E402

from .config import PipelineConfig  # noqa: E402
from .diff_runner import build_diff_graph  # noqa: E402
from .graph import build_full_graph, checkpointer  # noqa: E402
from .llm import get_llm  # noqa: E402
from .workspace import Workspace  # noqa: E402


def _thread_file(ws):
    return ws.state_dir / "thread.json"


def _current_thread(ws) -> dict | None:
    p = _thread_file(ws)
    return json.loads(p.read_text()) if p.exists() else None


def _open(args, kind: str | None = None):
    cfg = PipelineConfig.load(args.config)
    if getattr(args, "offline", False):
        cfg.llm.provider = "offline"
    ws = Workspace(cfg)
    thread = _current_thread(ws)
    kind = kind or (thread or {}).get("kind", "full")
    mode = (thread or {}).get("mode") if thread and not getattr(args, "offline", False) else None
    if mode == "offline":
        cfg.llm.provider = "offline"
    llm = get_llm(cfg.llm, ws.path("llm_calls.jsonl"))
    build = build_full_graph if kind == "full" else build_diff_graph
    graph, state_file = build(ws, llm, auto_approve=getattr(args, "auto_approve", False))
    app = graph.compile(checkpointer=checkpointer(ws))
    return ws, llm, app, state_file


def _drive(app, payload, config) -> int:
    """Streams node updates; returns 0 when finished, 3 when paused at a gate."""
    for update in app.stream(payload, config, stream_mode="updates"):
        for node, value in update.items():
            if node == "__interrupt__":
                info = value[0].value
                print(f"\n⏸  {info['title']}\n   Review: {info['review']}\n"
                      f"   Then run `rga approve` (or `rga redo` to regenerate this stage).")
                return 3
            res = (value or {}).get("results", {}).get(node)
            gate = (value or {}).get("gates", {}).get(node)
            if res is not None:
                print(f"✓ {node:16} {json.dumps(res)}")
            elif gate is not None:
                print(f"✓ {node:16} {gate['action']} ({gate.get('by', 'human')})")
    return 0


def _start_or_resume(args, kind: str) -> int:
    cfg = PipelineConfig.load(args.config)
    ws = Workspace(cfg)
    thread = _current_thread(ws)
    if kind == "diff" and not ws.path("manifest.json").exists():
        print("No previous run in this workspace; run `rga run` first.", file=sys.stderr)
        return 2
    fresh = thread is None or thread["kind"] != kind or thread.get("done")
    if fresh:
        mode = "offline" if args.offline else None
        thread = {"id": f"{kind}-{uuid.uuid4().hex[:8]}", "kind": kind, "done": False, "mode": mode}
        _thread_file(ws).write_text(json.dumps(thread))
    ws, llm, app, sf = _open(args, kind)
    config = {"configurable": {"thread_id": thread["id"]}}
    if fresh:
        sf.reset()
        print(f"{'Pipeline' if kind == 'full' else 'diff-runner'} for {ws.cfg.platform_name} · "
              f"LLM: {'Claude (' + ws.cfg.llm.model + ')' if llm else 'offline heuristics'} · output: {ws.root}")
        payload = {"results": {}, "gates": {}, "mode": "claude" if llm else "offline"}
    else:
        print(f"Resuming {thread['id']} (next: {', '.join(app.get_state(config).next) or 'nothing'})")
        payload = None
    return _finish(ws, app, payload, config, thread)


def _finish(ws, app, payload, config, thread) -> int:
    try:
        code = _drive(app, payload, config)
    except Exception as e:  # noqa: BLE001 - report and keep the checkpoint for resume
        print(f"\n✗ {type(e).__name__}: {e}\n  Fix the cause and run the same command again to resume.",
              file=sys.stderr)
        return 1
    if code == 0:
        thread["done"] = True
        _thread_file(ws).write_text(json.dumps(thread))
        print(f"\nDone. Coverage: {ws.path('coverage_map.md')}")
        bugs = sorted(ws.bugs_dir.glob("*.md"))
        if bugs:
            print(f"Bug reports ({len(bugs)}): {ws.bugs_dir}")
    return code


def cmd_resume_gate(args, action: str) -> int:
    cfg = PipelineConfig.load(args.config)
    thread = _current_thread(Workspace(cfg))
    if not thread or thread.get("done"):
        print("Nothing is waiting for review.", file=sys.stderr)
        return 2
    ws, llm, app, sf = _open(args)
    config = {"configurable": {"thread_id": thread["id"]}}
    state = app.get_state(config)
    if not any(t.interrupts for t in state.tasks):
        print("The run is not paused at a gate; use `rga run` to resume it.", file=sys.stderr)
        return 2
    decision = {"action": action, "by": "human", "note": getattr(args, "note", "") or ""}
    return _finish(ws, app, Command(resume=decision), config, thread)


def cmd_status(args) -> int:
    ws = Workspace(PipelineConfig.load(args.config))
    p = ws.path("state.json")
    if not p.exists():
        print("No run yet.")
        return 0
    data = json.loads(p.read_text())
    print(f"{data['kind']} run · {ws.root}")
    for name, st in data["stages"].items():
        extra = st.get("error") or (json.dumps(st["result"]) if st.get("result") else st.get("review", ""))
        print(f"  {name:16} {st['status']:20} {extra}")
    return 0


def cmd_reset(args) -> int:
    ws = Workspace(PipelineConfig.load(args.config))
    _thread_file(ws).unlink(missing_ok=True)
    (ws.state_dir / "checkpoints.db").unlink(missing_ok=True)
    ws.path("state.json").unlink(missing_ok=True)
    print("Run state cleared (artifacts kept).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rga", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("run", "diff", "approve", "redo", "status", "reset"):
        s = sub.add_parser(name)
        s.add_argument("-c", "--config", default="pipeline_config.json")
        if name in ("run", "diff"):
            s.add_argument("--auto-approve", action="store_true", help="don't pause at the review gates (CI)")
            s.add_argument("--offline", action="store_true", help="don't call Claude; use the heuristics")
        if name == "approve":
            s.add_argument("--note", default="")
    a = p.parse_args(argv)
    if a.cmd == "run":
        return _start_or_resume(a, "full")
    if a.cmd == "diff":
        return _start_or_resume(a, "diff")
    if a.cmd in ("approve", "redo"):
        return cmd_resume_gate(a, "approve" if a.cmd == "approve" else "redo")
    if a.cmd == "status":
        return cmd_status(a)
    return cmd_reset(a)


if __name__ == "__main__":
    sys.exit(main())
