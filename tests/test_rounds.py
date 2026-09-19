import logging

import pytest

from improve.findings import PICKS_SCHEMA, POSITIONS_SCHEMA, Finding, Report
from improve.rounds import Agreement, pick_winner, run_rounds, settle_round_one


def _finding(finding_id):
    report = Report("claude", f"Title {finding_id}", "", "")
    return Finding("review", "app.py", f"fn_{finding_id}", 1, "high", 90, [report], finding_id)


def _position(finding_id, decision, approach="-", reason="because"):
    return {"id": finding_id, "decision": decision, "approach": approach, "reason": reason}


def _pick(finding_id, pick):
    return {"id": finding_id, "pick": pick, "reason": "r"}


def _both_fix(claude_pick, codex_pick):
    return {
        ("claude", "positions"): {"positions": [_position("F1", "fix", "Claude's way")]},
        ("codex", "positions"): {"positions": [_position("F1", "fix", "Codex's way")]},
        ("claude", "picks"): {"picks": [_pick("F1", claude_pick)]},
        ("codex", "picks"): {"picks": [_pick("F1", codex_pick)]},
    }


def _rounds(findings, replies):
    calls = []

    def ask(agent, prompt, schema):
        key = next(iter(schema["properties"]))
        calls.append((agent, key, prompt, schema))
        return replies.get((agent, key), {})

    return run_rounds(findings, ask), calls


class TestSettleRoundOne:
    def test_a_finding_either_reviewer_left_out_is_unanswered_not_disputed(self):
        first, second = _finding("F1"), _finding("F2")
        claude = {"F1": _position("F1", "fix")}
        codex = {"F2": _position("F2", "fix")}

        agreement, still_open = settle_round_one([first, second], claude, codex)

        assert agreement == Agreement(unanswered=[first, second])
        assert still_open == []

    def test_a_finding_both_reviewers_skip_is_settled_with_claudes_reason(self):
        finding = _finding("F1")
        claude = {"F1": _position("F1", "skip", reason="Claude reason")}
        codex = {"F1": _position("F1", "skip", reason="Codex reason")}

        agreement, still_open = settle_round_one([finding], claude, codex)

        assert agreement == Agreement(skipped=[(finding, "Claude reason")])
        assert still_open == []

    @pytest.mark.parametrize(
        ("claude", "codex"), [("fix", "fix"), ("fix", "skip"), ("skip", "fix"), ("skip", "")]
    )
    def test_any_other_combination_stays_open(self, claude, codex):
        finding = _finding("F1")

        agreement, still_open = settle_round_one(
            [finding], {"F1": _position("F1", claude)}, {"F1": _position("F1", codex)}
        )

        assert agreement == Agreement()
        assert still_open == [finding]


class TestPickWinner:
    @pytest.mark.parametrize(
        ("claude", "codex", "winner"),
        [
            ("mine", "theirs", "claude"),
            ("theirs", "mine", "codex"),
            ("theirs", "theirs", "claude"),
            ("mine", "mine", None),
            (None, "theirs", None),
            ("mine", None, None),
        ],
    )
    def test_picks_the_version_both_reviewers_accept(self, claude, codex, winner):
        assert pick_winner(claude, codex) == winner


