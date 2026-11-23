import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "false")
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")


def make_config(tmp: Path, docs="docs", proto="shelfwise.proto", fixed=False, **target) -> Path:
    ws = tmp / "ws"
    if not (ws / "docs").exists():
        shutil.copytree(ROOT / "sample_service" / docs, ws / "docs")
        shutil.copy(ROOT / "sample_service" / "protos" / proto, ws / "shelfwise.proto")
    cfg = {
        "platform_name": "Shelfwise (test)", "docs_path": "ws/docs", "proto_path": "ws/shelfwise.proto",
        "client_path": str(ROOT / "sample_service" / "client.py"), "output_dir": "ws/output",
        "llm": {"provider": "offline"},
        "target": {"launcher": "sample_service.server:start_server", "launcher_args": {"fixed": fixed},
                   "reset_rpc": "TestSupportService.Reset", **target},
    }
    path = tmp / "pipeline_config.json"
    path.write_text(json.dumps(cfg))
    return path


def rga(*args, cwd=None):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)])}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.run([sys.executable, "-m", "regression_agent.cli", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=600)


class FakeLLM:
    """Scripted stand-in for ClaudeLLM: answers by task prefix and records every call."""

    name = "fake"

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def structured(self, *, task, system, prompt, schema, effort=None):
        self.calls.append({"task": task, "prompt": prompt, "schema": schema})
        for prefix, answer in self.answers.items():
            if task.startswith(prefix):
                return answer(task, prompt, schema) if callable(answer) else answer
        raise AssertionError(f"unexpected LLM task {task}")


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)
