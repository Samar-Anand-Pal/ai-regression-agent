"""Where every artifact lives. Stages only talk to the disk through this class,
so each stage's output can be read, reviewed and edited by a human."""
from __future__ import annotations

import json
from pathlib import Path

from .config import PipelineConfig


class Workspace:
    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self.root = cfg.output_dir
        for d in (self.root, self.docs_mirror, self.tests_dir, self.logs_dir, self.bugs_dir, self.state_dir):
            d.mkdir(parents=True, exist_ok=True)

    # folders
    @property
    def docs_mirror(self) -> Path: return self.root / "docs"
    @property
    def tests_dir(self) -> Path: return self.root / "tests" / "generated"
    @property
    def logs_dir(self) -> Path: return self.root / "test_logs"
    @property
    def bugs_dir(self) -> Path: return self.root / "bug_reports"
    @property
    def state_dir(self) -> Path: return self.root / ".state"

    def path(self, name: str) -> Path:
        return self.root / name

    def read_json(self, name: str, default=None):
        p = self.path(name)
        if not p.exists():
            if default is not None:
                return default
            raise FileNotFoundError(f"{p} not found; has the stage that writes it run?")
        return json.loads(p.read_text())

    def write_json(self, name: str, data) -> Path:
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n")
        return p

    def write_text(self, name: str, text: str) -> Path:
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p
