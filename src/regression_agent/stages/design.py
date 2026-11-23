"""Stage 5: E2E test design, followed by Gate 2 (test-spec review).

Builds a directed skill graph (skill A -> skill B when A's response produces an
ID that B's request needs) and designs scenarios that walk it: the setup steps
a rule needs, then the action under test, then the checks. Each test covers
one or more requirements and gets a 0-100 confidence score.

A test spec is plain JSON (see `validate`), not code. Code generation later
turns it into pytest verbatim, so what a reviewer approves here is exactly
what runs.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from ..workspace import Workspace

STATUS_CODES = ["OK", "CANCELLED", "UNKNOWN", "INVALID_ARGUMENT", "DEADLINE_EXCEEDED", "NOT_FOUND",
                "ALREADY_EXISTS", "PERMISSION_DENIED", "RESOURCE_EXHAUSTED", "FAILED_PRECONDITION", "ABORTED",
                "OUT_OF_RANGE", "UNIMPLEMENTED", "INTERNAL", "UNAVAILABLE", "DATA_LOSS", "UNAUTHENTICATED"]
REF = re.compile(r"\$\{([a-z_][a-z0-9_]*)\.([a-z0-9_.\[\]#]+)\}")
BACKTICK = re.compile(r"`([A-Z_]+)`")
# A status code only counts as the expected outcome after wording like "rejected with `X`".
EXPECTED_CODE = re.compile(r"(?:rejected with|returns|fails with|failing with|error)\s+`([A-Z_]+)`")


# ------------------------------------------------------------------ validation (shared by both modes)

def _field_names(fields):
    return {f["name"] for f in fields}


def validate(test: dict, spec: dict, mapping: dict) -> list[str]:
    """Returns a list of problems; an empty list means the spec only uses real methods, fields and refs."""
    errors, saved = [], {}
    if not test.get("steps"):
        return ["test has no steps"]
    for i, st in enumerate(test["steps"], start=1):
        where = f"step {i}"
        m = mapping.get(st.get("skill", ""))
        if not m or m.get("status") == "unmapped":
            errors.append(f"{where}: skill {st.get('skill')!r} is not a mapped skill")
            continue
        if st.get("method") != m["method"]:
            errors.append(f"{where}: skill {st['skill']} maps to {m['method']}, not {st.get('method')}")
            continue
        meth = spec["methods"][m["method"]]
        req_fields = _field_names(meth["request_fields"])
        for k, v in (st.get("request") or {}).items():
            if k not in req_fields:
                errors.append(f"{where}: {meth['request_message']} has no field {k!r}")
            for name, _ in REF.findall(json.dumps(v)):
                if name not in saved:
                    errors.append(f"{where}: ${{{name}...}} refers to a step that hasn't run yet")
        exp = st.get("expect") or {}
        code = exp.get("code", "OK")
        if code not in STATUS_CODES:
            errors.append(f"{where}: unknown status code {code!r}")
        resp_fields = _field_names(meth["response_fields"])
        for path in (exp.get("fields") or {}):
            head = path.lstrip("#").split(".")[0].split("[")[0]
            if head not in resp_fields:
                errors.append(f"{where}: {meth['response_message']} has no field {head!r}")
            if code != "OK":
                errors.append(f"{where}: field checks are only allowed when the call is expected to succeed")
        if st.get("save_as"):
            if code != "OK":
                errors.append(f"{where}: can't save the result of a call expected to fail")
            saved[st["save_as"]] = meth["response_message"]
    return errors


# ------------------------------------------------------------------ skill graph

def build_graph(skills: list[dict], spec: dict, mapping: dict) -> dict:
    """producer skill -> consumer skills, via `<entity>_id` fields."""
    produces, consumes = defaultdict(set), defaultdict(set)
    for s in skills:
        m = mapping.get(s["id"])
        if not m or "method" not in m:
            continue
        meth = spec["methods"][m["method"]]
        req = _field_names(meth["request_fields"])
        for f in meth["response_fields"]:
            if f["name"].endswith("_id") and f["name"] not in req:
                produces[f["name"]].add(s["id"])
        for f in req:
            if f.endswith("_id"):
                consumes[f].add(s["id"])
    edges = defaultdict(set)
    for field, prods in produces.items():
        for p in prods:
            edges[p] |= consumes.get(field, set()) - {p}
    return {"produces": {k: sorted(v) for k, v in produces.items()},
            "edges": {k: sorted(v) for k, v in sorted(edges.items())}}


# ------------------------------------------------------------------ offline heuristic designer

def _sample(field: dict, n: int):
    name, typ = field["name"], field["type"]
    if typ == "string":
        for key, val in (("isbn", f"978-0-00-{n:06d}"), ("email", f"member{n}@example.com"),
                         ("title", f"Test Title {n}"), ("author", f"Author {n}"), ("name", f"Member {n}")):
            if key in name:
                return val
        return f"{name}-{n}"
    if typ == "bool":
        return True
    if typ in ("int32", "int64", "uint32", "uint64", "sint32", "sint64"):
        return 1
    return None


class NoProducer(Exception):
    pass


class _Planner:
    """Builds the setup chain for one scenario using the skill graph."""

    def __init__(self, skills, spec, mapping):
        self.spec, self.mapping = spec, mapping
        self.by_id = {s["id"]: s for s in skills}
        # producer for each id field: the mapped skill with the fewest id inputs
        self.producer = {}
        for s in skills:
            m = mapping.get(s["id"])
            if not m or "method" not in m or s["domain"] == "testing":
                continue
            meth = spec["methods"][m["method"]]
            req = _field_names(meth["request_fields"])
            for f in meth["response_fields"]:
                if f["name"].endswith("_id") and f["name"] not in req:
                    cost = sum(x.endswith("_id") for x in req)
                    if f["name"] not in self.producer or cost < self.producer[f["name"]][0]:
                        self.producer[f["name"]] = (cost, s["id"])

    def new(self):
        self.steps, self.saved, self.n = [], {}, 0

    def method(self, sid):
        return self.spec["methods"][self.mapping[sid]["method"]]

    def request_for(self, sid, overrides=None):
        req = {}
        for f in self.method(sid)["request_fields"]:
            if overrides and f["name"] in overrides:
                req[f["name"]] = overrides[f["name"]]
            elif f["name"].endswith("_id"):
                req[f["name"]] = self.ensure(f["name"])
            else:
                self.n += 1
                v = _sample(f, self.n)
                if v is not None:
                    req[f["name"]] = v
        return req

    def ensure(self, id_field):
        entity = id_field[:-3]
        if entity in self.saved:
            return f"${{{entity}.{id_field}}}"
        if id_field not in self.producer:
            raise NoProducer(f"no skill produces {id_field}")
        sid = self.producer[id_field][1]
        req = self.request_for(sid)
        self.add(sid, req, save_as=entity)
        return f"${{{entity}.{id_field}}}"

    def add(self, sid, req, expect=None, save_as=None):
        st = {"skill": sid, "method": self.mapping[sid]["method"], "request": req,
              "expect": expect or {"code": "OK", "fields": {}}}
        if save_as:
            st["save_as"] = save_as
            self.saved[save_as] = sid
        self.steps.append(st)


def _state_index(skills, spec, mapping):
    """'suspended' -> skill that puts the noun in that state, from rules like 'status becomes `SUSPENDED`'."""
    initial, index = set(), {}
    for s in skills:
        for r in s["rules"]:
            for m in re.finditer(r"(?:becomes|back to|starts out|is)\s+`([A-Z_]+)`", r["text"]):
                state = m.group(1)
                if s["verb"] in ("create", "register"):
                    initial.add(state)
                elif mapping.get(s["id"], {}).get("method"):
                    index.setdefault(state.lower(), s)
    return {k: v for k, v in index.items() if k.upper() not in initial}


def _enum_expectations(text, method, spec):
    exp = {}
    expected = set(EXPECTED_CODE.findall(text))
    for word in BACKTICK.findall(text):
        if word in expected:
            continue
        for f in method["response_fields"]:
            values = spec["enums"].get(f["type"], [])
            full = next((v for v in values if v.endswith("_" + word)), None)
            if full and not f["repeated"]:
                exp[f["name"]] = full
    return exp


def heuristic_design(skills, spec, mapping, targets: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Setup planning always sees every skill; tests are only designed for `targets` (default: all)."""
    plan = _Planner(skills, spec, mapping)
    states = _state_index(skills, spec, mapping)
    tests, gaps = [], []
    for s in skills:
        if targets is not None and s["id"] not in targets:
            continue
        m = mapping.get(s["id"])
        if not m or "method" not in m or s["domain"] == "testing":
            continue
        meth = spec["methods"][m["method"]]
        req_names = _field_names(meth["request_fields"])
        for r in s["rules"]:
            text = r["text"]
            codes = [w for w in EXPECTED_CODE.findall(text) if w in STATUS_CODES]
            code = codes[0] if codes else "OK"
            plain = BACKTICK.sub("", text).lower()
            plan.new()
            confidence = 60
            try:
                test = _design_rule(plan, states, s, r, meth, spec, req_names, code, plain, text, confidence, gaps)
            except NoProducer as e:
                gaps.append({"skill": s["id"], "requirement": r["req"], "reason": str(e)})
                continue
            if test:
                tests.append(test)
    return tests, gaps


