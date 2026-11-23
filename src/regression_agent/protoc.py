"""Compile a .proto file into Python modules at runtime and import them.

Generated code goes into a cache folder keyed by the file's hash, so a changed
proto is recompiled automatically and nothing generated is committed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

CACHE = Path.home() / ".cache" / "ai-regression-agent" / "protos"


def compile_proto(proto_path: str | Path, cache_dir: str | Path = CACHE):
    """Returns (pb2, pb2_grpc) modules for the given .proto file."""
    from grpc_tools import protoc
    try:
        from importlib.resources import files
        include = str(files("grpc_tools").joinpath("_proto"))
    except Exception:  # pragma: no cover
        include = ""

    proto_path = Path(proto_path).resolve()
    digest = hashlib.md5(proto_path.read_bytes()).hexdigest()[:12]
    out = Path(cache_dir) / f"{proto_path.stem}_{digest}"
    stem = proto_path.stem
    pb2_file, grpc_file = out / f"{stem}_pb2.py", out / f"{stem}_pb2_grpc.py"
    if not (pb2_file.exists() and grpc_file.exists()):
        out.mkdir(parents=True, exist_ok=True)
        args = ["protoc", f"-I{proto_path.parent}", f"--python_out={out}", f"--grpc_python_out={out}"]
        if include:
            args.append(f"-I{include}")
        if protoc.main(args + [str(proto_path)]) != 0:
            raise RuntimeError(f"protoc failed for {proto_path}")

    key = f"_rgastub_{stem}_{digest}"
    if key in sys.modules:
        return sys.modules[key], sys.modules[key + "_grpc"]
    pb2 = _load(f"{stem}_pb2", pb2_file)
    sys.modules[key] = pb2
    grpc_mod = _load(f"{stem}_pb2_grpc", grpc_file)
    sys.modules[key + "_grpc"] = grpc_mod
    return pb2, grpc_mod


def _load(name: str, path: Path):
    # The generated *_pb2_grpc module does `import <stem>_pb2`, so register the
    # pb2 module under its plain name before loading it.
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
