from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import NamedTuple

from improve import color
from improve.process import format_duration

logger = logging.getLogger("improve")

STATE_DIR = Path(".improve-loop")
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "run.log"
CRASHED_SUMMARY = "Phase crashed"


class CIFixResult(NamedTuple):
    passed: bool
    retries: int
    claude_time: float
    ci_time: float


@dataclass
class PhaseResult:
    """Result of a single phase execution within an iteration."""

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
    codex_seconds: float = 0.0

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
            summary=CRASHED_SUMMARY,
            ci_passed=True,
            ci_retries=0,
        )

    @property
    def is_crashed(self) -> bool:
        return self.summary == CRASHED_SUMMARY


@dataclass(frozen=True)
class LedgerEntry:
    """A council finding an iteration settled: fixed, skipped or disputed."""

    iteration: int
    outcome: str
    phase: str
    severity: str
    file: str
    symbol: str
    line: int
    title: str
    reason: str


@dataclass
class LoopState:
    """Persistent state for the iteration loop, saved to .improve-loop/state.json."""

    branch: str
    started_at: str
    iteration: int = 0
    results: list[dict] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)

    def add(self, result: PhaseResult) -> None:
        self.results.append(asdict(result))
        self.save()

    def kept_results(self) -> list[dict]:
        return [r for r in self.results if r["changes_made"]]

    def settle(self, entries: list[LedgerEntry]) -> None:
        self.ledger.extend(asdict(entry) for entry in entries)
        self.save()

    def crashed_last(self, phase: str) -> bool:
        if not self.results:
            return False
        last = self.results[-1]
        return last["phase"] == phase and last["summary"] == CRASHED_SUMMARY

    def unchanged_last(self, phase: str) -> bool:
        finished = [r for r in self.results if r["summary"] != CRASHED_SUMMARY]
        if not finished:
            return False
        last = finished[-1]
        return last["phase"] == phase and not last["changes_made"]

    def context(self) -> str:
        changed = self.kept_results()
        if not changed:
            return "None (first iteration)"
        return "\n".join(f"- [{r['phase']}] {r['summary']}" for r in changed)

    def save(self) -> None:
        try:
            STATE_DIR.mkdir(exist_ok=True)
            temp = STATE_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps(asdict(self), indent=2))
            temp.replace(STATE_FILE)
        except OSError as exc:
            logger.warning("state] Failed to save %s: %s", STATE_FILE, exc)

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
                ledger=data.get("ledger", []),
            )
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning("state] Failed to load %s: %s", STATE_FILE, exc)
            return None


def _ci_label(r: dict) -> str:
    if r["ci_passed"]:
        return color.wrap("PASS", color.DARK_GREEN)
    return color.wrap("FAIL", color.RED)


def _disputed_lines(ledger: Sequence[dict]) -> list[str]:
    disputed = [entry for entry in ledger if entry.get("outcome") == "disputed"]
    if not disputed:
        return []
    heading = color.wrap("Disputed (left for you to decide):", color.DARK_YELLOW)
    return [
        "",
        f"  {heading}",
        *(f"    - {e['file']}:{e['symbol'] or e['line']}  {e['title']}" for e in disputed),
    ]


def format_summary(results: list[dict], total_elapsed: float, ledger: Sequence[dict] = ()) -> str:
    total_claude = sum(r.get("claude_seconds", 0) for r in results)
    total_codex = sum(r.get("codex_seconds", 0) for r in results)
    total_ci = sum(r.get("ci_seconds", 0) for r in results)
    overhead = format_duration(max(0, total_elapsed - total_claude - total_ci))
    codex_time = color.wrap(format_duration(total_codex), color.DIM)
    banner = color.separator()
    lines = [
        f"\n{banner}",
        color.section_title("Results"),
        banner,
        f"  Phases run:     {len(results)}",
        f"  With changes:   {sum(1 for r in results if r['changes_made'])}",
        f"  CI fixes:       {sum(r['ci_retries'] for r in results)}",
        f"  Total time:     {color.wrap(format_duration(total_elapsed), color.DIM)}",
        f"  Claude time:    {color.wrap(format_duration(total_claude), color.DIM)}",
        *([f"  Codex time:     {codex_time}"] if total_codex else []),
        f"  CI time:        {color.wrap(format_duration(total_ci), color.DIM)}",
        f"  Overhead:       {color.wrap(overhead, color.DIM)}",
        "",
    ]
    for r in results:
        mark = color.status_mark(r["ci_passed"], r["changes_made"])
        phase_name = color.wrap(f"{r['phase']:10s}", color.phase_color(r["phase"]))
        dur = color.wrap(f"{format_duration(r.get('duration_seconds', 0)):>9s}", color.DIM)
        ci_label = _ci_label(r)
        lines.append(f"  {mark} {phase_name}  {ci_label}  {dur}  {r['summary']}")
    lines.extend(_disputed_lines(ledger))
    lines.extend([f"\n  State: {STATE_FILE}", f"  Log:   {LOG_FILE}"])
    return "\n".join(lines)