def _design_rule(plan, states, s, r, meth, spec, req_names, code, plain, text, confidence, gaps):
    """One scenario for one requirement, or None (with a gap recorded) if offline mode can't build it."""
    # state setup: "a suspended member ..." -> suspend the member first
    for word, state_skill in states.items():
        if state_skill["id"] != s["id"] and re.search(rf"\b{word}\s+{state_skill['noun']}", plain):
            plan.add(state_skill["id"], plan.request_for(state_skill["id"]))
            confidence += 10

    overrides, repeat = {}, False
    if code == "NOT_FOUND":
        id_field = f"{s['noun']}_id" if f"{s['noun']}_id" in req_names else next(
            (f for f in req_names if f.endswith("_id")), None)
        if not id_field:
            gaps.append({"skill": s["id"], "requirement": r["req"], "reason": "no ID field to make missing"})
            return None
        overrides[id_field] = "does-not-exist"
        confidence = 75
    elif code == "ALREADY_EXISTS" or (code == "FAILED_PRECONDITION" and "already" in plain):
        if code == "ALREADY_EXISTS" and s["verb"] not in ("create", "register", "place"):
            gaps.append({"skill": s["id"], "requirement": r["req"],
                         "reason": "a duplicate for this action involves another entity; offline mode "
                                   "can't build that, run with Claude"})
            return None
        repeat, confidence = True, 75  # do the same thing twice
    elif code == "INVALID_ARGUMENT":
        more = re.search(r"more than (\d+)", plain)
        int_fields = [f["name"] for f in meth["request_fields"] if f["type"].startswith("int")]
        if "non-positive" in plain and int_fields:
            overrides[int_fields[-1]] = 0
        elif more and int_fields:
            overrides[int_fields[-1]] = int(more.group(1)) + 1
        else:
            gaps.append({"skill": s["id"], "requirement": r["req"],
                         "reason": "offline mode can't tell which input makes this invalid"})
            return None
        confidence = 70
    elif code != "OK" and len(plan.steps) == 0:
        gaps.append({"skill": s["id"], "requirement": r["req"],
                     "reason": f"needs a precondition for {code} that offline mode can't infer; run with Claude"})
        return None

    req = plan.request_for(s["id"], overrides)
    if repeat:
        plan.add(s["id"], dict(req))
    fields = _enum_expectations(text, meth, spec) if code == "OK" else {}
    if code == "OK" and not fields:
        confidence -= 25  # only checks that the call succeeds
    plan.add(s["id"], req, expect={"code": code, "fields": fields})
    used = list(dict.fromkeys(st["skill"] for st in plan.steps))
    return {"title": f"{s['id']}: {text[:90]}", "requirements": [r["req"]], "skills": used,
            "target_skill": s["id"], "confidence": max(5, min(confidence, 95)),
            "rationale": "designed offline from the requirement text", "steps": plan.steps}


