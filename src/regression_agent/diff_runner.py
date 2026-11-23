"""diff-runner: doc-diff and surgical update of an existing run's output.

Instead of rebuilding everything when the docs change, it:
  1. diffs the docs against manifest.json (MD5 per file);
  2. re-extracts skills only from changed docs and classifies every skill as
     unchanged / updated / obsolete / new / relocated;
  3. rebuilds the API spec and diffs it method by method (endpoint drift: a
     changed RPC contract invalidates its tests even if no doc changed);
  4. classifies every test as VALID / UPDATE_REQUIRED / UNCERTAIN / OBSOLETE / NEW;
  5. regenerates only UPDATE_REQUIRED and NEW tests, deletes OBSOLETE ones, and
     leaves VALID and UNCERTAIN tests untouched (UNCERTAIN ones are flagged as
     stale in the coverage map for a person to look at);
  6. re-runs the suite, triage and coverage, then resets state.json so the
     next doc update can run straight away.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .graph import GATES, RunState, StateFile, after_gate, gate_node, stage_node
from .stages import codegen, coverage, design, envsetup, execute, ingest, mapping, skills as skills_stage, spec, triage
from .stages.spec import method_signature
from .workspace import Workspace

DIFF_ORDER = ["doc_diff", "skill_diff", "endpoint_diff", "remap", "classify_tests", "test_spec_review",
              "regenerate", "envsetup", "code_env_review", "execute", "triage", "coverage", "finalize"]


def doc_diff(ws: Workspace) -> dict:
    old = ws.read_json("manifest.json")
    ws.write_json(".state/previous_manifest.json", old)
    ingest.run(ws)
    new = ws.read_json("manifest.json")
    diff = {"changed": sorted(k for k in new if k in old and new[k] != old[k]),
            "added": sorted(k for k in new if k not in old),
            "removed": sorted(k for k in old if k not in new),
            "unchanged": sorted(k for k in new if k in old and new[k] == old[k])}
    ws.write_json("doc_diff.json", diff)
    return {k: len(v) for k, v in diff.items()}


def skill_diff(ws: Workspace, llm) -> dict:
    diff = ws.read_json("doc_diff.json")
    old_skills = {s["id"]: s for s in ws.read_json("skills.json")}
    ws.write_json(".state/previous_skills.json", list(old_skills.values()))
    touched = set(diff["changed"]) | set(diff["added"]) | set(diff["removed"])
    fresh, excluded = skills_stage.extract_from_docs(ws, llm, names=diff["changed"] + diff["added"])

    # skills from untouched docs are carried over as they are; nothing is re-extracted for them
    kept = [s for s in old_skills.values() if s["source"] not in touched]
    new_skills = {s["id"]: s for s in kept}
    for s in fresh:
        if s["id"] in new_skills:  # same id now also produced by a changed doc: merge rules
            new_skills[s["id"]]["rules"] = sorted(new_skills[s["id"]]["rules"] + s["rules"], key=lambda r: r["req"])
            new_skills[s["id"]]["content_hash"] = skills_stage.rules_hash(new_skills[s["id"]]["rules"])
            new_skills[s["id"]]["requirements"] = [r["req"] for r in new_skills[s["id"]]["rules"]]
        else:
            new_skills[s["id"]] = s

    status = {}
    for sid, s in new_skills.items():
        if sid not in old_skills:
            status[sid] = "new"
        elif old_skills[sid]["content_hash"] != s["content_hash"]:
            status[sid] = "updated"
        else:
            status[sid] = "unchanged"
    relocated = {}
    for sid, s in old_skills.items():
        if sid in new_skills:
            continue
        # same requirements and wording under a different id/doc: it moved, it didn't disappear
        twin = next((n for n, ns in new_skills.items() if status.get(n) == "new"
                     and ns["content_hash"] == s["content_hash"]), None)
        if twin:
            status[twin] = "relocated"
            relocated[sid] = twin
            status[sid] = "relocated_from"
        else:
            status[sid] = "obsolete"

    merged = sorted(new_skills.values(), key=lambda s: s["id"])
    # keep excluded actions from untouched docs plus the fresh ones
    old_excluded = [e for e in ws.read_json("excluded_actions.json", default=[]) if e["source"] not in touched]
    skills_stage.write_outputs(ws, merged, old_excluded + excluded)
    ws.write_json("skill_diff.json", {"status": status, "relocated": relocated})
    counts = {}
    for v in status.values():
        if v != "relocated_from":
            counts[v] = counts.get(v, 0) + 1
    return counts


def endpoint_diff(ws: Workspace) -> dict:
    old = ws.read_json("grpc_spec.json")
    ws.write_json(".state/previous_spec.json", old)
    spec.run(ws)
    new = ws.read_json("grpc_spec.json")
    result = {
        "added": sorted(k for k in new["methods"] if k not in old["methods"]),
        "removed": sorted(k for k in old["methods"] if k not in new["methods"]),
        "changed": sorted(k for k in new["methods"] if k in old["methods"]
                          and method_signature(new["methods"][k]) != method_signature(old["methods"][k])),
    }
    ws.write_json("endpoint_diff.json", result)
    return {k: len(v) for k, v in result.items()}


def remap(ws: Workspace, llm) -> dict:
    status = ws.read_json("skill_diff.json")["status"]
    old_map = ws.read_json("skill_endpoint_map.json")
    ws.write_json(".state/previous_map.json", old_map)
    skills = ws.read_json("skills.json")
    sp = ws.read_json("grpc_spec.json")
    ep = ws.read_json("endpoint_diff.json")
    redo = [s for s in skills if status.get(s["id"]) in ("new", "updated", "relocated")
            or old_map.get(s["id"], {}).get("method") in ep["removed"]]
    fresh = mapping.map_skills(redo, sp, llm, previous=old_map)
    new_map = {s["id"]: fresh.get(s["id"], old_map.get(s["id"])) for s in skills}
    new_map = {k: v for k, v in new_map.items() if v is not None}
    ws.write_json("skill_endpoint_map.json", new_map)
    mapping.write_review(ws, skills, new_map, sp)
    return {"remapped": len(redo), "kept": len(new_map) - len(redo)}


def classify_tests(ws: Workspace) -> dict:
    tests = ws.read_json("e2e_tests.json")
    sd = ws.read_json("skill_diff.json")
    status, relocated = sd["status"], sd["relocated"]
    ep = ws.read_json("endpoint_diff.json")
    drifted = set(ep["changed"]) | set(ep["removed"])
    live_reqs = {r["req"] for s in ws.read_json("skills.json") for r in s["rules"]}

    verdicts, stale = {}, []
    for t in tests:
        target = t["target_skill"]
        used = set(t["skills"])
        methods = {st["method"] for st in t["steps"]}
        if status.get(target) == "obsolete" or not set(t["requirements"]) & live_reqs:
            v, why = "OBSOLETE", "its skill or requirements were removed from the docs"
        elif target in relocated:
            v, why = "UPDATE_REQUIRED", f"its skill moved to {relocated[target]}"
        elif status.get(target) == "updated":
            v, why = "UPDATE_REQUIRED", "the requirements of its skill changed"
        elif methods & drifted:
            v, why = "UPDATE_REQUIRED", f"API contract changed: {', '.join(sorted(methods & drifted))}"
        elif any(status.get(s) in ("updated", "obsolete") or s in relocated for s in used - {target}):
            v, why = "UNCERTAIN", "a setup skill it uses changed; kept as is, flagged for review"
            stale.append({"test_id": t["id"], "reason": why})
        else:
            v, why = "VALID", "nothing it depends on changed"
        verdicts[t["id"]] = {"verdict": v, "reason": why, "target_skill": target}

    tested = {t["target_skill"] for t in tests}
    new_targets = sorted(s for s, st in status.items() if st == "new" and s not in tested
                         and not s.startswith("testing."))
    upd_targets = sorted({relocated.get(v["target_skill"], v["target_skill"])
                          for v in verdicts.values() if v["verdict"] == "UPDATE_REQUIRED"})
    ws.write_json("test_classification.json", {"tests": verdicts, "new_skill_targets": new_targets,
                                               "update_targets": upd_targets})
    ws.write_json("stale_tests.json", stale)

    counts = {k: 0 for k in ("VALID", "UPDATE_REQUIRED", "UNCERTAIN", "OBSOLETE")}
    for v in verdicts.values():
        counts[v["verdict"]] += 1
    counts["NEW_skills"] = len(new_targets)

    lines = ["# Gate 2: what diff-runner will change", "",
             "Only UPDATE_REQUIRED tests and tests for NEW skills are regenerated. VALID and UNCERTAIN tests are left",
             "as they are; OBSOLETE tests are deleted. Edit `test_classification.json` to override a verdict, then",
             "run `rga approve`.", "", "| Test | Verdict | Reason |", "|---|---|---|"]
    for tid, v in verdicts.items():
        if v["verdict"] != "VALID":
            lines.append(f"| {tid} | {v['verdict']} | {v['reason']} |")
    lines += ["", f"VALID (untouched): {counts['VALID']}", "",
              "New skills that will get tests: " + (", ".join(f"`{s}`" for s in new_targets) or "none")]
    ws.write_text("e2e_tests.md", "\n".join(lines) + "\n")
    return counts


def regenerate(ws: Workspace, llm) -> dict:
    tests = ws.read_json("e2e_tests.json")
    cls = ws.read_json("test_classification.json")
    verdicts = cls["tests"]
    targets = set(cls["update_targets"]) | set(cls["new_skill_targets"])

    replaced = [t for t in tests if verdicts.get(t["id"], {}).get("verdict") == "UPDATE_REQUIRED"]
    obsolete = [t for t in tests if verdicts.get(t["id"], {}).get("verdict") == "OBSOLETE"]
    keep = [t for t in tests if t not in replaced and t not in obsolete]

    fresh, gaps = design.design(ws, llm, skill_ids=targets) if targets else ([], [])
    # reuse the old ID when a fresh test covers the same requirements as a replaced one
    free = {tuple(sorted(t["requirements"])): t["id"] for t in replaced}
    next_id = max([int(t["id"].split("-")[1]) for t in tests] or [0]) + 1
    regenerated = []
    for t in fresh:
        key = tuple(sorted(t["requirements"]))
        if key in free:
            t["id"] = free.pop(key)
        else:
            t["id"] = f"E2E-{next_id:03d}"
            next_id += 1
        regenerated.append(t)

    for t in obsolete + replaced:
        for f in ws.tests_dir.glob(f"test_{t['id'].lower().replace('-', '_')}_*.py"):
            f.unlink()
    result = codegen.generate(ws, regenerated)
    final = sorted(keep + regenerated, key=lambda t: t["id"])
    ws.write_json("e2e_tests.json", final)
    old_gaps = [g for g in ws.read_json("design_gaps.json", default=[]) if g["skill"] not in targets]
    ws.write_json("design_gaps.json", old_gaps + gaps)
    ws.write_json("codegen_report.json", result)
    return {"regenerated": len(regenerated), "deleted": len(obsolete) + len(free),
            "untouched": len(keep), "llm_designed_skills": len(targets) if llm else 0}


def finalize(ws: Workspace, state_file: StateFile) -> dict:
    report = {name: ws.read_json(f, default={}) for name, f in (
        ("docs", "doc_diff.json"), ("endpoints", "endpoint_diff.json"))}
    sd = ws.read_json("skill_diff.json")
    tc = ws.read_json("test_classification.json")
    lines = ["# diff-runner report", "",
             f"- Docs changed: {', '.join(report['docs'].get('changed', [])) or 'none'}; added: "
             f"{', '.join(report['docs'].get('added', [])) or 'none'}; removed: "
             f"{', '.join(report['docs'].get('removed', [])) or 'none'}",
             f"- API methods changed: {', '.join(report['endpoints'].get('changed', [])) or 'none'}; added: "
             f"{', '.join(report['endpoints'].get('added', [])) or 'none'}", "", "## Skills", ""]
    for sid, st in sorted(sd["status"].items()):
        if st != "unchanged":
            lines.append(f"- `{sid}`: {st}" + (f" → `{sd['relocated'][sid]}`" if sid in sd["relocated"] else ""))
    lines += ["", "## Tests", ""]
    for tid, v in tc["tests"].items():
        if v["verdict"] != "VALID":
            lines.append(f"- {tid}: **{v['verdict']}** ({v['reason']})")
    lines.append(f"- {sum(v['verdict'] == 'VALID' for v in tc['tests'].values())} tests VALID and untouched")
    ws.write_text("diff_report.md", "\n".join(lines) + "\n")
    state_file.reset()  # ready for the next documentation update
    return {"report": "diff_report.md"}


def build_diff_graph(ws: Workspace, llm, auto_approve: bool = False):
    sf = StateFile(ws, DIFF_ORDER, "diff")
    steps = {
        "doc_diff": lambda: doc_diff(ws),
        "skill_diff": lambda: skill_diff(ws, llm),
        "endpoint_diff": lambda: endpoint_diff(ws),
        "remap": lambda: remap(ws, llm),
        "classify_tests": lambda: classify_tests(ws),
        "regenerate": lambda: regenerate(ws, llm),
        "envsetup": lambda: envsetup.run(ws),
        "execute": lambda: execute.run(ws),
        "triage": lambda: triage.run(ws, llm),
        "coverage": lambda: coverage.run(ws),
        "finalize": lambda: finalize(ws, sf),
    }
    redo_targets = {"test_spec_review": "classify_tests", "code_env_review": "regenerate"}
    g = StateGraph(RunState)
    for name in DIFF_ORDER:
        if name in GATES:
            g.add_node(name, gate_node(name, ws, sf, auto_approve))
        else:
            g.add_node(name, stage_node(name, steps[name], sf))
    g.add_edge(START, DIFF_ORDER[0])
    for a, b in zip(DIFF_ORDER, DIFF_ORDER[1:]):
        if a in GATES:
            redo = redo_targets[a]
            g.add_conditional_edges(
                a, lambda s, a=a, b=b, redo=redo: redo if s.get("gates", {}).get(a, {}).get("action") == "redo" else b,
                [b, redo])
        else:
            g.add_edge(a, b)
    g.add_edge(DIFF_ORDER[-1], END)
    return g, sf


__all__ = ["build_diff_graph", "DIFF_ORDER", "after_gate"]
