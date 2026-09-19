from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

AGENTS = ("claude", "codex")
OTHER_AGENT = {"claude": "codex", "codex": "claude"}
SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
KEEP_CONFIDENCE = 50
LINE_WINDOW = 3
SUPPRESSED_OUTCOMES = ("skipped", "disputed")
STRING = {"type": "string"}


def _object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _list_schema(key: str, item: dict) -> dict:
    return _object_schema({key: {"type": "array", "items": _object_schema(item)}})


POSITIONS_SCHEMA = _list_schema(
    "positions",
    {
        "id": STRING,
        "decision": {"type": "string", "enum": ["fix", "skip"]},
        "approach": STRING,
        "reason": STRING,
    },
)
PICKS_SCHEMA = _list_schema(
    "picks",
    {"id": STRING, "pick": {"type": "string", "enum": ["mine", "theirs"]}, "reason": STRING},
)


def findings_schema(phases: list[str]) -> dict:
    return _list_schema(
        "findings",
        {
            "phase": {"type": "string", "enum": list(phases)},
            "file": STRING,
            "symbol": STRING,
            "line": {"type": "integer"},
            "severity": {"type": "string", "enum": list(SEVERITY_RANK)},
            "confidence": {"type": "integer"},
            "title": STRING,
            "detail": STRING,
            "suggested_fix": STRING,
        },
    )


class Place(NamedTuple):
    file: str
    symbol: str
    line: int


@dataclass(frozen=True)
class Report:
    """One reviewer's description of a finding."""

    agent: str
    title: str
    detail: str
    suggested_fix: str


@dataclass
class Finding:
    """A review finding at one place in the code, reported by one or both reviewers."""

    phase: str
    file: str
    symbol: str
    line: int
    severity: str
    confidence: int
    reports: list[Report]
    id: str = ""

    @property
    def place(self) -> Place:
        return Place(self.file, self.symbol, self.line)

    @property
    def sources(self) -> list[str]:
        return [report.agent for report in self.reports]

    @property
    def title(self) -> str:
        return self.reports[0].title


class _SymbolKey(NamedTuple):
    """A symbol split into its enclosing qualifier and its name; an empty qualifier matches any."""

    qualifier: str
    name: str


def _symbol_key(symbol: str) -> _SymbolKey:
    parts = [part for part in symbol.strip().removesuffix("()").split(".") if part]
    if not parts:
        return _SymbolKey("", "")
    name = parts[-1]
    return _SymbolKey(parts[-2] if len(parts) > 1 else "", name)


def same_place(a: Place, b: Place) -> bool:
    if a.file != b.file:
        return False
    (qual_a, name_a), (qual_b, name_b) = _symbol_key(a.symbol), _symbol_key(b.symbol)
    if name_a and name_b:
        return name_a == name_b and (not qual_a or not qual_b or qual_a == qual_b)
    if not a.line or not b.line:
        return False
    return abs(a.line - b.line) <= LINE_WINDOW


def normalize_path(raw: str, root: str) -> str:
    path = Path(raw.strip().removeprefix("./"))
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _to_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def text_of(row: dict, key: str) -> str:
    return str(row.get(key) or "")


def reply_rows(reply: dict, key: str) -> list[dict]:
    rows = reply.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _parse(agent: str, raw: dict, root: str) -> Finding:
    severity = text_of(raw, "severity")
    report = Report(
        agent=agent,
        title=text_of(raw, "title"),
        detail=text_of(raw, "detail"),
        suggested_fix=text_of(raw, "suggested_fix"),
    )
    return Finding(
        phase=text_of(raw, "phase") or "review",
        file=normalize_path(text_of(raw, "file"), root),
        symbol=text_of(raw, "symbol").strip(),
        line=_to_int(raw.get("line")),
        severity=severity if severity in SEVERITY_RANK else "low",
        confidence=_to_int(raw.get("confidence")),
        reports=[report],
    )


def _absorb(merged: list[Finding], finding: Finding) -> None:
    agent = finding.reports[0].agent
    match = next(
        (m for m in merged if agent not in m.sources and same_place(m.place, finding.place)),
        None,
    )
    if match is None:
        merged.append(finding)
        return
    match.reports.extend(finding.reports)
    if SEVERITY_RANK[finding.severity] > SEVERITY_RANK[match.severity]:
        match.severity = finding.severity
    match.confidence = max(match.confidence, finding.confidence)


def _worth_debating(finding: Finding) -> bool:
    return (
        len(finding.reports) > 1
        or finding.severity in ("critical", "high")
        or finding.confidence >= KEEP_CONFIDENCE
    )


def _settled(finding: Finding, ledger: list[dict]) -> bool:
    return any(
        entry.get("outcome") in SUPPRESSED_OUTCOMES
        and entry.get("phase") == finding.phase
        and SEVERITY_RANK.get(text_of(entry, "severity"), 0) >= SEVERITY_RANK[finding.severity]
        and same_place(finding.place, Place(entry["file"], entry["symbol"], entry["line"]))
        for entry in ledger
    )


def merge_findings(
    reports: dict[str, list[dict]], changed: list[str], ledger: list[dict], root: str
) -> list[Finding]:
    merged: list[Finding] = []
    for agent, raw_findings in reports.items():
        for raw in raw_findings:
            if not isinstance(raw, dict):
                continue
            finding = _parse(agent, raw, root)
            if finding.file in changed:
                _absorb(merged, finding)
    kept = [f for f in merged if _worth_debating(f) and not _settled(f, ledger)]
    kept.sort(key=lambda f: (f.file, f.line))
    for number, finding in enumerate(kept, 1):
        finding.id = f"F{number}"
    return kept
