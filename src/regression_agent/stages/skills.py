"""Stage 2: skills extraction.

A *skill* is one atomic action a user can take through the system, named
domain.noun.verb (e.g. lending.book.checkout). Each skill carries the exact
requirement sentences it is responsible for, with the doc and line they came
from, so every later artifact can be traced back to the docs.

Rules:
- split by action, not by outcome: "add a book" and "adding a duplicate ISBN
  is rejected" are the same skill (catalog.book.create) with two rules;
- never silently drop anything: lines that can't be tested through the API are
  written to excluded_actions.md with a reason.

Claude does the grouping when available; otherwise a verb/noun lexicon does.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass

from ..workspace import Workspace

REQ_LINE = re.compile(r"^\s*[-*]\s+\*\*(?P<id>(?:REQ|OPS)-\d+)\*\*\s*(?P<text>.+)$")
BULLET = re.compile(r"^\s*[-*]\s+(?P<text>.+)$")
HEADING = re.compile(r"^(#+)\s+(?P<title>.+)$")


@dataclass
class Unit:
    """One requirement sentence (testable candidate) or other bullet (exclusion candidate)."""

    req_id: str | None
    text: str
    source: str
    line: int
    section: str


def parse_doc(name: str, text: str) -> list[Unit]:
    units, section = [], ""
    for i, line in enumerate(text.splitlines(), start=1):
        h = HEADING.match(line)
        if h:
            section = h.group("title").strip()
            continue
        m = REQ_LINE.match(line)
        if m:
            units.append(Unit(m.group("id"), m.group("text").strip(), name, i, section))
            continue
        b = BULLET.match(line)
        if b and not line.lstrip().startswith("**Owner"):
            units.append(Unit(None, b.group("text").strip(), name, i, section))
    return units


def rules_hash(rules: list[dict]) -> str:
    body = "\n".join(f"{r['req']}:{r['text']}" for r in sorted(rules, key=lambda r: r["req"]))
    return hashlib.md5(body.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ offline heuristic

# (phrase, canonical verb). Longest phrase at the earliest position wins.
VERB_PHRASES = [
    ("see the list", "list"), ("list", "list"), ("browse", "list"),
    ("add", "create"), ("adding", "create"), ("create", "create"), ("creating", "create"),
    ("register", "register"), ("registering", "register"), ("sign up", "register"),
    ("look up", "get"), ("looked up", "get"), ("see", "get"), ("view", "get"),
    ("change", "update"), ("update", "update"), ("updating", "update"),
    ("remove", "delete"), ("removal", "delete"), ("delete", "delete"),
    ("suspend", "suspend"), ("reinstate", "reinstate"),
    ("check out", "checkout"), ("checking out", "checkout"), ("checked out", "checkout"),
    ("checks out", "checkout"), ("checkout", "checkout"), ("borrow", "checkout"),
    ("returns", "return"), ("return", "return"), ("returned", "return"), ("returning", "return"),
    ("renew", "renew"), ("renewed", "renew"), ("renewal", "renew"),
    ("place", "place"), ("cancel", "cancel"),
    ("pay", "pay"), ("paying", "pay"),
    ("move the service clock forward", "advance"), ("advance", "advance"),
    ("reset", "reset"),
]
NOUNS = {
    "book": "book", "books": "book", "copy": "book", "copies": "book", "catalog": "book",
    "member": "member", "members": "member", "profile": "member",
    "loan": "loan", "loans": "loan", "hold": "hold", "holds": "hold",
    "account": "account", "balance": "account", "fine": "account", "fines": "account",
    "clock": "clock", "service": "service",
}
# Verbs whose object is fixed regardless of the wording.
DEFAULT_NOUN = {"checkout": "book", "return": "book", "renew": "loan", "place": "hold", "cancel": "hold",
                "pay": "account", "advance": "clock", "reset": "service", "register": "member",
                "suspend": "member", "reinstate": "member"}


def _find_verb(text: str) -> tuple[str, int] | None:
    low = text.lower()
    best = None
    for phrase, verb in VERB_PHRASES:
        for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", low):
            key = (m.start(), -len(phrase))
            if best is None or key < best[0]:
                best = (key, verb, m.end())
            break
    return (best[1], best[2]) if best else None


def _find_noun(text: str, verb: str, after: int) -> str | None:
    if verb in DEFAULT_NOUN:
        return DEFAULT_NOUN[verb]
    low = text.lower()
    words = re.findall(r"[a-z]+", low[after:]) + re.findall(r"[a-z]+", low[:after])
    return next((NOUNS[w] for w in words if w in NOUNS), None)


def _domain(source: str) -> str:
    stem = source.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-") or "general"


def heuristic_extract(units: list[Unit]) -> tuple[list[dict], list[dict]]:
    skills: dict[str, dict] = {}
    excluded = []
    for u in units:
        if u.req_id is None:
            reason = ("user story: narrative context, covered by the numbered requirements"
                      if u.text.lower().startswith("as a") else
                      "no requirement ID and no observable API behavior (UI, performance, email or similar)")
            excluded.append({"text": u.text, "source": u.source, "line": u.line, "reason": reason})
            continue
        found = _find_verb(u.text)
        noun = _find_noun(u.text, found[0], found[1]) if found else None
        if not found or not noun:
            excluded.append({"text": u.text, "source": u.source, "line": u.line, "req": u.req_id,
                             "reason": "could not identify an action and object (offline mode)"})
            continue
        verb = found[0]
        sid = f"{_domain(u.source)}.{noun}.{verb}"
        s = skills.setdefault(sid, {"id": sid, "domain": _domain(u.source), "noun": noun, "verb": verb,
                                    "description": u.text, "actor": None, "preconditions": [],
                                    "rules": [], "source": u.source})
        s["rules"].append({"req": u.req_id, "text": u.text, "line": u.line})
    return list(skills.values()), excluded


# ------------------------------------------------------------------ Claude

SYSTEM = """You turn product documentation into a list of testable skills for an end-to-end test generator.

