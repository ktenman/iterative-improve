from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_DIR = Path(".improve-loop")
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "run.log"


@dataclass
class PhaseResult:
    iteration: int
    phase: str
    changes_made: bool
    files: list[str]
    summary: str
    ci_passed: bool
    ci_retries: int
    duration_seconds: float = 0.0
    claude_seconds: float = 0.0
    ci_seconds: float = 0.0


@dataclass
class LoopState:
    branch: str
    started_at: str
    iteration: int = 0
    results: list[dict] = field(default_factory=list)

    def add(self, result: PhaseResult) -> None:
        self.results.append(asdict(result))
        self.save()

    def context(self) -> str:
        changed = [r for r in self.results if r["changes_made"]]
        if not changed:
            return "None (first iteration)"
        return "\n".join(f"- [{r['phase']}] {r['summary']}" for r in changed)

    def save(self) -> None:
        STATE_DIR.mkdir(exist_ok=True)
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(asdict(self), indent=2))
        temp.replace(STATE_FILE)

    @staticmethod
    def load() -> LoopState | None:
        if not STATE_FILE.exists():
            return None
        try:
            data = json.loads(STATE_FILE.read_text())
            return LoopState(
                branch=data["branch"],
                started_at=data["started_at"],
                iteration=data.get("iteration", 0),
                results=data.get("results", []),
            )
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            return None
