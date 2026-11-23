"""Unit tests for the deterministic parts of each stage."""
import json

from regression_agent.config import PipelineConfig
from regression_agent.stages import design, ingest, mapping, skills, spec
from regression_agent.stages.spec import method_signature, parse_proto
from regression_agent.workspace import Workspace

from conftest import ROOT


def ws_for(config):
    return Workspace(PipelineConfig.load(config))


def test_ingest_mirrors_docs_and_hard_stops_when_empty(config, tmp_path):
    ws = ws_for(config)
    assert ingest.run(ws) == {"docs": 6}
    manifest = ws.read_json("manifest.json")
    assert (ws.docs_mirror / "lending.md").read_bytes() == (ws.cfg.docs_path / "lending.md").read_bytes()
    assert len(manifest["lending.md"]) == 32
    for f in ws.cfg.docs_path.iterdir():
        f.unlink()
    try:
        ingest.run(ws)
        raise AssertionError("expected EmptyDocsError")
    except ingest.EmptyDocsError:
        pass


def test_parse_doc_keeps_line_numbers():
    units = skills.parse_doc("x.md", "# T\n\n- **REQ-001** Do a thing.\n- A user story\n")
    assert [(u.req_id, u.line) for u in units] == [("REQ-001", 3), (None, 4)]


def test_heuristic_skills_assign_every_requirement(config):
    ws = ws_for(config)
    ingest.run(ws)
    out = skills.run(ws, None)
    found = {r for s in ws.read_json("skills.json") for r in s["requirements"]}
    expected = set()
    for f in ws.docs_mirror.glob("*.md"):
        expected |= {u.req_id for u in skills.parse_doc(f.name, f.read_text()) if u.req_id}
    assert found == expected and out["excluded"] == 7
    ids = {s["id"] for s in ws.read_json("skills.json")}
    assert {"catalog.book.create", "lending.loan.renew", "members.member.suspend"} <= ids
    create = next(s for s in ws.read_json("skills.json") if s["id"] == "catalog.book.create")
    assert create["requirements"] == ["REQ-001", "REQ-002", "REQ-003"]  # split by action, not outcome


def test_proto_parser_and_client_wrappers():
    sp = spec.build_spec(ROOT / "sample_service/protos/shelfwise.proto", ROOT / "sample_service/client.py")
    assert len(sp["methods"]) == 21
    m = sp["methods"]["LendingService.CheckoutBook"]
    assert m["request_message"] == "CheckoutBookRequest" and m["client_method"] == "checkout_book"
    assert {f["name"] for f in m["response_fields"]} >= {"loan_id", "due_day", "status"}
    unwrapped = sorted(k for k, v in sp["methods"].items() if not v["client_method"])
    assert unwrapped == ["TestSupportService.AdvanceClock", "TestSupportService.Reset"]
    assert "MEMBER_STATUS_SUSPENDED" in sp["enums"]["MemberStatus"]


def test_method_signature_detects_contract_change():
    v1 = parse_proto("service S { rpc A(Req) returns (Resp); } message Req { string id = 1; } message Resp {}")
    v2 = parse_proto("service S { rpc A(Req) returns (Resp); } message Req { string id = 1; bool x = 2; } message Resp {}")
    sig = lambda p: method_signature({"request_message": "Req", "response_message": "Resp",
                                      "request_fields": p["messages"]["Req"], "response_fields": []})
    assert sig(v1) != sig(v2)


def test_proto_parser_handles_nested_braces_and_comments():
    p = parse_proto("// c\nmessage Outer { message Inner { int32 a = 1; } repeated Inner items = 2; /* x */ }")
    assert {"name": "items", "type": "Inner", "repeated": True} in p["messages"]["Outer"]


def _prepared(config):
    ws = ws_for(config)
    ingest.run(ws)
    skills.run(ws, None)
    spec.run(ws)
    mapping.run(ws, None)
    return ws


def test_mapping_is_deterministic_for_clear_cases(config):
    ws = _prepared(config)
    m = ws.read_json("skill_endpoint_map.json")
    assert m["lending.loan.renew"]["method"] == "LendingService.RenewLoan"
    assert m["billing.account.pay"]["method"] == "BillingService.PayFine"
    assert m["lending.loan.renew"]["match_method"] == "deterministic"
    assert m["testing.service.reset"]["status"] == "low_confidence"
    assert "Gate 1" in ws.path("skills_review.md").read_text()


def test_human_mapping_survives_a_rerun(config):
    ws = _prepared(config)
    m = ws.read_json("skill_endpoint_map.json")
    m["testing.service.reset"].update(match_method="human", confidence=1.0, status="mapped")
    ws.write_json("skill_endpoint_map.json", m)
    mapping.run(ws, None)
    assert ws.read_json("skill_endpoint_map.json")["testing.service.reset"]["match_method"] == "human"


def test_validate_rejects_invented_calls(config):
    ws = _prepared(config)
    sp, mp = ws.read_json("grpc_spec.json"), ws.read_json("skill_endpoint_map.json")
    good = {"steps": [{"skill": "catalog.book.create", "method": "CatalogService.CreateBook",
                       "request": {"title": "t", "isbn": "1", "total_copies": 1}, "save_as": "book",
                       "expect": {"code": "OK", "fields": {"available_copies": 1}}},
                      {"skill": "catalog.book.get", "method": "CatalogService.GetBook",
                       "request": {"book_id": "${book.book_id}"}, "expect": {"code": "OK"}}]}
    assert design.validate(good, sp, mp) == []
    bad = json.loads(json.dumps(good))
    bad["steps"][0]["request"]["colour"] = "red"
    bad["steps"][1]["method"] = "CatalogService.ListBooks"
    bad["steps"][0]["expect"]["fields"] = {"rating": 5}
    errors = design.validate(bad, sp, mp)
    assert any("no field 'colour'" in e for e in errors)
    assert any("maps to CatalogService.GetBook" in e for e in errors)
    assert any("no field 'rating'" in e for e in errors)
    early = {"steps": [{"skill": "catalog.book.get", "method": "CatalogService.GetBook",
                        "request": {"book_id": "${book.book_id}"}, "expect": {"code": "OK"}}]}
    assert any("hasn't run yet" in e for e in design.validate(early, sp, mp))


def test_heuristic_design_builds_state_setup(config):
    ws = _prepared(config)
    design.run(ws, None)
    tests = ws.read_json("e2e_tests.json")
    t = next(t for t in tests if t["requirements"] == ["REQ-014"])
    assert [s["skill"] for s in t["steps"]][:2] == ["members.member.register", "members.member.suspend"]
    assert t["steps"][-1]["expect"]["code"] == "PERMISSION_DENIED"
    sp, mp = ws.read_json("grpc_spec.json"), ws.read_json("skill_endpoint_map.json")
    assert all(design.validate(t, sp, mp) == [] for t in tests)
    # every requirement is either tested or explained
    gaps = {g["requirement"] for g in ws.read_json("design_gaps.json")}
    tested = {q for t in tests for q in t["requirements"]}
    all_reqs = {r for s in ws.read_json("skills.json") if s["domain"] != "testing" for r in s["requirements"]}
    assert all_reqs == tested | gaps


def test_skill_graph_edges(config):
    ws = _prepared(config)
    g = design.build_graph(ws.read_json("skills.json"), ws.read_json("grpc_spec.json"),
                           ws.read_json("skill_endpoint_map.json"))
    assert "lending.book.checkout" in g["edges"]["catalog.book.create"]
    assert "lending.loan.renew" in g["edges"]["lending.book.checkout"]
