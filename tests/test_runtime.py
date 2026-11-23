"""The harness that generated tests use, against a live in-process server."""
import json

import pytest

from regression_agent.runtime import Harness, StepFailure, get_path
from sample_service.server import DEFAULT_PROTO, start_server


@pytest.fixture(scope="module")
def server():
    srv, port, _ = start_server(0, fixed=True)
    yield f"127.0.0.1:{port}"
    srv.stop(0)


def harness(server, tmp_path):
    h = Harness(server, str(DEFAULT_PROTO), tmp_path, reset_rpc="TestSupportService.Reset")
    h.test_id = "E2E-T"
    h.reset()
    return h


def test_get_path():
    data = {"holds": [{"status": "A"}, {"status": "B"}], "x": {"y": 1}}
    assert get_path(data, "holds[1].status") == "B"
    assert get_path(data, "#holds") == 2 and get_path(data, "x.y") == 1
    with pytest.raises(KeyError):
        get_path(data, "holds[5].status")


def test_refs_expectations_and_evidence(server, tmp_path):
    h = harness(server, tmp_path)
    h.call(1, "catalog.book.create", "CatalogService.CreateBook",
           {"title": "Dune", "author": "F", "isbn": "1", "total_copies": 2}, save_as="book")
    h.call(2, "catalog.book.get", "CatalogService.GetBook", {"book_id": "${book.book_id}"},
           expect_fields={"available_copies": 2, "title": "Dune"})
    h.call(3, "catalog.book.get", "CatalogService.GetBook", {"book_id": "nope"}, expect_code="NOT_FOUND")
    with pytest.raises(StepFailure) as e:
        h.call(4, "catalog.book.get", "CatalogService.GetBook", {"book_id": "${book.book_id}"},
               expect_fields={"available_copies": 3})
    assert e.value.kind == "expectation_field"
    path = h.write_evidence("failed")
    ev = json.loads(path.read_text())
    assert ev["failure"]["step"] == 4 and ev["steps"][0]["response"]["grpc_status"] == "OK"
    assert ev["steps"][3]["mismatches"] == [{"path": "available_copies", "expected": 3, "actual": 2}]


def test_harness_errors_are_labelled(server, tmp_path):
    h = harness(server, tmp_path)
    with pytest.raises(StepFailure) as e:
        h.call(1, "x", "CatalogService.GetBook", {"book_id": "${missing.book_id}"})
    assert e.value.kind == "harness"
    with pytest.raises(StepFailure) as e:
        h.call(1, "x", "CatalogService.Nope", {})
    assert e.value.kind == "harness"


def test_unreachable_target_is_transport(tmp_path):
    h = Harness("127.0.0.1:1", str(DEFAULT_PROTO), tmp_path, timeout=1)
    with pytest.raises(StepFailure) as e:
        h.call(1, "x", "CatalogService.GetBook", {"book_id": "a"})
    assert e.value.kind == "transport"