class TestRunRounds:
    def test_skips_round_two_when_both_reviewers_skip_everything(self):
        replies = {
            ("claude", "positions"): {"positions": [_position("F1", "skip", reason="Not a bug")]},
            ("codex", "positions"): {"positions": [_position("F1", "skip", reason="Fine")]},
        }

        agreement, calls = _rounds([_finding("F1")], replies)

        assert isinstance(agreement, Agreement)
        assert sorted((agent, key) for agent, key, _, _ in calls) == [
            ("claude", "positions"),
            ("codex", "positions"),
        ]
        assert [(f.id, reason) for f, reason in agreement.skipped] == [("F1", "Not a bug")]
        assert (agreement.fixes, agreement.disputed) == ([], [])

    def test_fixes_with_claudes_approach_when_codex_picks_theirs(self):
        agreement, _ = _rounds([_finding("F1")], _both_fix("mine", "theirs"))

        assert [(f.id, approach) for f, approach in agreement.fixes] == [("F1", "Claude's way")]
        assert (agreement.skipped, agreement.disputed) == ([], [])

    def test_fixes_with_codex_approach_when_claude_picks_theirs(self):
        agreement, _ = _rounds([_finding("F1")], _both_fix("theirs", "mine"))

        assert [(f.id, approach) for f, approach in agreement.fixes] == [("F1", "Codex's way")]

    def test_uses_claudes_version_when_both_pick_theirs(self):
        agreement, _ = _rounds([_finding("F1")], _both_fix("theirs", "theirs"))

        assert [approach for _, approach in agreement.fixes] == ["Claude's way"]

    def test_uses_claudes_approach_when_both_want_a_fix_and_neither_yields(self):
        agreement, _ = _rounds([_finding("F1")], _both_fix("mine", "mine"))

        assert [(f.id, approach) for f, approach in agreement.fixes] == [("F1", "Claude's way")]
        assert (agreement.skipped, agreement.disputed) == ([], [])

    def test_disputes_a_finding_when_one_reviewer_wants_a_fix_and_the_other_a_skip(self):
        replies = {
            ("claude", "positions"): {"positions": [_position("F1", "fix", "Guard it")]},
            ("codex", "positions"): {"positions": [_position("F1", "skip", reason="No bug")]},
            ("claude", "picks"): {"picks": [_pick("F1", "mine")]},
            ("codex", "picks"): {"picks": [_pick("F1", "mine")]},
        }

        agreement, _ = _rounds([_finding("F1")], replies)

        assert [f.id for f in agreement.disputed] == ["F1"]
        assert (agreement.fixes, agreement.skipped) == ([], [])

    def test_skips_a_finding_when_the_winning_position_is_skip(self):
        replies = {
            ("claude", "positions"): {
                "positions": [_position("F1", "skip", reason="False positive")]
            },
            ("codex", "positions"): {"positions": [_position("F1", "fix", "Guard it")]},
            ("claude", "picks"): {"picks": [_pick("F1", "mine")]},
            ("codex", "picks"): {"picks": [_pick("F1", "theirs")]},
        }

        agreement, _ = _rounds([_finding("F1")], replies)

        assert [(f.id, reason) for f, reason in agreement.skipped] == [("F1", "False positive")]
        assert agreement.fixes == []

    def test_uses_empty_text_when_the_winning_position_has_no_approach_or_reason(self):
        replies = _both_fix("mine", "theirs")
        replies[("claude", "positions")] = {"positions": [{"id": "F1", "decision": "fix"}]}

        agreement, _ = _rounds([_finding("F1")], replies)

        assert [approach for _, approach in agreement.fixes] == [""]

    def test_a_missing_round_two_pick_leaves_the_finding_unanswered_not_disputed(self):
        replies = _both_fix("mine", "theirs")
        replies[("codex", "picks")] = {"picks": []}

        agreement, _ = _rounds([_finding("F1")], replies)

        assert [f.id for f in agreement.unanswered] == ["F1"]
        assert (agreement.fixes, agreement.skipped, agreement.disputed) == ([], [], [])

    def test_a_round_two_pick_outside_the_schema_leaves_the_finding_unanswered(self):
        replies = _both_fix("mine", "theirs")
        replies[("claude", "picks")] = {"picks": [_pick("F1", ["mine"])]}

        agreement, _ = _rounds([_finding("F1")], replies)

        assert [f.id for f in agreement.unanswered] == ["F1"]
        assert agreement.fixes == []

    def test_disputes_a_finding_when_the_winning_position_is_not_fix_or_skip(self):
        finding = _finding("F1")
        replies = _both_fix("mine", "theirs")
        replies[("claude", "positions")] = {"positions": [_position("F1", "Fix", "Claude's way")]}

        agreement, _ = _rounds([finding], replies)

        assert agreement.fixes == []
        assert agreement.skipped == []
        assert agreement.disputed == [finding]

    def test_leaves_findings_unanswered_when_a_reply_is_malformed(self):
        replies = {
            ("claude", "positions"): {"positions": "not a list"},
            ("codex", "positions"): {
                "positions": [_position("F1", "fix"), "junk", {"decision": "fix"}]
            },
        }

        agreement, calls = _rounds([_finding("F1")], replies)

        assert [f.id for f in agreement.unanswered] == ["F1"]
        assert agreement.disputed == []
        assert len(calls) == 2

    def test_the_first_answer_for_a_repeated_id_wins(self):
        replies = {
            ("claude", "positions"): {
                "positions": [
                    _position("F1", "skip", reason="first"),
                    _position("F1", "fix", reason="second"),
                ]
            },
            ("codex", "positions"): {"positions": [_position("F1", "skip", reason="codex")]},
        }

        agreement, _ = _rounds([_finding("F1")], replies)

        assert [reason for _, reason in agreement.skipped] == ["first"]

    def test_warns_when_a_reviewer_answers_the_same_id_twice(self, caplog):
        replies = {
            ("claude", "positions"): {
                "positions": [_position("F1", "skip"), _position("F1", "skip")]
            },
            ("codex", "positions"): {"positions": [_position("F1", "skip")]},
        }

        with caplog.at_level(logging.WARNING, logger="improve"):
            _rounds([_finding("F1")], replies)

        assert "council] Ignoring a repeated answer for F1" in caplog.messages

    def test_a_failing_reviewer_aborts_the_round_instead_of_waiting(self):
        def ask(agent, prompt, schema):
            if agent == "claude":
                raise RuntimeError("Claude died")
            return {"positions": [_position("F1", "skip")]}

        with pytest.raises(RuntimeError, match="Claude died"):
            run_rounds([_finding("F1")], ask)

    def test_matches_ids_with_surrounding_whitespace(self):
        replies = {
            ("claude", "positions"): {"positions": [_position(" F1 ", "skip")]},
            ("codex", "positions"): {"positions": [_position("F1", "skip")]},
        }

        agreement, _ = _rounds([_finding("F1")], replies)

        assert [f.id for f, _ in agreement.skipped] == ["F1"]

    def test_round_two_only_covers_findings_still_open(self):
        replies = {
            ("claude", "positions"): {
                "positions": [_position("F1", "skip"), _position("F2", "fix", "A")]
            },
            ("codex", "positions"): {
                "positions": [_position("F1", "skip"), _position("F2", "fix", "B")]
            },
            ("claude", "picks"): {"picks": [_pick("F2", "mine")]},
            ("codex", "picks"): {"picks": [_pick("F2", "theirs")]},
        }

        agreement, calls = _rounds([_finding("F1"), _finding("F2")], replies)

        round_two = [prompt for _, key, prompt, _ in calls if key == "picks"]
        assert len(round_two) == 2
        assert all("F2 · review" in p and "F1 · review" not in p for p in round_two)
        assert [(f.id, approach) for f, approach in agreement.fixes] == [("F2", "A")]
        assert [f.id for f, _ in agreement.skipped] == ["F1"]

    def test_asks_both_reviewers_with_the_position_then_pick_schemas(self):
        _, calls = _rounds([_finding("F1")], _both_fix("mine", "theirs"))

        asked = sorted((agent, key, schema is POSITIONS_SCHEMA) for agent, key, _, schema in calls)
        assert asked == [
            ("claude", "picks", False),
            ("claude", "positions", True),
            ("codex", "picks", False),
            ("codex", "positions", True),
        ]
        assert all(schema is PICKS_SCHEMA for _, key, _, schema in calls if key == "picks")

    def test_each_reviewer_sees_its_own_round_one_position_as_mine(self):
        _, calls = _rounds([_finding("F1")], _both_fix("mine", "theirs"))

        prompts = {agent: prompt for agent, key, prompt, _ in calls if key == "picks"}
        assert (
            "Your round 1 position:\n    decision: fix\n    approach: Claude's way"
            in prompts["claude"]
        )
        assert (
            "Your round 1 position:\n    decision: fix\n    approach: Codex's way"
            in prompts["codex"]
        )

    def test_each_reviewer_gets_its_own_round_one_prompt(self):
        _, calls = _rounds([_finding("F1")], _both_fix("mine", "theirs"))

        prompts = {agent: prompt for agent, key, prompt, _ in calls if key == "positions"}
        assert prompts["claude"].startswith("You and Codex reviewed")
        assert prompts["codex"].startswith("You and Claude reviewed")

    def test_logs_the_agreement(self, caplog):
        with caplog.at_level(logging.INFO, logger="improve"):
            _rounds([_finding("F1")], _both_fix("mine", "mine"))

        assert caplog.messages == ["council] Agreed: 1 to fix, 0 to skip, 0 disputed, 0 unanswered"]