# ------------------------------------------------------------------ Claude designer

SYSTEM = """You design end-to-end API regression tests from product requirements.

You receive one target skill (a user action) with the exact requirement sentences it must satisfy, plus the catalogue of
every skill you may call (each bound to one real API method with its request and response fields).

Write tests as JSON steps. Conventions:
- The service is reset to an empty state before every test{reset_note}. Create everything a test needs inside the test.
- Each step calls one skill. `request` is a list of field/value pairs; values are JSON (strings quoted).
- `save_as` stores a successful response under a name; later steps refer to its fields as "${{name.field}}".
- `expect.code` is a gRPC status name (OK, NOT_FOUND, FAILED_PRECONDITION, ...). Field checks (`expect.fields`) are only
  allowed on OK steps; paths may use dots and [index], and "#field" means the length of a repeated field. Enum values are
  written as their full names (e.g. LOAN_STATUS_ACTIVE).
- Cover every requirement of the target skill. Prefer realistic flows of 3-8 steps that check the stated outcome
  precisely (exact numbers, states and counts the requirement promises), including boundaries (e.g. the last allowed
  renewal succeeds and the next one fails).
- Only use skills, fields and enum values from the catalogue. Do not test behavior the requirements don't state.
- confidence (0-100): how sure you are the test checks exactly what the requirement promises, given the available API."""

