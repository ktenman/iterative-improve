from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from improve import color
from improve.process import format_duration

logger = logging.getLogger("improve")

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
    reverted: bool = False

    @classmethod
    def no_changes(
        cls, iteration: int, phase: str, duration: float = 0.0, claude_seconds: float = 0.0
    ) -> PhaseResult:
        return cls(
            iteration=iteration,
            phase=phase,
            changes_made=False,
            files=[],
            summary="No changes needed",
            ci_passed=True,
            ci_retries=0,
            duration_seconds=duration,
            claude_seconds=claude_seconds,
        )

    @classmethod
    def crashed(cls, iteration: int, phase: str) -> PhaseResult:
        return cls(
            iteration=iteration,
            phase=phase,
            changes_made=False,
            files=[],
            summary="Phase crashed",
            ci_passed=True,
            ci_retries=0,
        )


@dataclass
class LoopState:
    branch: str
    started_at: str
    iteration: int = 0
    results: list[dict] = field(default_factory=list)

    def add(self, result: PhaseResult) -> None:
        self.results.append(asdict(result))
        self.save()

    def kept_results(self) -> list[dict]:
        return [r for r in self.results if r["changes_made"] and not r.get("reverted")]

    def context(self) -> str:
        changed = self.kept_results()
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
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning("state] Failed to load %s: %s", STATE_FILE, exc)
            return None


def _ci_label(r: dict) -> str:
    if r.get("reverted"):
        return color.wrap("REVT", color.DARK_YELLOW)
    return color.wrap("PASS", color.DARK_GREEN) if r["ci_passed"] else color.wrap("FAIL", color.RED)


def format_summary(results: list[dict], total_elapsed: float) -> str:
    total_claude = sum(r.get("claude_seconds", 0) for r in results)
    total_ci = sum(r.get("ci_seconds", 0) for r in results)
    overhead = format_duration(max(0, total_elapsed - total_claude - total_ci))
    banner = color.wrap("=" * 60, color.BOLD + color.CYAN)
    lines = [
        f"\n{banner}",
        color.wrap("RESULTS", color.BOLD + color.CYAN),
        banner,
        f"  Phases run:     {len(results)}",
        f"  With changes:   {sum(1 for r in results if r['changes_made'])}",
        f"  CI fixes:       {sum(r['ci_retries'] for r in results)}",
        f"  Reverted:       {sum(1 for r in results if r.get('reverted'))}",
        f"  Total time:     {color.wrap(format_duration(total_elapsed), color.DIM)}",
        f"  Claude time:    {color.wrap(format_duration(total_claude), color.DIM)}",
        f"  CI time:        {color.wrap(format_duration(total_ci), color.DIM)}",
        f"  Overhead:       {color.wrap(overhead, color.DIM)}",
        "",
    ]
    for r in results:
        marker_char = "+" if r["changes_made"] else " "
        if marker_char == "+":
            marker = color.wrap(f"[{marker_char}]", color.BOLD + color.DARK_GREEN)
        else:
            marker = f"[{marker_char}]"
        phase_name = color.wrap(r["phase"], color.phase_color(r["phase"]))
        dur = color.wrap(format_duration(r.get("duration_seconds", 0)), color.DIM)
        ci_label = _ci_label(r)
        lines.append(f"  {marker} {phase_name:>21s} | CI:{ci_label} | {dur:>20s} | {r['summary']}")
    lines.extend([f"\n  State: {STATE_FILE}", f"  Log:   {LOG_FILE}"])
    return "\n".join(lines)
