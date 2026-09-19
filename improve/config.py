from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from improve.ci import CIProvider

DEFAULT_EFFORT = "max"
DEFAULT_CODEX_MODEL = "gpt-6-astra"
EFFORT_TIMEOUTS = {"max": 2700, "medium": 900}


def _default_provider() -> CIProvider:
    from improve.ci_gh import GitHubCI

    return GitHubCI()


@dataclass
class Config:
    """Runtime settings for the iteration loop."""

    agent_timeout: int = EFFORT_TIMEOUTS[DEFAULT_EFFORT]
    ci_timeout: int = 900
    ci_provider: CIProvider = field(default_factory=_default_provider)
    effort: str = DEFAULT_EFFORT
    codex_model: str = DEFAULT_CODEX_MODEL
