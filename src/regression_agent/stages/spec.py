"""Stage 3: API spec build.

Parses the .proto with a small brace-balanced parser (services, RPCs, messages,
fields, enums) and cross-references the SDK client source to record which RPCs
have a wrapper method. The result, grpc_spec.json, is the only source of RPC
and field names later stages may use, so nothing can be invented.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from ..workspace import Workspace

COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
BLOCK_START = re.compile(r"\b(message|enum|service)\s+(\w+)\s*\{")
FIELD = re.compile(r"^(repeated\s+|optional\s+)?([\w.]+)\s+(\w+)\s*=\s*\d+")
RPC = re.compile(r"rpc\s+(\w+)\s*\(\s*(stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(stream\s+)?([\w.]+)\s*\)")
ENUM_VALUE = re.compile(r"^(\w+)\s*=\s*(-?\d+)")
SCALARS = {"double", "float", "int32", "int64", "uint32", "uint64", "sint32", "sint64", "fixed32",
           "fixed64", "sfixed32", "sfixed64", "bool", "string", "bytes"}


def _blocks(text: str):
    """Yields (kind, name, body) for each top-level block, honoring nested braces."""
    pos = 0
    while True:
        m = BLOCK_START.search(text, pos)
        if not m:
            return
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        if depth:
            raise ValueError(f"unbalanced braces in block {m.group(2)}")
        yield m.group(1), m.group(2), text[m.end():i - 1]
        pos = i


def parse_proto(text: str) -> dict:
    text = COMMENT.sub("", text)
    pkg = re.search(r"^\s*package\s+([\w.]+)\s*;", text, re.M)
    messages, enums, services = {}, {}, {}
    for kind, name, body in _blocks(text):
        statements = [s.strip() for s in body.replace("{", ";").replace("}", ";").split(";") if s.strip()]
        if kind == "message":
            fields = []
            for st in statements:
                f = FIELD.match(st)
                if f and f.group(2) not in ("message", "enum", "reserved", "option"):
                    fields.append({"name": f.group(3), "type": f.group(2), "repeated": bool(f.group(1))
                                   and f.group(1).strip() == "repeated"})
            messages[name] = fields
        elif kind == "enum":
            enums[name] = [v.group(1) for st in statements if (v := ENUM_VALUE.match(st))]
        else:
            services[name] = [{"rpc": r.group(1), "request": r.group(3).split(".")[-1],
                               "response": r.group(5).split(".")[-1],
                               "streaming": bool(r.group(2) or r.group(4))}
                              for r in RPC.finditer(body)]
    return {"package": pkg.group(1) if pkg else "", "messages": messages, "enums": enums, "services": services}


def client_wrappers(path: Path | None) -> dict[str, str]:
    """Maps RPC name -> SDK method name, from calls like `self._stub.CreateBook(...)` inside a method."""
    if not path or not path.exists():
        return {}
    found = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            for call in ast.walk(node):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr[:1].isupper()):
                    found.setdefault(call.func.attr, node.name)
    return found


def build_spec(proto_path: Path, client_path: Path | None) -> dict:
    parsed = parse_proto(proto_path.read_text())
    wrappers = client_wrappers(client_path)
    methods = {}
    for service, rpcs in parsed["services"].items():
        for r in rpcs:
            req = parsed["messages"].get(r["request"], [])
            resp = parsed["messages"].get(r["response"], [])
            methods[f"{service}.{r['rpc']}"] = {
                "service": service, "rpc": r["rpc"],
                "request_message": r["request"], "response_message": r["response"],
                "request_fields": req, "response_fields": resp,
                "client_method": wrappers.get(r["rpc"]),
                "streaming": r["streaming"],
            }
    return {"package": parsed["package"], "methods": methods, "messages": parsed["messages"],
            "enums": parsed["enums"]}


def method_signature(m: dict) -> str:
    """A string that changes whenever the RPC's contract changes (used for drift detection)."""
    f = lambda fs: ",".join(f"{x['name']}:{x['type']}{'[]' if x['repeated'] else ''}" for x in fs)
    return f"{m['request_message']}({f(m['request_fields'])})->{m['response_message']}({f(m['response_fields'])})"


def run(ws: Workspace) -> dict:
    spec = build_spec(ws.cfg.proto_path, ws.cfg.client_path)
    ws.write_json("grpc_spec.json", spec)
    unwrapped = [k for k, v in spec["methods"].items() if not v["client_method"]]
    return {"methods": len(spec["methods"]), "services": len({m["service"] for m in spec["methods"].values()}),
            "unwrapped": len(unwrapped)}
