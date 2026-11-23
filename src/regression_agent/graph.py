"""The orchestrator: a LangGraph state machine over the 10 stages and 3 gates.

    ingest -> skills -> spec -> mapping -> [Gate 1] -> design -> [Gate 2]
           -> codegen -> envsetup -> [Gate 3] -> execute -> triage -> coverage

- Each stage is a node. Stages write their artifacts to the workspace; the
  graph state only carries small summaries, so every artifact stays reviewable.
- Gates pause the graph with `interrupt()`. `rga approve` resumes it; `rga redo`
  re-runs the stage before the gate (keeping any human edits a stage respects).
- A SQLite checkpointer saves the state after every node, so a crash or a
  Ctrl+C resumes at the first unfinished stage with no rework.
- state.json mirrors per-stage status in plain JSON for people and CI.
"""
from __future__ import annotations

import json
import sqlite3
import time
import traceback
from operator import or_
from typing import Annotated, Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .stages import codegen, coverage, design, envsetup, execute, ingest, mapping, skills, spec, triage
from .workspace import Workspace

GATES = {
    "skill_review": ("Gate 1: skill review", "skills_review.md", "mapping"),
    "test_spec_review": ("Gate 2: test-spec review", "e2e_tests.md", "design"),
    "code_env_review": ("Gate 3: code + .env review", "env_review.md", "codegen"),
}


class RunState(TypedDict, total=False):
    results: Annotated[dict, or_]   # stage name -> summary dict (merged across nodes)
    gates: Annotated[dict, or_]     # gate name -> decision
    mode: str                       # "claude" or "offline"


class StateFile:
    """Human-readable mirror of progress: output/state.json."""

    def __init__(self, ws: Workspace, stages: list[str], kind: str):
        self.path = ws.path("state.json")
        self.kind = kind
        self.stages = stages

    def load(self) -> dict:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            if data.get("kind") == self.kind and list(data.get("stages", {})) == self.stages:
                return data
        return {"kind": self.kind, "stages": {s: {"status": "pending"} for s in self.stages}}

    def set(self, stage: str, status: str, **extra) -> None:
        data = self.load()
        entry = data["stages"].setdefault(stage, {})
        entry.update(status=status, updated=time.strftime("%Y-%m-%d %H:%M:%S"), **extra)
        if status != "failed":
            entry.pop("error", None)
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    def reset(self) -> None:
        self.path.write_text(json.dumps({"kind": self.kind, "stages": {s: {"status": "pending"} for s in self.stages}},
                                        indent=2) + "\n")


def stage_node(name: str, fn: Callable[[], dict], state_file: StateFile):
    def node(state: RunState) -> dict:
        state_file.set(name, "running")
        try:
            result = fn()
        except Exception as e:
            state_file.set(name, "failed", error=f"{type(e).__name__}: {e}",
                           trace=traceback.format_exc(limit=3))
            raise
        state_file.set(name, "approved", result=result)
        return {"results": {name: result}}
    node.__name__ = name
    return node


def gate_node(gate: str, ws: Workspace, state_file: StateFile, auto_approve: bool):
    title, review, _ = GATES[gate]

    def node(state: RunState) -> dict:
        if gate not in ws.cfg.gates or auto_approve:
            decision = {"action": "approve", "by": "auto"}
        else:
            state_file.set(gate, "waiting_for_review", review=review)
            decision = interrupt({"gate": gate, "title": title, "review": str(ws.path(review))})
            if not isinstance(decision, dict) or decision.get("action") not in ("approve", "redo"):
                decision = {"action": "approve", "by": "human"}
        state_file.set(gate, "approved" if decision["action"] == "approve" else "redo",
                       decision=decision)
        return {"gates": {gate: decision}}
    node.__name__ = gate
    return node


def after_gate(gate: str, next_node: str):
    redo = GATES[gate][2]
    return lambda s: redo if s.get("gates", {}).get(gate, {}).get("action") == "redo" else next_node


FULL_ORDER = ["ingest", "skills", "spec", "mapping", "skill_review", "design", "test_spec_review",
              "codegen", "envsetup", "code_env_review", "execute", "triage", "coverage"]


def build_full_graph(ws: Workspace, llm, auto_approve: bool = False):
    sf = StateFile(ws, FULL_ORDER, "full")
    steps = {
        "ingest": lambda: ingest.run(ws),
        "skills": lambda: skills.run(ws, llm),
        "spec": lambda: spec.run(ws),
        "mapping": lambda: mapping.run(ws, llm),
        "design": lambda: design.run(ws, llm),
        "codegen": lambda: codegen.run(ws),
        "envsetup": lambda: envsetup.run(ws),
        "execute": lambda: execute.run(ws),
        "triage": lambda: triage.run(ws, llm),
        "coverage": lambda: coverage.run(ws),
    }
    g = StateGraph(RunState)
    for name in FULL_ORDER:
        if name in GATES:
            g.add_node(name, gate_node(name, ws, sf, auto_approve))
        else:
            g.add_node(name, stage_node(name, steps[name], sf))
    g.add_edge(START, FULL_ORDER[0])
    for a, b in zip(FULL_ORDER, FULL_ORDER[1:]):
        if a in GATES:
            g.add_conditional_edges(a, after_gate(a, b), [b, GATES[a][2]])
        else:
            g.add_edge(a, b)
    g.add_edge(FULL_ORDER[-1], END)
    return g, sf


def checkpointer(ws: Workspace) -> SqliteSaver:
    conn = sqlite3.connect(ws.state_dir / "checkpoints.db", check_same_thread=False)
    return SqliteSaver(conn)
