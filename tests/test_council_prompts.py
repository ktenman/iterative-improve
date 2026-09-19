from improve.council_prompts import (
    NAMES,
    build_fix_prompt,
    build_review_prompt,
    build_round_one_prompt,
    build_round_two_prompt,
    describe,
    format_ledger,
)
from improve.findings import Finding, Report
from improve.phases import PHASE_PROMPTS

LEDGER_ENTRY = {
    "iteration": 1,
    "outcome": "skipped",
    "phase": "review",
    "file": "app.py",
    "symbol": "load",
    "line": 12,
    "title": "Unchecked None",
    "reason": "Callers never pass None",
}
CLAUDE_REPORT = Report("claude", "Crash on empty input", "It indexes [0]", "Return early")


def _finding(finding_id="F1", reports=None, symbol="run"):
    return Finding(
        "review", "app.py", symbol, 7, "high", 80, reports or [CLAUDE_REPORT], finding_id
    )


def _position(decision, approach, reason):
    return {"id": "F1", "decision": decision, "approach": approach, "reason": reason}


REVIEW_RULES = (
    "Rules for this step:\n"
    "- Report findings only. Do NOT edit, create or delete files, even where a focus area "
    "says to fix things.\n"
    "- Only report issues in code this branch added or changed.\n"
    "- Give every finding an honest confidence from 0 to 100, and include anything at 50 "
    "or above.\n"
    "- phase is the focus area the finding belongs to: review, security.\n"
    "- file is the repo-relative path. symbol is the enclosing function or class, like "
    '"run_job" or "Job.__init__" (empty for module-level code). line is the line number '
    "inside that file, as `cat -n <file>` shows it, not a position in a diff.\n"
    "- If nothing is worth reporting, return an empty list.\n"
    "Answer with JSON that matches the schema."
)
POSITION_RULES = (
    '- decision: "fix" or "skip". Skip false positives, issues this branch did not introduce, '
    "and changes that are not worth making.\n"
    '- approach: for fix, exactly what to change and where; for skip, "-".\n'
    "- reason: one or two sentences grounded in the code.\n"
)
DESCRIBED = (
    "F1 · review · app.py:7 (run) · severity high · confidence 80 · reported by Claude\n"
    "  [Claude] Crash on empty input\n"
    "    It indexes [0]\n"
    "    Suggested fix: Return early"
)


class TestBuildReviewPrompt:
    def test_builds_the_full_review_prompt(self):
        prompt = build_review_prompt(
            ["review", "security"], ["app.py", "lib/util.py"], [LEDGER_ENTRY]
        )

        assert prompt == (
            "You are one of two independent reviewers of a feature branch. Another model "
            "reviews the same code at the same time, and afterwards you will discuss the "
            "findings with it.\n\n"
            "Files changed on this branch (vs main):\napp.py\nlib/util.py\n\n"
            "Settled in earlier iterations. Never report a skipped or disputed one again; "
            "report a fixed one only if the fix is wrong or caused a new problem:\n"
            "- app.py:load · skipped · Unchecked None (Callers never pass None)\n\n"
            "Review focus areas:\n\n"
            f"### review\n{PHASE_PROMPTS['review']}\n\n"
            f"### security\n{PHASE_PROMPTS['security']}\n\n"
            f"{REVIEW_RULES}"
        )

    def test_includes_only_the_focus_areas_of_the_active_phases(self):
        prompt = build_review_prompt(["security"], ["app.py"], [])

        assert PHASE_PROMPTS["simplify"] not in prompt
        assert "belongs to: security.\n" in prompt

    def test_leaves_out_ledger_entries_for_files_the_branch_no_longer_changes(self):
        stale = {**LEDGER_ENTRY, "file": "gone.py", "title": "Stale entry"}

        prompt = build_review_prompt(["review"], ["app.py"], [LEDGER_ENTRY, stale])

        assert "Unchecked None" in prompt
        assert "Stale entry" not in prompt

    def test_says_nothing_is_settled_on_an_empty_ledger(self):
        prompt = build_review_prompt(["review"], ["app.py"], [])

        assert "caused a new problem:\nNone yet\n\n" in prompt


class TestFormatLedger:
    def test_lists_each_entry_on_its_own_line(self):
        second = {**LEDGER_ENTRY, "symbol": "save", "outcome": "fixed", "reason": "Guarded"}

        assert format_ledger([LEDGER_ENTRY, second]) == (
            "- app.py:load · skipped · Unchecked None (Callers never pass None)\n"
            "- app.py:save · fixed · Unchecked None (Guarded)"
        )

    def test_uses_the_line_when_the_entry_has_no_symbol(self):
        entry = {**LEDGER_ENTRY, "symbol": "", "outcome": "disputed"}

        assert format_ledger([entry]) == (
            "- app.py:12 · disputed · Unchecked None (Callers never pass None)"
        )


