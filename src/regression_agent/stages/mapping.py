"""Stage 4: skill -> RPC mapping, followed by Gate 1 (skill review).

Deterministic scoring first (verb, noun and service/domain token overlap).
Only the ambiguous residue goes to Claude, and Claude can only answer with
one of the real candidate RPCs (the JSON schema is an enum of them), so a
mapping can never point at a method that doesn't exist.
"""
from __future__ import annotations

import re

from ..workspace import Workspace

VERB_SYNONYMS = {
    "create": {"create", "add", "register", "new"}, "register": {"register", "create", "add", "signup"},
    "get": {"get", "view", "read", "fetch", "describe"}, "list": {"list", "search", "browse"},
    "update": {"update", "set", "change", "edit", "modify"}, "delete": {"delete", "remove"},
    "checkout": {"checkout", "check", "borrow", "lend"}, "return": {"return"}, "renew": {"renew", "extend"},
    "place": {"place", "create", "add"}, "cancel": {"cancel"}, "pay": {"pay"},
    "suspend": {"suspend", "disable"}, "reinstate": {"reinstate", "enable", "restore"},
    "advance": {"advance"}, "reset": {"reset"},
}
NOUN_SYNONYMS = {"account": {"account", "fine", "balance"}, "book": {"book", "copy", "copies"}}

DETERMINISTIC_MIN = 0.8
MARGIN = 0.15
LOW_CONFIDENCE = 0.7


def camel_tokens(name: str) -> set[str]:
    return {t.lower().rstrip("s") or t.lower() for t in re.findall(r"[A-Z][a-z0-9]*|[a-z0-9]+", name)}


def _stem(w: str) -> str:
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def score(skill: dict, method: dict) -> float:
    rpc = {_stem(t) for t in camel_tokens(method["rpc"])}
    svc = {_stem(t) for t in camel_tokens(method["service"])} - {"service"}
    verbs = {_stem(v) for v in VERB_SYNONYMS.get(skill["verb"], {skill["verb"]})}
    nouns = {_stem(n) for n in NOUN_SYNONYMS.get(skill["noun"], {skill["noun"]})}
    s = 0.0
    if verbs & rpc:
        s += 0.5
    if nouns & rpc:
        s += 0.35
    domain = _stem(skill["domain"].split("-")[0])
    if any(domain.startswith(t[:4]) or t.startswith(domain[:4]) for t in svc):
        s += 0.15
    return round(s, 3)


SYSTEM = """You map a product skill (one user action described in the docs) to the API method that performs it.
Pick exactly one method from the candidates. Prefer the method whose request is the action itself, not a lookup that
happens before it. Give a confidence between 0 and 1 and one sentence of reasoning."""


def llm_pick(llm, skill: dict, candidates: list[str], spec: dict) -> dict:
    lines = []
    for c in candidates:
        m = spec["methods"][c]
        fields = ", ".join(f["name"] for f in m["request_fields"])
        lines.append(f"- {c}({fields}) -> {m['response_message']}")
    prompt = (f"Skill `{skill['id']}`: {skill['description']}\nRules:\n"
              + "\n".join(f"- {r['req']}: {r['text']}" for r in skill["rules"])
              + "\n\nCandidate methods:\n" + "\n".join(lines))
    schema = {"type": "object", "properties": {
        "method": {"type": "string", "enum": candidates},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"}},
        "required": ["method", "confidence", "reasoning"], "additionalProperties": False}
    return llm.structured(task=f"map:{skill['id']}", system=SYSTEM, prompt=prompt, schema=schema, effort="medium")


def map_skills(skills: list[dict], spec: dict, llm, previous: dict | None = None) -> dict:
    result = {}
    methods = [k for k, m in spec["methods"].items() if not m["streaming"]]
    for s in skills:
        if previous and s["id"] in previous and previous[s["id"]].get("match_method") == "human":
            result[s["id"]] = previous[s["id"]]  # a reviewer's decision at Gate 1 is kept
            continue
        ranked = sorted(((score(s, spec["methods"][k]), k) for k in methods), reverse=True)
        top, second = ranked[0], ranked[1] if len(ranked) > 1 else (0.0, None)
        candidates = [{"method": k, "score": sc} for sc, k in ranked[:3]]
        if top[0] >= DETERMINISTIC_MIN and top[0] - second[0] >= MARGIN:
            chosen, conf, how, why = top[1], top[0], "deterministic", "verb, object and service all match"
        elif llm is not None:
            pool = [k for sc, k in ranked[:6] if sc > 0] or [k for _, k in ranked[:6]]
            pick = llm_pick(llm, s, pool, spec)
            chosen, conf, how, why = pick["method"], max(0.0, min(1.0, float(pick["confidence"]))), "llm", pick["reasoning"]
        elif top[0] > 0:
            chosen, conf, how, why = top[1], top[0], "heuristic", "best token overlap (offline mode, needs review)"
        else:
            result[s["id"]] = {"status": "unmapped", "confidence": 0.0, "match_method": "none",
                               "candidates": candidates, "reasoning": "no method shares any token with this skill"}
            continue
        m = spec["methods"][chosen]
        result[s["id"]] = {
            "status": "mapped" if conf >= LOW_CONFIDENCE else "low_confidence",
            "confidence": round(conf, 2), "match_method": how,
            "service": m["service"], "rpc_method": m["rpc"], "method": chosen,
            "request_message": m["request_message"], "client_method": m["client_method"],
            "candidates": candidates, "reasoning": why,
        }
    return result


def write_review(ws: Workspace, skills: list[dict], mapping: dict, spec: dict) -> None:
    by_id = {s["id"]: s for s in skills}
    used = {v.get("method") for v in mapping.values()}
    lines = ["# Gate 1: skill review", "",
             "Check that each skill points at the right API method. To change one, edit `skill_endpoint_map.json`",
             "(set `method`, `service`, `rpc_method` and `request_message`, and set `match_method` to `human`),",
             "then run `rga approve`.", "",
             "| Skill | Method | Confidence | How | Requirements |", "|---|---|---|---|---|"]
    order = sorted(mapping, key=lambda k: (mapping[k]["status"] == "mapped", mapping[k]["confidence"]))
    for sid in order:
        v = mapping[sid]
        flag = "" if v["status"] == "mapped" else " ⚠"
        lines.append(f"| `{sid}`{flag} | `{v.get('method', '—')}` | {v['confidence']:.2f} | {v['match_method']} | "
                     f"{', '.join(by_id[sid]['requirements'])} |")
    unused = [k for k in spec["methods"] if k not in used]
    lines += ["", f"## API methods no skill maps to ({len(unused)})", "",
              "These have no documented behavior, so no test will cover them unless the docs are updated.", ""]
    lines += [f"- `{k}`" + ("" if spec["methods"][k]["client_method"] else " (no SDK wrapper)") for k in unused]
    ws.write_text("skills_review.md", "\n".join(lines) + "\n")


def run(ws: Workspace, llm) -> dict:
    skills = ws.read_json("skills.json")
    spec = ws.read_json("grpc_spec.json")
    previous = ws.read_json("skill_endpoint_map.json", default={})
    mapping = map_skills(skills, spec, llm, previous)
    ws.write_json("skill_endpoint_map.json", mapping)
    write_review(ws, skills, mapping, spec)
    return {"mapped": sum(v["status"] == "mapped" for v in mapping.values()),
            "low_confidence": sum(v["status"] == "low_confidence" for v in mapping.values()),
            "unmapped": sum(v["status"] == "unmapped" for v in mapping.values())}