SCHEMA = {
    "type": "object",
    "properties": {"tests": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "requirements": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "integer"},
            "rationale": {"type": "string"},
            "steps": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "request": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"field": {"type": "string"}, "value_json": {"type": "string"}},
                        "required": ["field", "value_json"], "additionalProperties": False}},
                    "save_as": {"type": "string"},
                    "expect_code": {"type": "string", "enum": STATUS_CODES},
                    "expect_fields": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "value_json": {"type": "string"}},
                        "required": ["path", "value_json"], "additionalProperties": False}},
                },
                "required": ["skill", "request", "save_as", "expect_code", "expect_fields"],
                "additionalProperties": False}},
        },
        "required": ["title", "requirements", "confidence", "rationale", "steps"],
        "additionalProperties": False}}},
    "required": ["tests"], "additionalProperties": False,
}


def _catalogue(skills, spec, mapping) -> str:
    lines = []
    for s in skills:
        m = mapping.get(s["id"])
        if not m or "method" not in m:
            continue
        meth = spec["methods"][m["method"]]
        fmt = lambda fs: ", ".join(f"{f['name']}: {'repeated ' if f['repeated'] else ''}{f['type']}" for f in fs)
        lines.append(f"- {s['id']} -> {m['method']}\n    request {meth['request_message']}({fmt(meth['request_fields'])})"
                     f"\n    response {meth['response_message']}({fmt(meth['response_fields'])})")
    msgs = {f["type"] for m in spec["methods"].values() for f in m["response_fields"]} & set(spec["messages"])
    for name in sorted(msgs):
        lines.append(f"- message {name}({', '.join(f['name'] + ': ' + f['type'] for f in spec['messages'][name])})")
    for name, values in sorted(spec["enums"].items()):
        lines.append(f"- enum {name}: {', '.join(values)}")
    return "\n".join(lines)


def _from_llm(t: dict, mapping: dict) -> dict:
    steps = []
    for st in t["steps"]:
        req = {}
        for kv in st["request"]:
            try:
                req[kv["field"]] = json.loads(kv["value_json"])
            except json.JSONDecodeError:
                req[kv["field"]] = kv["value_json"]
        fields = {}
        for kv in st["expect_fields"]:
            try:
                fields[kv["path"]] = json.loads(kv["value_json"])
            except json.JSONDecodeError:
                fields[kv["path"]] = kv["value_json"]
        step = {"skill": st["skill"], "method": mapping.get(st["skill"], {}).get("method"),
                "request": req, "expect": {"code": st["expect_code"], "fields": fields}}
        if st["save_as"]:
            step["save_as"] = st["save_as"]
        steps.append(step)
    return {"title": t["title"], "requirements": t["requirements"], "confidence": t["confidence"],
            "rationale": t["rationale"], "steps": steps}


def llm_design_skill(llm, skill, skills, spec, mapping, reset_note) -> tuple[list[dict], list[dict]]:
    system = SYSTEM.format(reset_note=reset_note)
    base = (f"Target skill `{skill['id']}` ({mapping[skill['id']]['method']}): {skill['description']}\n"
            "Requirements to cover:\n" + "\n".join(f"- {r['req']}: {r['text']}" for r in skill["rules"])
            + "\n\nSkill catalogue:\n" + _catalogue(skills, spec, mapping))
    out = llm.structured(task=f"design:{skill['id']}", system=system, prompt=base, schema=SCHEMA)
    good, bad = [], []
    for t in out["tests"]:
        test = _from_llm(t, mapping)
        errs = validate(test, spec, mapping)
        if errs:  # one repair round with the validator's errors
            fix = llm.structured(task=f"design-fix:{skill['id']}", system=system, schema=SCHEMA, prompt=(
                base + "\n\nThis test you wrote is invalid:\n" + json.dumps(t) + "\nProblems:\n- "
                + "\n- ".join(errs) + "\nReturn a corrected version as the only item in `tests`."))
            test = _from_llm(fix["tests"][0], mapping) if fix["tests"] else test
            errs = validate(test, spec, mapping)
        if errs:
            bad.append({"skill": skill["id"], "title": t["title"], "errors": errs})
            continue
        test["target_skill"] = skill["id"]
        test["skills"] = list(dict.fromkeys(st["skill"] for st in test["steps"]))
        test["confidence"] = max(0, min(100, int(test["confidence"])))
        good.append(test)
    covered = {r for t in good for r in t["requirements"]}
    gaps = [{"skill": skill["id"], "requirement": r["req"], "reason": "no valid test was designed"}
            for r in skill["rules"] if r["req"] not in covered]
    return good, gaps + [{"skill": b["skill"], "requirement": None, "reason": f"rejected test '{b['title']}': "
                          + "; ".join(b["errors"][:3])} for b in bad]


