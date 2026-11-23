"""Stage 7: environment setup, followed by Gate 3 (code + .env review).

Compiles the gRPC stubs (rebuilt automatically when the .proto changes) and
walks the generated code's syntax tree to find exactly which environment
variables it reads. Writes config.py and .env so a reviewer sees every setting
and credential before anything runs against a live system.
"""
from __future__ import annotations

import ast

from ..protoc import compile_proto
from ..workspace import Workspace

SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def env_vars_used(paths) -> list[str]:
    found = set()
    for p in paths:
        for node in ast.walk(ast.parse(p.read_text())):
            # os.environ["X"]
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "environ" and isinstance(node.slice, ast.Constant)):
                found.add(node.slice.value)
            # os.environ.get("X") / os.getenv("X")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args \
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                f = node.func
                if f.attr == "getenv" or (f.attr == "get" and isinstance(f.value, ast.Attribute)
                                          and f.value.attr == "environ"):
                    found.add(node.args[0].value)
    return sorted(found)


def default_values(ws: Workspace) -> dict[str, str]:
    t = ws.cfg.target
    return {
        "RGA_TARGET": t.address or "launched-by-rga",
        "RGA_PROTO": str(ws.cfg.proto_path),
        "RGA_RESET_RPC": t.reset_rpc or "",
        "RGA_EVIDENCE_DIR": str(ws.logs_dir),
    }


def read_env(ws: Workspace) -> dict[str, str]:
    p = ws.path(".env")
    if not p.exists():
        return {}
    env = {}
    for line in p.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def run(ws: Workspace) -> dict:
    pb2, _ = compile_proto(ws.cfg.proto_path)
    files = sorted(ws.tests_dir.glob("*.py"))
    names = env_vars_used(files)
    existing = read_env(ws)
    defaults = default_values(ws)
    values = {n: existing.get(n, defaults.get(n, "")) for n in names}

    ws.write_text("config.py", '"""Settings the generated tests read (discovered from their code)."""\nimport os\n\n'
                  + "\n".join(f'{n} = os.getenv("{n}")' for n in names) + "\n")
    ws.write_text(".env", "# Discovered by stage 7 (env setup). Review before running live.\n"
                  + "\n".join(f"{n}={values[n]}" for n in names) + "\n")

    codegen = ws.read_json("codegen_report.json", default={"written": [], "rejected": []})
    missing = [n for n in names if not values[n]]
    lines = ["# Gate 3: code + .env review", "",
             "Look over the generated tests and the settings below before anything runs against a live system.",
             "Edit `.env` if a value is wrong, then run `rga approve`.", "",
             f"- Generated test files: {len(codegen['written'])} in `tests/generated/`",
             f"- Rejected at code generation: {len(codegen['rejected'])}",
             f"- gRPC stubs compiled from `{ws.cfg.proto_path.name}` ({len(pb2.DESCRIPTOR.services_by_name)} services)",
             "", "| Variable | Value |", "|---|---|"]
    for n in names:
        shown = "••••••" if any(h in n for h in SECRET_HINTS) and values[n] else (values[n] or "**missing**")
        lines.append(f"| `{n}` | `{shown}` |")
    for r in codegen["rejected"]:
        lines.append(f"\n- Rejected {r['id']}: {'; '.join(r['errors'])}")
    ws.write_text("env_review.md", "\n".join(lines) + "\n")
    return {"config_vars": len(names), "missing": len(missing)}