class TestDescribe:
    def test_describes_a_finding_with_its_reports(self):
        assert describe(_finding()) == DESCRIBED

    def test_says_both_reviewers_when_two_reported_it(self):
        reports = [Report("claude", "A", "", ""), Report("codex", "B", "", "")]

        assert describe(_finding(reports=reports)) == (
            "F1 · review · app.py:7 (run) · severity high · confidence 80 · "
            "reported by both reviewers\n"
            "  [Claude] A\n"
            "  [Codex] B"
        )

    def test_names_codex_when_it_reported_alone(self):
        text = describe(_finding(reports=[Report("codex", "A", "", "")]))

        assert text.endswith("reported by Codex\n  [Codex] A")

    def test_says_module_level_when_there_is_no_symbol(self):
        assert "app.py:7 (module level) ·" in describe(_finding(symbol=""))

    def test_names_both_agents(self):
        assert NAMES == {"claude": "Claude", "codex": "Codex"}


class TestRoundPrompts:
    def test_builds_the_full_round_one_prompt_for_claude(self):
        prompt = build_round_one_prompt([_finding()], "claude")

        assert prompt == (
            "You and Codex reviewed this branch independently. These are the merged findings "
            "(each says who reported it):\n\n"
            f"{DESCRIBED}\n\n"
            "Round 1 of 2. Codex answers the same question at the same time. "
            "For every finding id, give your position:\n"
            f"{POSITION_RULES}\n"
            "In round 2 you will both see each other's positions and pick which version to "
            "implement. Answer every id. "
            "Do not edit any files. Answer with JSON that matches the schema."
        )

    def test_round_one_tells_codex_that_claude_is_the_other_reviewer(self):
        prompt = build_round_one_prompt([_finding()], "codex")

        assert prompt.startswith("You and Claude reviewed")
        assert "Round 1 of 2. Claude answers" in prompt

    def test_round_one_separates_findings_with_a_blank_line(self):
        prompt = build_round_one_prompt([_finding("F1"), _finding("F2")], "claude")

        assert "Suggested fix: Return early\n\nF2 · review" in prompt

    def test_builds_the_full_round_two_prompt_for_codex(self):
        mine = {"F1": _position("fix", "Return early", "Crashes")}
        theirs = {"F1": _position("skip", "-", "Never empty")}

        prompt = build_round_two_prompt([_finding()], mine, theirs, "codex")

        assert prompt == (
            "Round 2 of 2, the final round. For each item you see your position and "
            "Claude's. Pick the version that should be implemented:\n"
            '- "mine" keeps your position; "theirs" adopts Claude\'s.\n'
            '- If you both pick "mine": when you both chose to fix, Claude\'s approach is '
            "used; otherwise the item stays unresolved and is not fixed.\n"
            '- If you both pick "theirs", Claude\'s version is used.\n\n'
            f"{DESCRIBED}\n"
            "  Your round 1 position:\n"
            "    decision: fix\n"
            "    approach: Return early\n"
            "    reason: Crashes\n"
            "  Claude's round 1 position:\n"
            "    decision: skip\n"
            "    approach: -\n"
            "    reason: Never empty\n\n"
            "Answer every id with a pick and a one-sentence reason. "
            "Do not edit any files. Answer with JSON that matches the schema."
        )

    def test_round_two_separates_findings_with_a_blank_line(self):
        positions = {
            "F1": _position("fix", "A", "B"),
            "F2": _position("fix", "C", "D"),
        }

        prompt = build_round_two_prompt(
            [_finding("F1"), _finding("F2")], positions, positions, "claude"
        )

        assert "    reason: B\n\nF2 · review" in prompt
        assert "  Codex's round 1 position:" in prompt

    def test_round_two_shows_blanks_for_missing_position_fields(self):
        mine = {"F1": {"id": "F1"}}

        prompt = build_round_two_prompt([_finding()], mine, mine, "claude")

        assert "    decision: \n    approach: \n    reason: \n" in prompt


class TestBuildFixPrompt:
    def test_carries_both_reviewers_descriptions_of_one_finding(self):
        codex_report = Report("codex", "Leaks the file handle", "It never closes", "Use with")
        finding = _finding(reports=[CLAUDE_REPORT, codex_report])

        prompt = build_fix_prompt([(finding, "Return early and use with")])

        assert "[Claude] Crash on empty input" in prompt
        assert "[Codex] Leaks the file handle" in prompt
        assert "    Suggested fix: Use with" in prompt

    def test_builds_the_full_fix_prompt(self):
        fixes = [(_finding("F1"), "Return early"), (_finding("F2"), "Guard the index")]

        assert build_fix_prompt(fixes) == (
            "You and Codex reviewed this branch and agreed on the plan below. "
            "Implement it now.\n\n"
            f"{describe(_finding('F1'))}\n"
            "  Agreed approach: Return early\n\n"
            f"{describe(_finding('F2'))}\n"
            "  Agreed approach: Guard the index\n\n"
            "Rules:\n"
            "- Implement exactly these items and nothing else.\n"
            "- Make changes directly to the files; do not use the Agent tool.\n"
            "- Run the project's full test suite and its lint and format checks, and fix "
            "anything you broke.\n"
            '- End with one line starting with "SUMMARY:" that says what you changed.'
        )
