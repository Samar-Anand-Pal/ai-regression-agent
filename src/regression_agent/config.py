"""pipeline_config.json: everything platform-specific lives here, so the
orchestrator and stages stay platform-agnostic."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LLMConfig:
    provider: str = "auto"          # "claude", "offline", or "auto" (claude if credentials exist)
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 16000


@dataclass
class TargetConfig:
    address: str | None = None      # an already running server, e.g. "127.0.0.1:50051"
    launcher: str | None = None     # "module:function" that starts one in-process
    launcher_args: dict = field(default_factory=dict)
    reset_rpc: str | None = None    # "Service.Rpc" called before every test, if any


@dataclass
class PipelineConfig:
    platform_name: str
    docs_path: Path
    proto_path: Path
    client_path: Path | None
    output_dir: Path
    llm: LLMConfig = field(default_factory=LLMConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    gates: list[str] = field(default_factory=lambda: ["skill_review", "test_spec_review", "code_env_review"])
    max_skills_per_test: int = 8
    config_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        path = Path(path).resolve()
        raw = json.loads(path.read_text())
        base = path.parent

        def p(key, required=True):
            v = raw.get(key)
            if v is None:
                if required:
                    raise ValueError(f"{path.name}: '{key}' is required")
                return None
            return (base / v).resolve()

        missing = [k for k in ("platform_name", "docs_path", "proto_path") if k not in raw]
        if missing:
            raise ValueError(f"{path.name}: missing {', '.join(missing)}")
        return cls(
            platform_name=raw["platform_name"],
            docs_path=p("docs_path"),
            proto_path=p("proto_path"),
            client_path=p("client_path", required=False),
            output_dir=p("output_dir", required=False) or (base / "output").resolve(),
            llm=LLMConfig(**raw.get("llm", {})),
            target=TargetConfig(**raw.get("target", {})),
            gates=raw.get("gates", ["skill_review", "test_spec_review", "code_env_review"]),
            max_skills_per_test=raw.get("max_skills_per_test", 8),
            config_path=path,
        )
