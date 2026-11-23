"""The Claude code paths, driven by a scripted fake (no network, no API key)."""
import json
from types import SimpleNamespace

import pytest

from regression_agent.config import LLMConfig, PipelineConfig
from regression_agent.llm import ClaudeLLM, LLMError, get_llm
from regression_agent.stages import design, ingest, mapping, skills, spec, triage
from regression_agent.workspace import Workspace

from conftest import FakeLLM


class FakeMessages:
    def __init__(self, text, stop="end_turn"):
        self.text, self.stop, self.kwargs = text, stop, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop, model=kwargs["model"],
                               content=[SimpleNamespace(type="text", text=self.text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0))


def test_claude_request_shape_and_parsing(tmp_path):
    msgs = FakeMessages('{"ok": true}')
    client = SimpleNamespace(beta=SimpleNamespace(messages=msgs), messages=msgs)
    llm = ClaudeLLM(LLMConfig(), tmp_path / "calls.jsonl", client=client)
    out = llm.structured(task="t", system="sys", prompt="p", schema={"type": "object"}, effort="medium")
    assert out == {"ok": True}
    k = msgs.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["fallbacks"] == "default" and k["betas"] == ["server-side-fallback-2026-07-01"]
    assert k["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert k["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert json.loads((tmp_path / "calls.jsonl").read_text())["output_tokens"] == 5


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_claude_bad_stop_reasons_raise(stop):
    msgs = FakeMessages("{}", stop=stop)
    llm = ClaudeLLM(LLMConfig(), None, client=SimpleNamespace(beta=SimpleNamespace(messages=msgs)))
    with pytest.raises(LLMError):
        llm.structured(task="t", system="s", prompt="p", schema={})


def test_offline_selected_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert get_llm(LLMConfig(provider="auto")) is None
    assert get_llm(LLMConfig(provider="offline")) is None


def test_llm_skill_extraction_keeps_doc_text_and_never_drops(config):
    ws = Workspace(PipelineConfig.load(config))
    ingest.run(ws)

    def answer(task, prompt, schema):
        if "lending.md" in task:  # model forgets REQ-028 and invents a requirement
            return {"skills": [{"id": "lending.book.checkout", "description": "borrow", "actor": "member",
                                "preconditions": [], "requirements": ["REQ-020", "REQ-021", "REQ-022", "REQ-999"]},
                               {"id": "lending.book.return", "description": "return", "actor": "member",
                                "preconditions": [], "requirements": ["REQ-023", "REQ-024"]},
                               {"id": "lending.loan.renew", "description": "renew", "actor": "member",
                                "preconditions": [], "requirements": ["REQ-025", "REQ-026"]},
                               {"id": "lending.loan.list", "description": "list", "actor": "member",
                                "preconditions": [], "requirements": ["REQ-027"]}], "excluded": []}
        return {"skills": [], "excluded": []}
    llm = FakeLLM({"skills:": answer})
    out, excluded = skills.extract_from_docs(ws, llm, names=["lending.md"])
    reqs = {r for s in out for r in s["requirements"]}
    assert "REQ-999" not in reqs           # invented ID ignored
    assert "REQ-028" in reqs               # forgotten one recovered by the heuristic
    co = next(s for s in out if s["id"] == "lending.book.checkout")
    assert co["rules"][0]["text"].startswith("An active member can check out")  # text from the doc, not the model


def test_llm_mapping_can_only_pick_real_candidates(config):
    ws = Workspace(PipelineConfig.load(config))
    ingest.run(ws)
    skills.run(ws, None)
    spec.run(ws)
    sk = [s for s in ws.read_json("skills.json") if s["id"] == "testing.service.reset"]
    llm = FakeLLM({"map:": lambda t, p, schema: {"method": schema["properties"]["method"]["enum"][0],
                                                   "confidence": 0.9, "reasoning": "r"}})
    out = mapping.map_skills(sk, ws.read_json("grpc_spec.json"), llm)
    enum = llm.calls[0]["schema"]["properties"]["method"]["enum"]
    assert out["testing.service.reset"]["method"] in enum and out["testing.service.reset"]["match_method"] == "llm"
    assert "TestSupportService.Reset" in enum


def _ws_ready(config):
    ws = Workspace(PipelineConfig.load(config))
    ingest.run(ws)
    skills.run(ws, None)
    spec.run(ws)
    mapping.run(ws, None)
    return ws


def test_llm_design_repairs_invalid_tests_once(config):
    ws = _ws_ready(config)
    good_step = {"skill": "catalog.book.create", "request": [{"field": "title", "value_json": '"Dune"'},
                 {"field": "isbn", "value_json": '"1"'}, {"field": "total_copies", "value_json": "1"}],
                 "save_as": "book", "expect_code": "OK",
                 "expect_fields": [{"path": "available_copies", "value_json": "1"}]}
    bad_step = dict(good_step, request=[{"field": "colour", "value_json": '"red"'}])
    first = {"tests": [{"title": "add", "requirements": ["REQ-001"], "confidence": 90, "rationale": "r",
                        "steps": [bad_step]}]}
    fixed = {"tests": [{"title": "add", "requirements": ["REQ-001"], "confidence": 90, "rationale": "r",
                        "steps": [good_step]}]}
    llm = FakeLLM({"design-fix:": fixed, "design:": first})
    tests, gaps = design.design(ws, llm, skill_ids={"catalog.book.create"})
    assert len(tests) == 1 and tests[0]["steps"][0]["request"]["title"] == "Dune"
    assert "colour" in llm.calls[1]["prompt"]  # the validator's errors were sent back
    assert {g["requirement"] for g in gaps} == {"REQ-002", "REQ-003"}  # uncovered ones are reported


def test_llm_triage_only_for_ambiguous(config):
    ws = _ws_ready(config)
    test = {"id": "E2E-001", "title": "t", "requirements": ["REQ-020"], "target_skill": "lending.book.checkout",
            "confidence": 90, "skills": [], "steps": [{"skill": "catalog.book.create"}, {"skill": "lending.book.checkout"}]}
    ws.write_json("e2e_tests.json", [test])
    ws.write_json("execution_results.json", {"tests": {"E2E-001": {"outcome": "failed"}}})
    ev = {"test_id": "E2E-001", "outcome": "failed", "failure": {"step": 1, "kind": "expectation_code",
          "skill_id": "catalog.book.create", "rpc": "CatalogService.CreateBook"},
          "steps": [{"step": 1, "rpc": "CatalogService.CreateBook", "skill_id": "catalog.book.create",
                     "request": {"body": {}}, "expected": {"code": "OK", "fields": {}},
                     "response": {"grpc_status": "INVALID_ARGUMENT", "details": "title required"}}]}
    ws.logs_dir.mkdir(parents=True, exist_ok=True)
    (ws.logs_dir / "E2E-001.evidence.json").write_text(json.dumps(ev))
    llm = FakeLLM({"triage:": {"classification": "TEST_BUG", "severity": "Low", "explanation": "empty title"}})
    triage.run(ws, llm)
    t = ws.read_json("triage.json")["E2E-001"]
    assert t["classification"] == "TEST_BUG" and t["decided_by"] == "llm"
    triage.run(ws, None)  # offline: fail-safe default
    assert ws.read_json("triage.json")["E2E-001"]["classification"] == "PLATFORM_BUG"
