"""End-to-end: the real CLI in a subprocess, offline mode, against the demo server."""
import json

from conftest import ROOT, make_config, rga


def state(tmp_path):
    return json.loads((tmp_path / "ws/output/state.json").read_text())


def test_gates_pause_and_resume(tmp_path):
    cfg = str(make_config(tmp_path))
    r = rga("run", "-c", cfg)
    assert r.returncode == 3 and "Gate 1" in r.stdout
    assert state(tmp_path)["stages"]["skill_review"]["status"] == "waiting_for_review"
    assert rga("approve", "-c", cfg).returncode == 3          # -> Gate 2
    r = rga("redo", "-c", cfg)                                # regenerate the design, pause at Gate 2 again
    assert r.returncode == 3 and "Gate 2" in r.stdout
    assert rga("approve", "-c", cfg).returncode == 3          # -> Gate 3
    r = rga("approve", "-c", cfg)
    assert r.returncode == 0, r.stdout + r.stderr
    st = state(tmp_path)["stages"]
    assert st["test_spec_review"]["decision"]["action"] == "approve"
    assert st["execute"]["result"]["failed"] == 1
    triage = json.loads((tmp_path / "ws/output/triage.json").read_text())
    (bug,) = triage.values()
    assert bug["classification"] == "PLATFORM_BUG"
    report = next((tmp_path / "ws/output/bug_reports").glob("*.md")).read_text()
    assert "REQ-014" in report and "PERMISSION_DENIED" in report
    assert rga("approve", "-c", cfg).returncode == 2          # nothing left to approve


def test_no_false_positives_on_the_fixed_server(tmp_path):
    cfg = str(make_config(tmp_path, fixed=True))
    r = rga("run", "-c", cfg, "--auto-approve")
    assert r.returncode == 0, r.stdout + r.stderr
    res = json.loads((tmp_path / "ws/output/execution_results.json").read_text())["summary"]
    assert res["failed"] == 0 and res["passed"] == res["total"] > 20


def test_crash_then_resume_without_rework(tmp_path):
    cfg_path = make_config(tmp_path, address="127.0.0.1:1")   # nothing listens there
    r = rga("run", "-c", str(cfg_path), "--auto-approve")
    assert r.returncode == 1 and "pre-flight failed" in r.stderr
    st = state(tmp_path)["stages"]
    assert st["execute"]["status"] == "failed" and st["coverage"]["status"] == "pending"
    ingest_time = st["ingest"]["updated"]
    cfg = json.loads(cfg_path.read_text())
    cfg["target"].pop("address")
    cfg_path.write_text(json.dumps(cfg))
    r = rga("run", "-c", str(cfg_path), "--auto-approve")
    assert r.returncode == 0 and "Resuming" in r.stdout, r.stdout + r.stderr
    st = state(tmp_path)["stages"]
    assert st["ingest"]["updated"] == ingest_time and st["coverage"]["status"] == "approved"


def test_diff_runner_updates_only_what_changed(tmp_path):
    cfg_path = make_config(tmp_path)
    assert rga("run", "-c", str(cfg_path), "--auto-approve").returncode == 0
    before = {p.name: p.read_text() for p in (tmp_path / "ws/output/tests/generated").glob("test_*.py")}
    ws = tmp_path / "ws"
    import shutil
    shutil.rmtree(ws / "docs")
    shutil.copytree(ROOT / "sample_service/docs_v2", ws / "docs")
    shutil.copy(ROOT / "sample_service/protos/shelfwise_v2.proto", ws / "shelfwise.proto")
    r = rga("diff", "-c", str(cfg_path), "--auto-approve")
    assert r.returncode == 0, r.stdout + r.stderr
    out = ws / "output"
    sd = json.loads((out / "skill_diff.json").read_text())["status"]
    assert sd["catalog.book.list"] == "obsolete" and sd["lending.loan.renew"] == "updated"
    assert sd["members.member.update"] == "new" and sd["fines.account.pay"] == "relocated"
    ep = json.loads((out / "endpoint_diff.json").read_text())
    assert ep["changed"] == ["HoldService.ListHolds"] and ep["added"] == ["MemberService.UpdateMemberEmail"]
    verdicts = {k: v["verdict"] for k, v in json.loads((out / "test_classification.json").read_text())["tests"].items()}
    assert "OBSOLETE" in verdicts.values() and "UNCERTAIN" in verdicts.values()
    after = {p.name: p.read_text() for p in (out / "tests/generated").glob("test_*.py")}
    valid = [k for k, v in verdicts.items() if v in ("VALID", "UNCERTAIN")]
    for tid in valid:  # untouched byte for byte
        name = next(n for n in before if n.startswith(f"test_{tid.lower().replace('-', '_')}_"))
        assert after[name] == before[name]
    assert json.loads((out / "state.json").read_text())["stages"]["doc_diff"]["status"] == "pending"  # auto-reset
    res = json.loads((out / "execution_results.json").read_text())["summary"]
    assert res["failed"] == 1  # still only the planted bug