# ------------------------------------------------------------------ stage

def design(ws: Workspace, llm, skill_ids: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    skills = ws.read_json("skills.json")
    spec = ws.read_json("grpc_spec.json")
    mapping = ws.read_json("skill_endpoint_map.json")
    targets = [s for s in skills if skill_ids is None or s["id"] in skill_ids]
    if llm is None:
        return heuristic_design(skills, spec, mapping, skill_ids) if targets else ([], [])
    reset = ws.cfg.target.reset_rpc
    note = f" (by calling {reset})" if reset else ""
    tests, gaps = [], []
    for s in targets:
        m = mapping.get(s["id"])
        if not m or "method" not in m or s["domain"] == "testing":
            continue
        t, g = llm_design_skill(llm, s, skills, spec, mapping, note)
        tests += t
        gaps += g
    return tests, gaps


def number(tests: list[dict], start: int = 1) -> list[dict]:
    for i, t in enumerate(tests, start=start):
        t["id"] = f"E2E-{i:03d}"
    return tests


def write_review(ws: Workspace, tests: list[dict], gaps: list[dict]) -> None:
    lines = ["# Gate 2: test-spec review", "",
             f"{len(tests)} tests designed. Read them before any code is generated. To change one, edit",
             "`e2e_tests.json` (delete a test, fix a step or an expected value), then run `rga approve`.", ""]
    low = [t for t in tests if t["confidence"] < 60]
    if low:
        lines += [f"**{len(low)} low-confidence tests** (below 60) are marked ⚠.", ""]
    for t in tests:
        flag = " ⚠" if t["confidence"] < 60 else ""
        lines += [f"## {t['id']}: {t['title']}{flag}",
                  f"Confidence: {t['confidence']}/100 · Verifies: {', '.join(t['requirements'])} · "
                  f"Skills: {len(t['skills'])}", ""]
        for i, st in enumerate(t["steps"], start=1):
            req = ", ".join(f"{k}={json.dumps(v)}" for k, v in st["request"].items())
            exp = st["expect"]["code"]
            if st["expect"].get("fields"):
                exp += " and " + ", ".join(f"{k}={json.dumps(v)}" for k, v in st["expect"]["fields"].items())
            save = f" → `{st['save_as']}`" if st.get("save_as") else ""
            lines.append(f"{i}. `{st['skill']}` {st['method']}({req}) expect {exp}{save}")
        lines.append("")
    if gaps:
        lines += [f"## Not designed ({len(gaps)})", ""]
        lines += [f"- {g.get('requirement') or '—'} ({g['skill']}): {g['reason']}" for g in gaps]
    ws.write_text("e2e_tests.md", "\n".join(lines) + "\n")


def run(ws: Workspace, llm) -> dict:
    skills = ws.read_json("skills.json")
    spec = ws.read_json("grpc_spec.json")
    mapping = ws.read_json("skill_endpoint_map.json")
    ws.write_json("skill_graph.json", build_graph(skills, spec, mapping))
    tests, gaps = design(ws, llm)
    tests = number(tests)
    ws.write_json("e2e_tests.json", tests)
    ws.write_json("design_gaps.json", gaps)
    write_review(ws, tests, gaps)
    avg = sum(len(t["skills"]) for t in tests) / len(tests) if tests else 0
    return {"tests": len(tests), "gaps": len(gaps), "avg_skills_per_test": round(avg, 2)}
