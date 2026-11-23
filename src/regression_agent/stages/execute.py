"""Stage 8: test execution.

Four pre-flight checks must pass before anything runs live:
  1. every generated test file compiles;
  2. the gRPC stubs compile and contain every method the tests call;
  3. .env has a value for every variable the tests read;
  4. the target answers (it is started first if the config names a launcher).
Then pytest runs the generated suite. The harness writes one evidence file per
test (every request, response and status), which stage 9 triages.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import grpc

from ..protoc import compile_proto
from ..workspace import Workspace
from .envsetup import env_vars_used, read_env


class PreflightError(RuntimeError):
    pass


def start_target(ws: Workspace):
    """Returns (address, stop_fn)."""
    t = ws.cfg.target
    if t.address:
        return t.address, lambda: None
    if not t.launcher:
        raise PreflightError("pipeline_config.json needs target.address or target.launcher")
    module, func = t.launcher.split(":")
    root = str(ws.cfg.config_path.parent) if ws.cfg.config_path else os.getcwd()
    if root not in sys.path:
        sys.path.insert(0, root)
    fn = getattr(importlib.import_module(module), func)
    kwargs = dict(t.launcher_args)
    if "proto" in inspect.signature(fn).parameters:
        kwargs.setdefault("proto", str(ws.cfg.proto_path))
    server, port, *_ = fn(**kwargs)
    return f"127.0.0.1:{port}", lambda: server.stop(0)


def preflight(ws: Workspace, tests: list[dict]) -> list[dict]:
    checks = []
    files = sorted(ws.tests_dir.glob("test_*.py"))
    bad = []
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(f"{f.name}: {e.msg}")
    checks.append({"check": "generated tests compile", "ok": not bad and bool(files),
                   "detail": "; ".join(bad) or f"{len(files)} files"})

    try:
        pb2, _ = compile_proto(ws.cfg.proto_path)
        services = pb2.DESCRIPTOR.services_by_name
        missing = sorted({st["method"] for t in tests for st in t["steps"]
                          if st["method"].split(".")[0] not in services
                          or st["method"].split(".")[1] not in services[st["method"].split(".")[0]].methods_by_name})
        checks.append({"check": "stubs contain every called method", "ok": not missing,
                       "detail": ", ".join(missing) or f"{len(services)} services"})
    except Exception as e:  # noqa: BLE001 - any protoc/import failure fails the check
        checks.append({"check": "stubs contain every called method", "ok": False, "detail": str(e)})

    env = read_env(ws)
    needed = env_vars_used(sorted(ws.tests_dir.glob("*.py")))
    empty = [n for n in needed if not env.get(n) and n != "RGA_RESET_RPC"]
    checks.append({"check": ".env complete", "ok": not empty, "detail": ", ".join(empty) or f"{len(needed)} vars"})
    return checks


def run(ws: Workspace) -> dict:
    tests = ws.read_json("e2e_tests.json")
    checks = preflight(ws, tests)
    address, stop = None, lambda: None
    if all(c["ok"] for c in checks):
        try:
            address, stop = start_target(ws)
            grpc.channel_ready_future(grpc.insecure_channel(address)).result(timeout=10)
            checks.append({"check": "target reachable", "ok": True, "detail": address})
        except Exception as e:  # noqa: BLE001
            checks.append({"check": "target reachable", "ok": False, "detail": str(e) or type(e).__name__})
    ws.write_json("preflight.json", checks)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        stop()
        raise PreflightError("pre-flight failed: " + "; ".join(f"{c['check']} ({c['detail']})" for c in failed))

    try:
        if ws.logs_dir.exists():
            shutil.rmtree(ws.logs_dir)
        ws.logs_dir.mkdir(parents=True)
        env = {**os.environ, **read_env(ws), "RGA_TARGET": address, "RGA_EVIDENCE_DIR": str(ws.logs_dir)}
        src = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = os.pathsep.join([src, env.get("PYTHONPATH", "")]).strip(os.pathsep)
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(ws.tests_dir), "-q", "-p", "no:cacheprovider",
             "--rootdir", str(ws.tests_dir), "-o", "addopts=", "--junitxml", str(ws.path("junit.xml"))],
            cwd=ws.root, env=env, capture_output=True, text=True, timeout=1800)
    finally:
        stop()
    ws.write_text("pytest_output.txt", proc.stdout + proc.stderr)

    results = {}
    for t in tests:
        ev = ws.logs_dir / f"{t['id']}.evidence.json"
        if ev.exists():
            data = json.loads(ev.read_text())
            results[t["id"]] = {"outcome": data["outcome"], "file": t.get("file"), "failure": data["failure"]}
        else:
            results[t["id"]] = {"outcome": "not_run", "file": t.get("file"),
                                "failure": {"kind": "harness", "message": "no evidence written (test did not run)"}}
    summary = {"total": len(results), "passed": sum(r["outcome"] == "passed" for r in results.values()),
               "failed": sum(r["outcome"] == "failed" for r in results.values()),
               "not_run": sum(r["outcome"] == "not_run" for r in results.values()),
               "pytest_exit_code": proc.returncode}
    ws.write_json("execution_results.json", {"summary": summary, "tests": results})
    return summary
