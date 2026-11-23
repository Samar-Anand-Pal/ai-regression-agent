"""Stage 9: bug triage.

A deterministic rule cascade classifies every failure from its evidence:
  transport errors          -> ENV_ERROR
  harness errors            -> TEST_BUG   (bad reference, request didn't fit the proto)
  failing setup step        -> AMBIGUOUS  (the test may have built bad data)
  low-confidence test/map   -> AMBIGUOUS
  otherwise                 -> PLATFORM_BUG
Claude adjudicates only the AMBIGUOUS cases; without Claude they default to
PLATFORM_BUG (fail-safe: a person looks at it rather than it being hidden).
Failures with the same signature are grouped into one bug report.
"""
from __future__ import annotations

import json
from collections import defaultdict

from ..workspace import Workspace

CLASSES = ["PLATFORM_BUG", "TEST_BUG", "ENV_ERROR"]


def _target_step(test: dict) -> int:
    steps = [i for i, st in enumerate(test["steps"], start=1) if st["skill"] == test.get("target_skill")]
    return steps[-1] if steps else len(test["steps"])


def classify(test: dict, evidence: dict, mapping: dict) -> tuple[str, str]:
    f = evidence.get("failure") or {}
    kind = f.get("kind")
    if kind == "transport":
        return "ENV_ERROR", "the target was unreachable or timed out"
    if kind == "harness":
        return "TEST_BUG", f"the test itself is broken: {f.get('message')}"
    step = f.get("step")
    if step is not None and step < _target_step(test):
        return "AMBIGUOUS", "a setup step failed before the action under test ran"
    m = mapping.get(f.get("skill_id", ""), {})
    if test.get("confidence", 100) < 60 or m.get("confidence", 1.0) < 0.7:
        return "AMBIGUOUS", "low-confidence test or skill mapping"
    return "PLATFORM_BUG", "the action under test did not do what the requirement states"


def severity(evidence: dict) -> str:
    f = evidence.get("failure") or {}
    step = next((s for s in evidence["steps"] if s["step"] == f.get("step")), {})
    got = (step.get("response") or {}).get("grpc_status")
    want = (step.get("expected") or {}).get("code")
    if got in ("INTERNAL", "UNKNOWN", "DATA_LOSS"):
        return "Critical"
    if want != "OK" and got == "OK":
        return "High"  # a documented rule is not enforced
    if f.get("kind") == "expectation_field":
        return "Medium"  # wrong value
    return "Medium"


SYSTEM = """You triage a failed end-to-end API test. Decide whether the failure is a PLATFORM_BUG (the service does not
do what the requirement states), a TEST_BUG (the test asked for something the requirement doesn't promise, or built
invalid setup data), or an ENV_ERROR (infrastructure). Base the decision only on the requirement text and the evidence."""

SCHEMA = {"type": "object", "properties": {
    "classification": {"type": "string", "enum": CLASSES},
    "severity": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"]},
    "explanation": {"type": "string"}},
    "required": ["classification", "severity", "explanation"], "additionalProperties": False}


def adjudicate(llm, test, evidence, req_text) -> dict:
    prompt = (f"Test {test['id']}: {test['title']}\nRequirements:\n{req_text}\n\n"
              f"Evidence (every call the test made):\n{json.dumps(evidence['steps'], indent=1)[:12000]}")
    return llm.structured(task=f"triage:{test['id']}", system=SYSTEM, prompt=prompt, schema=SCHEMA, effort="medium")


