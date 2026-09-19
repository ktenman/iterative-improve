from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from improve.council_prompts import build_round_one_prompt, build_round_two_prompt
from improve.findings import (
    AGENTS,
    OTHER_AGENT,
    PICKS_SCHEMA,
    POSITIONS_SCHEMA,
    Finding,
    reply_rows,
    text_of,
)
from improve.process import abort_on_first_failure

logger = logging.getLogger("improve")

WINNERS = {
    ("mine", "theirs"): "claude",
    ("theirs", "mine"): "codex",
    ("theirs", "theirs"): "claude",
}
VALID_PICKS = ("mine", "theirs")


@dataclass
class Agreement:
    """How the two reviewers settled each finding: fix it, skip it, or leave it disputed."""

    fixes: list[tuple[Finding, str]] = field(default_factory=list)
    skipped: list[tuple[Finding, str]] = field(default_factory=list)
    disputed: list[Finding] = field(default_factory=list)
    unanswered: list[Finding] = field(default_factory=list)


def settle_round_one(
    findings: list[Finding], claude: dict[str, dict], codex: dict[str, dict]
) -> tuple[Agreement, list[Finding]]:
    agreement = Agreement()
    still_open: list[Finding] = []
    for finding in findings:
        mine, theirs = claude.get(finding.id), codex.get(finding.id)
        if mine is None or theirs is None:
            agreement.unanswered.append(finding)
        elif mine.get("decision") == theirs.get("decision") == "skip":
            agreement.skipped.append((finding, text_of(mine, "reason")))
        else:
            still_open.append(finding)
    return agreement, still_open


def pick_winner(claude_pick: str | None, codex_pick: str | None) -> str | None:
    return WINNERS.get((claude_pick, codex_pick))


def _by_id(reply: dict, key: str) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for row in reply_rows(reply, key):
        if not row.get("id"):
            continue
        row_id = str(row["id"]).strip()
        if row_id in by_id:
            logger.warning("council] Ignoring a repeated answer for %s", row_id)
            continue
        by_id[row_id] = row
    return by_id


def ask_both(
    ask: Callable[[str, str, dict], dict], prompts: dict[str, str], schema: dict
) -> dict[str, dict]:
    with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
        futures = {
            agent: pool.submit(ask, agent, prompt, schema) for agent, prompt in prompts.items()
        }
        abort_on_first_failure(futures.values())
        return {agent: future.result() for agent, future in futures.items()}


def _answers_by_id(
    ask: Callable[[str, str, dict], dict], prompts: dict[str, str], schema: dict, key: str
) -> dict[str, dict[str, dict]]:
    replies = ask_both(ask, prompts, schema)
    return {agent: _by_id(reply, key) for agent, reply in replies.items()}


def _pick_of(picks: dict[str, dict], finding_id: str) -> str:
    pick = text_of(picks.get(finding_id, {}), "pick")
    return pick if pick in VALID_PICKS else ""


def _stalemate_winner(positions: dict[str, dict[str, dict]], finding_id: str) -> str | None:
    both_want_a_fix = all(positions[agent][finding_id].get("decision") == "fix" for agent in AGENTS)
    return "claude" if both_want_a_fix else None


def _settle_pick(
    agreement: Agreement,
    finding: Finding,
    picks: dict[str, dict[str, dict]],
    positions: dict[str, dict[str, dict]],
) -> None:
    claude_pick = _pick_of(picks["claude"], finding.id)
    codex_pick = _pick_of(picks["codex"], finding.id)
    if not claude_pick or not codex_pick:
        agreement.unanswered.append(finding)
        return
    winner = pick_winner(claude_pick, codex_pick) or _stalemate_winner(positions, finding.id)
    if winner is None:
        agreement.disputed.append(finding)
        return
    position = positions[winner][finding.id]
    decision = position.get("decision")
    if decision == "fix":
        agreement.fixes.append((finding, text_of(position, "approach")))
    elif decision == "skip":
        agreement.skipped.append((finding, text_of(position, "reason")))
    else:
        agreement.disputed.append(finding)


def _round_two(
    agreement: Agreement,
    findings: list[Finding],
    positions: dict[str, dict[str, dict]],
    ask: Callable[[str, str, dict], dict],
) -> None:
    prompts = {
        agent: build_round_two_prompt(
            findings, positions[agent], positions[OTHER_AGENT[agent]], agent
        )
        for agent in AGENTS
    }
    picks = _answers_by_id(ask, prompts, PICKS_SCHEMA, "picks")
    for finding in findings:
        _settle_pick(agreement, finding, picks, positions)


def run_rounds(findings: list[Finding], ask: Callable[[str, str, dict], dict]) -> Agreement:
    prompts = {agent: build_round_one_prompt(findings, agent) for agent in AGENTS}
    positions = _answers_by_id(ask, prompts, POSITIONS_SCHEMA, "positions")
    agreement, still_open = settle_round_one(findings, positions["claude"], positions["codex"])
    if still_open:
        _round_two(agreement, still_open, positions, ask)
    logger.info(
        "council] Agreed: %d to fix, %d to skip, %d disputed, %d unanswered",
        len(agreement.fixes),
        len(agreement.skipped),
        len(agreement.disputed),
        len(agreement.unanswered),
    )
    return agreement
