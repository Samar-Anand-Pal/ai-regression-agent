"""Stage 1: doc ingestion.

Mirrors the docs folder byte for byte (no AI, no network) and writes
manifest.json with an MD5 per file. The manifest is the baseline that
diff-runner compares against on the next run.
"""
from __future__ import annotations

import hashlib
import shutil

from ..workspace import Workspace

DOC_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm"}


class EmptyDocsError(RuntimeError):
    pass


def md5_of(path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def run(ws: Workspace) -> dict:
    src = ws.cfg.docs_path
    files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix.lower() in DOC_SUFFIXES)
    if not files:
        raise EmptyDocsError(f"no docs found in {src} (looked for {', '.join(sorted(DOC_SUFFIXES))})")

    if ws.docs_mirror.exists():
        shutil.rmtree(ws.docs_mirror)
    manifest = {}
    for f in files:
        rel = f.relative_to(src).as_posix()
        dest = ws.docs_mirror / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)
        manifest[rel] = md5_of(dest)
        assert md5_of(f) == manifest[rel], f"mirror of {rel} is not byte-exact"
    ws.write_json("manifest.json", manifest)
    return {"docs": len(manifest)}