A skill is ONE atomic action a user can perform through the product's API, named domain.noun.verb in lowercase
(for example catalog.book.create, lending.loan.renew). Use the document's file name (without extension) as the domain.

Rules:
- Split by action, not by outcome. A requirement that describes a rejection or an edge case of an action belongs to that
  action's skill (e.g. "adding a duplicate ISBN is rejected" belongs to catalog.book.create).
- A requirement that describes what happens as a side effect of an action belongs to the action that triggers it
  (e.g. "when a copy is returned, the first hold becomes READY" belongs to the return action).
- Every requirement ID you are given must appear in exactly one skill, or in `excluded` with a concrete reason.
- Bullets without a requirement ID (user stories, non-functional notes, out-of-scope items) go to `excluded` unless they
  describe an API-observable behavior not covered by any numbered requirement.
- Never invent behavior that the document does not state. Preconditions are short facts that must hold before the action."""

SCHEMA = {
    "type": "object",
    "properties": {
        "skills": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "description": {"type": "string"},
                "actor": {"type": "string"},
                "preconditions": {"type": "array", "items": {"type": "string"}},
                "requirements": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id", "description", "actor", "preconditions", "requirements"],
            "additionalProperties": False,
        }},
        "excluded": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["text", "reason"],
            "additionalProperties": False,
        }},
    },
    "required": ["skills", "excluded"],
    "additionalProperties": False,
}

SKILL_ID = re.compile(r"^[a-z0-9-]+\.[a-z0-9-]+\.[a-z0-9-]+$")


def llm_extract(llm, name: str, text: str, units: list[Unit]) -> tuple[list[dict], list[dict]]:
    by_req = {u.req_id: u for u in units if u.req_id}
    prompt = (f"Document `{name}`:\n\n<document>\n{text}\n</document>\n\n"
              f"Requirement IDs that must each be assigned: {', '.join(by_req) or '(none)'}")
    out = llm.structured(task=f"skills:{name}", system=SYSTEM, prompt=prompt, schema=SCHEMA)

    skills, assigned = [], set()
    for s in out["skills"]:
        sid = s["id"].strip().lower()
        reqs = [r for r in s["requirements"] if r in by_req and r not in assigned]
        if not SKILL_ID.match(sid) or not reqs:
            continue  # malformed or empty: its requirements fall through to the heuristic below
        assigned.update(reqs)
        domain, noun, verb = sid.split(".")
        skills.append({"id": sid, "domain": domain, "noun": noun, "verb": verb, "description": s["description"],
                       "actor": s["actor"] or None, "preconditions": s["preconditions"], "source": name,
                       # rule text always comes from the doc itself, never from the model
                       "rules": [{"req": r, "text": by_req[r].text, "line": by_req[r].line} for r in reqs]})
    excluded = [{"text": e["text"], "source": name, "line": None, "reason": e["reason"]} for e in out["excluded"]]

    leftovers = [u for u in units if u.req_id and u.req_id not in assigned
                 and not any(u.req_id in e["text"] for e in out["excluded"])]
    if leftovers:  # nothing is dropped: unassigned requirements get the heuristic treatment
        extra, extra_excl = heuristic_extract(leftovers)
        skills.extend(extra)
        excluded.extend(extra_excl)
    return skills, excluded


# ------------------------------------------------------------------ stage

def extract_from_docs(ws: Workspace, llm, names: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    manifest = ws.read_json("manifest.json")
    skills, excluded = [], []
    for name in names if names is not None else sorted(manifest):
        text = (ws.docs_mirror / name).read_text()
        units = parse_doc(name, text)
        if llm is not None:
            s, e = llm_extract(llm, name, text, units)
        else:
            s, e = heuristic_extract(units)
        skills.extend(s)
        excluded.extend(e)
    merged: dict[str, dict] = {}
    for s in skills:  # same id from two docs: keep one skill, both sets of rules
        if s["id"] in merged:
            merged[s["id"]]["rules"].extend(s["rules"])
        else:
            merged[s["id"]] = s
    out = sorted(merged.values(), key=lambda s: s["id"])
    for s in out:
        s["rules"].sort(key=lambda r: r["req"])
        s["requirements"] = [r["req"] for r in s["rules"]]
        s["content_hash"] = rules_hash(s["rules"])
    return out, excluded


def write_outputs(ws: Workspace, skills: list[dict], excluded: list[dict]) -> None:
    ws.write_json("skills.json", skills)
    ws.write_json("skill_traceability.json", {
        s["id"]: [{"requirement": r["req"], "source": s["source"], "line": r["line"]} for r in s["rules"]]
        for s in skills})
    lines = [f"# Skills ({len(skills)})", ""]
    by_domain = defaultdict(list)
    for s in skills:
        by_domain[s["domain"]].append(s)
    for d in sorted(by_domain):
        lines += [f"## {d}", ""]
        for s in by_domain[d]:
            lines += [f"### {s['id']}", f"{s['description']}", ""]
            if s.get("actor"):
                lines.append(f"- Actor: {s['actor']}")
            for p in s.get("preconditions") or []:
                lines.append(f"- Precondition: {p}")
            for r in s["rules"]:
                lines.append(f"- **{r['req']}** ({s['source']}:{r['line']}) {r['text']}")
            lines.append("")
    ws.write_text("skills.md", "\n".join(lines))
    ws.write_json("excluded_actions.json", excluded)
    ex = [f"# Excluded actions ({len(excluded)})", "",
          "Lines from the docs that were not turned into skills, and why. Nothing is dropped silently.", ""]
    for e in excluded:
        loc = f"{e['source']}:{e['line']}" if e.get("line") else e["source"]
        ex.append(f"- `{loc}` {e['text']}\n  - **Reason:** {e['reason']}")
    ws.write_text("excluded_actions.md", "\n".join(ex) + "\n")


def run(ws: Workspace, llm) -> dict:
    skills, excluded = extract_from_docs(ws, llm)
    write_outputs(ws, skills, excluded)
    return {"skills": len(skills), "excluded": len(excluded),
            "domains": len({s["domain"] for s in skills})}
