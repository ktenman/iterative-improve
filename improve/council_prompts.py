from __future__ import annotations

from improve.findings import OTHER_AGENT, Finding, text_of
from improve.phases import PHASE_PROMPTS

NAMES = {"claude": "Claude", "codex": "Codex"}
READ_ONLY = "Do not edit any files. Answer with JSON that matches the schema."
POSITION_RULES = (
    '- decision: "fix" or "skip". Skip false positives, issues this branch did not introduce, '
    "and changes that are not worth making.\n"
    '- approach: for fix, exactly what to change and where; for skip, "-".\n'
    "- reason: one or two sentences grounded in the code.\n"
)
FIX_RULES = (
    "Rules:\n"
    "- Implement exactly these items and nothing else.\n"
    "- Make changes directly to the files; do not use the Agent tool.\n"
    "- Run the project's full test suite and its lint and format checks, and fix anything "
    "you broke.\n"
    '- End with one line starting with "SUMMARY:" that says what you changed.'
)


def describe(finding: Finding) -> str:
    sources = finding.sources
    reporter = "both reviewers" if len(sources) > 1 else NAMES[sources[0]]
    lines = [
        f"{finding.id} · {finding.phase} · {finding.file}:{finding.line} "
        f"({finding.symbol or 'module level'}) · severity {finding.severity} · "
        f"confidence {finding.confidence} · reported by {reporter}"
    ]
    for report in finding.reports:
        lines.append(f"  [{NAMES[report.agent]}] {report.title}")
        if report.detail:
            lines.append(f"    {report.detail}")
        if report.suggested_fix:
            lines.append(f"    Suggested fix: {report.suggested_fix}")
    return "\n".join(lines)


def format_ledger(ledger: list[dict]) -> str:
    if not ledger:
        return "None yet"
    return "\n".join(
        f"- {entry['file']}:{entry['symbol'] or entry['line']} · {entry['outcome']} · "
        f"{entry['title']} ({entry['reason']})"
        for entry in ledger
    )


def _position_text(position: dict) -> str:
    return (
        f"decision: {text_of(position, 'decision')}\n"
        f"    approach: {text_of(position, 'approach')}\n"
        f"    reason: {text_of(position, 'reason')}"
    )


def build_review_prompt(phases: list[str], changed: list[str], ledger: list[dict]) -> str:
    focus = "\n\n".join(f"### {phase}\n{PHASE_PROMPTS[phase]}" for phase in phases)
    files = "\n".join(changed)
    settled = [entry for entry in ledger if entry["file"] in changed]
    return (
        "You are one of two independent reviewers of a feature branch. Another model reviews "
        "the same code at the same time, and afterwards you will discuss the findings with it."
        "\n\n"
        f"Files changed on this branch (vs main):\n{files}\n\n"
        "Settled in earlier iterations. Never report a skipped or disputed one again; report a "
        "fixed one only if the fix is wrong or caused a new problem:\n"
        f"{format_ledger(settled)}\n\n"
        f"Review focus areas:\n\n{focus}\n\n"
        "Rules for this step:\n"
        "- Report findings only. Do NOT edit, create or delete files, even where a focus area "
        "says to fix things.\n"
        "- Only report issues in code this branch added or changed.\n"
        "- Give every finding an honest confidence from 0 to 100, and include anything at 50 "
        "or above.\n"
        f"- phase is the focus area the finding belongs to: {', '.join(phases)}.\n"
        "- file is the repo-relative path. symbol is the enclosing function or class, like "
        '"run_job" or "Job.__init__" (empty for module-level code). line is the line number '
        "inside that file, as `cat -n <file>` shows it, not a position in a diff.\n"
        "- If nothing is worth reporting, return an empty list.\n"
        "Answer with JSON that matches the schema."
    )


def build_round_one_prompt(findings: list[Finding], agent: str) -> str:
    other = NAMES[OTHER_AGENT[agent]]
    described = "\n\n".join(describe(finding) for finding in findings)
    return (
        f"You and {other} reviewed this branch independently. These are the merged findings "
        "(each says who reported it):\n\n"
        f"{described}\n\n"
        f"Round 1 of 2. {other} answers the same question at the same time. "
        "For every finding id, give your position:\n"
        f"{POSITION_RULES}\n"
        "In round 2 you will both see each other's positions and pick which version to "
        f"implement. Answer every id. {READ_ONLY}"
    )


def build_round_two_prompt(
    findings: list[Finding], mine: dict[str, dict], theirs: dict[str, dict], agent: str
) -> str:
    other = NAMES[OTHER_AGENT[agent]]
    blocks = "\n\n".join(
        f"{describe(finding)}\n"
        f"  Your round 1 position:\n    {_position_text(mine[finding.id])}\n"
        f"  {other}'s round 1 position:\n    {_position_text(theirs[finding.id])}"
        for finding in findings
    )
    return (
        f"Round 2 of 2, the final round. For each item you see your position and {other}'s. "
        "Pick the version that should be implemented:\n"
        f'- "mine" keeps your position; "theirs" adopts {other}\'s.\n'
        '- If you both pick "mine": when you both chose to fix, Claude\'s approach is used; '
        "otherwise the item stays unresolved and is not fixed.\n"
        '- If you both pick "theirs", Claude\'s version is used.\n\n'
        f"{blocks}\n\n"
        f"Answer every id with a pick and a one-sentence reason. {READ_ONLY}"
    )


def build_fix_prompt(fixes: list[tuple[Finding, str]]) -> str:
    items = "\n\n".join(
        f"{describe(finding)}\n  Agreed approach: {approach}" for finding, approach in fixes
    )
    return (
        "You and Codex reviewed this branch and agreed on the plan below. Implement it now.\n\n"
        f"{items}\n\n{FIX_RULES}"
    )
