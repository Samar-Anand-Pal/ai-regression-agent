"""The only place that talks to Claude.

Every LLM step asks for JSON that matches a schema (structured outputs), so a
stage never has to parse free text. Calls are logged with token usage to
llm_calls.jsonl in the workspace.

When no Claude credentials are available the pipeline runs in offline mode:
each LLM step falls back to its deterministic heuristic (lower quality, but
the whole pipeline still runs, which is what CI uses).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import LLMConfig

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


def claude_credentials_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()  # `ant auth login` profile


class ClaudeLLM:
    name = "claude"

    def __init__(self, cfg: LLMConfig, log_path: Path | None = None, client=None):
        self.cfg = cfg
        self.log_path = log_path
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client

    def structured(self, *, task: str, system: str, prompt: str, schema: dict,
                   effort: str | None = None) -> dict:
        """One request; returns the parsed JSON object that matches `schema`."""
        import anthropic

        request = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            # The system prompt is identical across calls of the same stage: cache it.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": effort or self.cfg.effort,
                           "format": {"type": "json_schema", "schema": schema}},
        )
        started = time.time()
        try:
            # Server-side fallback: if the model declines, the API retries on a
            # fallback model inside the same call.
            resp = self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **request)
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            resp = self.client.messages.create(**request)

        if resp.stop_reason == "refusal":
            raise LLMError(f"{task}: the model declined the request")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"{task}: output hit max_tokens ({self.cfg.max_tokens}); raise llm.max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"{task}: response had no text block")
        data = json.loads(text)
        self._log(task, resp, time.time() - started)
        return data

    def _log(self, task, resp, seconds):
        if not self.log_path:
            return
        u = getattr(resp, "usage", None)
        row = {
            "task": task, "model": getattr(resp, "model", self.cfg.model), "seconds": round(seconds, 2),
            "input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None),
            "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", None),
        }
        with open(self.log_path, "a") as f:
            f.write(json.dumps(row) + "\n")


def get_llm(cfg: LLMConfig, log_path: Path | None = None):
    """Returns a ClaudeLLM, or None for offline mode."""
    provider = cfg.provider
    if provider == "offline":
        return None
    if provider == "auto" and not claude_credentials_available():
        return None
    return ClaudeLLM(cfg, log_path)