def run(ws: Workspace, llm) -> dict:
    tests = {t["id"]: t for t in ws.read_json("e2e_tests.json")}
    results = ws.read_json("execution_results.json")["tests"]
    mapping = ws.read_json("skill_endpoint_map.json")
    skills = ws.read_json("skills.json")
    req_text = {r["req"]: (r["text"], s["source"], r["line"]) for s in skills for r in s["rules"]}

    triage = {}
    for tid, r in results.items():
        if r["outcome"] == "passed" or tid not in tests:
            continue
        ev_path = ws.logs_dir / f"{tid}.evidence.json"
        evidence = json.loads(ev_path.read_text()) if ev_path.exists() else {"steps": [], "failure": r["failure"]}
        cls, why = classify(tests[tid], evidence, mapping)
        method, sev = "rules", severity(evidence) if evidence["steps"] else "Medium"
        if cls == "AMBIGUOUS":
            if llm is not None:
                texts = "\n".join(f"- {q}: {req_text[q][0]}" for q in tests[tid]["requirements"] if q in req_text)
                verdict = adjudicate(llm, tests[tid], evidence, texts)
                cls, sev, why, method = verdict["classification"], verdict["severity"], verdict["explanation"], "llm"
            else:
                cls, method = "PLATFORM_BUG", "fail-safe"
                why += "; defaulted to PLATFORM_BUG so a person reviews it (offline mode)"
        triage[tid] = {"classification": cls, "severity": sev, "reason": why, "decided_by": method,
                       "failure": evidence.get("failure")}
    ws.write_json("triage.json", triage)

    # group platform bugs that share a root cause signature
    groups = defaultdict(list)
    for tid, t in triage.items():
        if t["classification"] != "PLATFORM_BUG":
            continue
        ev = json.loads((ws.logs_dir / f"{tid}.evidence.json").read_text())
        f = ev["failure"] or {}
        step = next((s for s in ev["steps"] if s["step"] == f.get("step")), {})
        got = (step.get("response") or {}).get("grpc_status")
        fields = ",".join(m["path"] for m in step.get("mismatches", []))
        groups[(f.get("rpc"), step.get("expected", {}).get("code"), got, fields)].append((tid, ev, step))

    for old in ws.bugs_dir.glob("*.md"):
        old.unlink()
    order = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    for (rpc, want, got, _), items in groups.items():
        items.sort(key=lambda x: x[0])
        tid, ev, step = items[0]
        reqs = sorted({q for t, _, _ in items for q in tests[t]["requirements"]})
        sev = min((triage[t]["severity"] for t, _, _ in items), key=lambda s: order.get(s, 9))
        lines = [f"# {rpc}: expected {want}, got {got}", "",
                 f"- **Severity:** {sev}", "- **Classification:** PLATFORM_BUG",
                 f"- **Affected tests:** {', '.join(t for t, _, _ in items)}", ""]
        lines += ["## Requirement", ""]
        for q in reqs:
            if q in req_text:
                text, src, line = req_text[q]
                lines.append(f"- **{q}** ({src}:{line}) {text}")
        lines += ["", f"## Failed step ({tid}, step {step.get('step')})", "",
                  f"`{step.get('skill_id')}` → `{rpc}`", "", "Request:", "```json",
                  json.dumps((step.get("request") or {}).get("body"), indent=2), "```", "",
                  f"Expected: `{want}`" + (f" with {json.dumps(step['expected']['fields'])}" if step.get("expected", {}).get("fields") else ""),
                  "", f"Actual: `{got}`" + (f" ({step['response']['details']})" if (step.get("response") or {}).get("details") else ""),
                  ""]
        if step.get("mismatches"):
            lines += ["Field mismatches:", ""] + [f"- `{m['path']}`: expected `{m['expected']}`, got `{m['actual']}`"
                                                   for m in step["mismatches"]] + [""]
        lines += ["## Steps to reproduce", ""]
        for s in ev["steps"]:
            body = json.dumps((s.get("request") or {}).get("body"))
            lines.append(f"{s['step']}. `{s['rpc']}` {body} → {(s.get('response') or {}).get('grpc_status', '—')}")
        lines += ["", f"Evidence: `test_logs/{tid}.evidence.json` · Why: {triage[tid]['reason']}"]
        name = f"bug_{tid}.md"
        ws.write_text(f"bug_reports/{name}", "\n".join(lines) + "\n")
        for t, _, _ in items:
            triage[t]["bug_report"] = f"bug_reports/{name}"
    ws.write_json("triage.json", triage)
    counts = defaultdict(int)
    for t in triage.values():
        counts[t["classification"]] += 1
    return {"failures": len(triage), "platform_bugs": len(groups), **{k.lower(): v for k, v in counts.items()}}
