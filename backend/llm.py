"""The one place the backend talks to Claude.

The rules engine decides; Claude only drafts explanations and suggestions that a person
approves (see domain/recommendations.py). Keeping the client here lets tests swap in a fake
through `client_factory`, so no test ever calls the API.
"""
from __future__ import annotations

import os
from typing import Any, Callable

MODEL = "claude-opus-5-5"
# Re-run a request the model declines on Anthropic's recommended fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(RuntimeError):
    pass


def _default_client() -> Any:
    if not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        raise LLMUnavailable("Claude recommendations aren't enabled on this server yet (ANTHROPIC_API_KEY is not set).")
    import anthropic

    return anthropic.Anthropic(timeout=180.0, max_retries=2)


client_factory: Callable[[], Any] = _default_client
