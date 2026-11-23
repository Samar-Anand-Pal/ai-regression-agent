"""Runtime used by generated tests.

Each generated test is a list of `rga.call(...)` lines. The harness resolves
`${name.field}` references, builds the request from the .proto, calls the RPC,
checks the status code and fields, and records everything (request, response,
status, timing) as evidence. Triage reads that evidence, never the test code.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import grpc
from google.protobuf import json_format

from .protoc import compile_proto

REF = re.compile(r"\$\{([a-z_][a-z0-9_]*)\.([a-z0-9_.\[\]#]+)\}")
TRANSPORT = {"UNAVAILABLE", "DEADLINE_EXCEEDED"}


class StepFailure(AssertionError):
    """A step did not behave as the spec says. `kind` drives triage."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # expectation_code | expectation_field | transport | harness


def get_path(data, path: str):
    """'holds[0].status' and '#holds' (length) on a MessageToDict result."""
    if path.startswith("#"):
        v = get_path(data, path[1:])
        return len(v) if isinstance(v, list) else 0
    cur = data
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            idx = int(part[1:-1])
            if not isinstance(cur, list) or idx >= len(cur):
                raise KeyError(path)
            cur = cur[idx]
        else:
            if not isinstance(cur, dict) or part not in cur:
                raise KeyError(path)
            cur = cur[part]
    return cur


class Harness:
    def __init__(self, target: str, proto: str, evidence_dir: str | Path, reset_rpc: str | None = None,
                 timeout: float = 10.0):
        self.pb2, self.grpc_mod = compile_proto(proto)
        self.channel = grpc.insecure_channel(target)
        self.timeout = timeout
        self.evidence_dir = Path(evidence_dir)
        self.reset_rpc = reset_rpc
        self.saved: dict[str, dict] = {}
        self.steps: list[dict] = []
        self.failure: dict | None = None
        self.test_id = "unknown"
        self.meta: dict = {}

    # ------------------------------------------------------------ helpers
    def _resolve(self, value):
        if isinstance(value, dict):
            return {k: self._resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v) for v in value]
        if isinstance(value, str):
            whole = REF.fullmatch(value)
            if whole:
                return self._lookup(*whole.groups())
            return REF.sub(lambda m: str(self._lookup(*m.groups())), value)
        return value

    def _lookup(self, name, path):
        if name not in self.saved:
            raise StepFailure("harness", f"${{{name}.{path}}}: no earlier step saved '{name}'")
        try:
            return get_path(self.saved[name], path)
        except KeyError:
            raise StepFailure("harness", f"${{{name}.{path}}}: '{name}' has no field '{path}'")

    def _method(self, method: str):
        service, rpc = method.split(".")
        svc = self.pb2.DESCRIPTOR.services_by_name.get(service)
        if svc is None or rpc not in svc.methods_by_name:
            raise StepFailure("harness", f"{method} is not in the compiled .proto")
        req_cls = getattr(self.pb2, svc.methods_by_name[rpc].input_type.name)
        stub = getattr(self.grpc_mod, f"{service}Stub")(self.channel)
        return getattr(stub, rpc), req_cls

    @staticmethod
    def _to_dict(msg):
        return json_format.MessageToDict(msg, always_print_fields_with_no_presence=True,
                                         preserving_proto_field_name=True)

    # ------------------------------------------------------------ API used by generated tests
    def reset(self):
        if self.reset_rpc:
            fn, req_cls = self._method(self.reset_rpc)
            fn(req_cls(), timeout=self.timeout)

    def call(self, step: int, skill: str, method: str, request: dict, expect_code: str = "OK",
             expect_fields: dict | None = None, save_as: str | None = None) -> dict | None:
        record = {"step": step, "skill_id": skill, "rpc": method, "expected": {"code": expect_code,
                  "fields": expect_fields or {}}}
        self.steps.append(record)
        try:
            body = self._resolve(request)
            record["request"] = {"rpc": method, "body": body}
            fn, req_cls = self._method(method)
            msg = json_format.ParseDict(body, req_cls())
        except StepFailure as e:
            return self._fail(record, e)
        except json_format.ParseError as e:
            return self._fail(record, StepFailure("harness", f"request does not fit {method}: {e}"))

        started = time.perf_counter()
        try:
            resp = fn(msg, timeout=self.timeout)
            code, details, data = "OK", "", self._to_dict(resp)
        except grpc.RpcError as e:
            code, details, data = e.code().name, e.details() or "", None
        record["response"] = {"grpc_status": code, "details": details, "body": data}
        record["ms"] = round((time.perf_counter() - started) * 1000, 1)

        if code != expect_code:
            kind = "transport" if code in TRANSPORT else "expectation_code"
            return self._fail(record, StepFailure(
                kind, f"step {step} {method}: expected {expect_code}, got {code}" + (f" ({details})" if details else "")))
        mismatches = []
        for path, want in (expect_fields or {}).items():
            want = self._resolve(want)
            try:
                got = get_path(data, path)
            except KeyError:
                got = "<missing>"
            if got != want:
                mismatches.append({"path": path, "expected": want, "actual": got})
        if mismatches:
            record["mismatches"] = mismatches
            desc = "; ".join(f"{m['path']}: expected {m['expected']!r}, got {m['actual']!r}" for m in mismatches)
            return self._fail(record, StepFailure("expectation_field", f"step {step} {method}: {desc}"))
        record["outcome"] = "passed"
        if save_as:
            self.saved[save_as] = data
        return data

    def _fail(self, record, err: StepFailure):
        record["outcome"] = "failed"
        record["error"] = str(err)
        self.failure = {"step": record["step"], "kind": err.kind, "message": str(err), "skill_id": record["skill_id"],
                        "rpc": record["rpc"]}
        raise err

    # ------------------------------------------------------------ evidence
    def write_evidence(self, outcome: str, error: str | None = None) -> Path:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        if outcome == "failed" and self.failure is None:
            self.failure = {"step": None, "kind": "harness", "message": error or "test failed outside a step"}
        path = self.evidence_dir / f"{self.test_id}.evidence.json"
        path.write_text(json.dumps({"test_id": self.test_id, "outcome": outcome, **self.meta,
                                    "failure": self.failure, "steps": self.steps}, indent=2) + "\n")
        return path
