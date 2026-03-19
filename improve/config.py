from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from improve.ci import CIProvider


def _default_provider() -> CIProvider:
    from improve.ci_gh import GitHubCI

    return GitHubCI()


@dataclass
class Config:
    """Runtime settings for the iteration loop."""

    claude_timeout: int = 900
    ci_timeout: int = 900
    ci_provider: CIProvider = field(default_factory=_default_provider)
