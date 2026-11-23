"""Stage 10: coverage map.

Cross-references requirements, skills, tests, execution and triage into six
coverage dimensions, and reverse-checks which real API methods were actually
called. Favors clarity about gaps and confidence over a raw test count.
"""
from __future__ import annotations

import json

from ..workspace import Workspace


def run(ws: Workspace) -> dict:
    skills = ws.read_json("skills.json")
    spec = ws.read_json("grpc_spec.json")
    mapping = ws.read_json("skill_endpoint_map.json")
    tests = ws.read_json("e2e_tests.json")
    results = ws.read_json("execution_results.json", default={"tests": {}})["tests"]
    triage = ws.read_json("triage.json", default={})
    gaps_designed = ws.read_json("design_gaps.json", default=[])
    stale = ws.read_json("stale_tests.json", default=[])
    excluded_md = ws.path("excluded_actions.md")
    excluded = excluded_md.read_text().count("**Reason:**") if excluded_md.exists() else 0

    all_reqs = {r["req"]: s["id"] for s in skills for r in s["rules"]}
    by_req = {q: [] for q in all_reqs}
    for t in tests:
        for q in t["requirements"]:
            by_req.setdefault(q, []).append(t["id"])
    req_rows = {}
    for q, tids in sorted(by_req.items()):
        outcomes = [results.get(t, {}).get("outcome", "not_run") for t in tids]
        state = ("uncovered" if not tids else "failing" if "failed" in outcomes
                 else "passing" if all(o == "passed" for o in outcomes) else "not_run")
        req_rows[q] = {"skill": all_reqs.get(q), "tests": tids, "status": state}

    tested_skills = {t["target_skill"] for t in tests}
    called = set()
    for ev in ws.logs_dir.glob("*.evidence.json"):
        for st in json.loads(ev.read_text())["steps"]:
            if st.get("response"):
                called.add(st["rpc"])
    endpoints = {m: ("called" if m in called else "never_called") for m in spec["methods"]}

    low_maps = [{"skill_id": k, "confidence": v["confidence"], "endpoint": v.get("method")}
                for k, v in mapping.items() if v["confidence"] < 0.7]
    low_tests = [{"test_id": t["id"], "confidence": t["confidence"], "title": t["title"]}
                 for t in tests if t["confidence"] < 60]

    cmap = {
        "summary": {
            "requirements": len(req_rows),
            "requirements_covered": sum(r["status"] != "uncovered" for r in req_rows.values()),
            "requirements_failing": sum(r["status"] == "failing" for r in req_rows.values()),
            "skills": len(skills), "skills_with_tests": len(tested_skills & {s["id"] for s in skills}),
            "endpoints": len(endpoints), "endpoints_called": len(called & set(endpoints)),
            "tests": len(tests), "passed": sum(r.get("outcome") == "passed" for r in results.values()),
            "failed": sum(r.get("outcome") == "failed" for r in results.values()),
            "platform_bug_reports": len({t.get("bug_report") for t in triage.values() if t.get("bug_report")}),
            "excluded_actions": excluded,
        },
        "requirements": req_rows,
        "skills_without_tests": sorted({s["id"] for s in skills} - tested_skills),
        "endpoints": endpoints,
        "low_confidence_mappings": low_maps,
        "low_confidence_tests": low_tests,
        "stale_tests": stale,
    }
    ws.write_json("coverage_map.json", cmap)
    gaps = {
        "uncovered_requirements": [q for q, r in req_rows.items() if r["status"] == "uncovered"],
        "why_not_designed": gaps_designed,
        "endpoints_never_called": [m for m, s in endpoints.items() if s == "never_called"],
        "skills_without_tests": cmap["skills_without_tests"],
    }
    ws.write_json("coverage_gaps.json", gaps)

    s = cmap["summary"]
    pct = lambda a, b: f"{(100 * a / b):.0f}%" if b else "—"
    lines = [f"# Coverage map: {ws.cfg.platform_name}", "",
             "| Dimension | Result |", "|---|---|",
             f"| Requirements covered by a test | {s['requirements_covered']}/{s['requirements']} ({pct(s['requirements_covered'], s['requirements'])}) |",
             f"| Requirements with a failing test | {s['requirements_failing']} |",
             f"| Skills with at least one test | {s['skills_with_tests']}/{s['skills']} |",
             f"| API methods actually called (reverse check) | {s['endpoints_called']}/{s['endpoints']} |",
             f"| Low-confidence mappings (< 0.7) | {len(low_maps)} |",
             f"| Low-confidence tests (< 60) | {len(low_tests)} |",
             f"| Stale tests | {len(stale)} |",
             f"| Excluded doc lines (not testable via API) | {excluded} |",
             "", f"Tests: {s['tests']} · passed {s['passed']} · failed {s['failed']} · "
             f"bug reports {s['platform_bug_reports']}", "", "## Requirements", "",
             "| Requirement | Skill | Tests | Status |", "|---|---|---|---|"]
    icon = {"passing": "✅ passing", "failing": "❌ failing", "uncovered": "⚪ uncovered", "not_run": "… not run"}
    for q, r in req_rows.items():
        lines.append(f"| {q} | `{r['skill']}` | {', '.join(r['tests']) or '—'} | {icon[r['status']]} |")
    if gaps["why_not_designed"]:
        lines += ["", "## Why some requirements have no test", ""]
        lines += [f"- {g.get('requirement') or '—'}: {g['reason']}" for g in gaps["why_not_designed"]]
    lines += ["", "## API methods never called", ""] + [f"- `{m}`" for m in gaps["endpoints_never_called"]]
    if low_maps:
        lines += ["", "## Low-confidence mappings", ""] + [f"- `{m['skill_id']}` → `{m['endpoint']}` ({m['confidence']})"
                                                         for m in low_maps]
    if stale:
        lines += ["", "## Stale tests", ""] + [f"- {t['test_id']}: {t['reason']}" for t in stale]
    ws.write_text("coverage_map.md", "\n".join(lines) + "\n")
    return {"requirements_covered": f"{s['requirements_covered']}/{s['requirements']}",
            "endpoints_called": f"{s['endpoints_called']}/{s['endpoints']}",
            "low_confidence_tests": len(low_tests)}
