import pytest

from improve.phases import (
    ACTION_VERBS,
    _truncate,
    build_ci_fix_prompt,
    build_commit_message,
    build_conflict_prompt,
    build_phase_prompt,
    build_squash_prompt,
    extract_summary,
    strip_code_fences,
)


class TestExtractSummary:
    @pytest.mark.parametrize(
        "output, expected",
        [
            ("Some text\nSUMMARY: Fixed duplicate imports\nMore text", "Fixed duplicate imports"),
            ("Short\nThis is a longer line that qualifies", "This is a longer line that qualifies"),
            ("", "Code improvements"),
            ("summary: Refactored error handling", "Refactored error handling"),
            ("SUMMARY: upper case", "upper case"),
            ("Summary: mixed case", "mixed case"),
            ("   \n   ", "Code improvements"),
            ("short\nalso short", "Code improvements"),
            ("SUMMARY:", ""),
            ("SUMMARY:   spaces around   ", "spaces around"),
            ("SUMMARY: no newline at all", "no newline at all"),
            ("\nSUMMARY: preceded by newline", "preceded by newline"),
        ],
    )
    def test_extracts_summary(self, output, expected):
        assert extract_summary(output) == expected

    def test_summary_takes_precedence_over_fallback(self):
        result = extract_summary("This is a long fallback line\nSUMMARY: actual summary")
        assert result == "actual summary"

    def test_picks_first_long_line_as_fallback(self):
        result = extract_summary("short\nFirst long line here!!\nSecond long line here!!")
        assert result == "First long line here!!"

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("123456789012345", "Code improvements"),
            ("1234567890123456", "1234567890123456"),
        ],
    )
    def test_boundary_at_15_and_16_chars(self, text, expected):
        assert extract_summary(text) == expected


class TestBuildCommitMessage:
    @pytest.mark.parametrize(
        "phase, summary, expected_prefix",
        [
            ("simplify", "Extract shared logic", "Extract"),
            ("simplify", "duplicated validation", "Simplify"),
            ("review", "missing null check", "Fix"),
            ("security", "exposed credentials in config", "Fix"),
            ("unknown_phase", "something", "Fix"),
        ],
    )
    def test_starts_with_correct_prefix(self, phase, summary, expected_prefix):
        result = build_commit_message(phase, summary)
        assert result.startswith(expected_prefix)

    def test_truncates_long_messages_at_50_chars(self):
        result = build_commit_message(
            "simplify",
            "Remove unnecessary complexity from the authentication middleware layer",
        )
        assert len(result) <= 50

    @pytest.mark.parametrize(
        "phase, summary, expected",
        [
            ("review", "``****``", "Fix code issues"),
            ("simplify", "```", "Simplify code"),
            ("review", "", "Fix code issues"),
            ("simplify", "", "Simplify code"),
        ],
    )
    def test_empty_summary_uses_fallback(self, phase, summary, expected):
        assert build_commit_message(phase, summary) == expected

    def test_capitalizes_action_verb_from_summary(self):
        result = build_commit_message("simplify", "extract shared logic")
        assert result.startswith("Extract")

    def test_lowercases_first_char_when_not_action_verb(self):
        result = build_commit_message("review", "Broken null check")
        assert result.startswith("Fix broken")

    def test_action_verb_preserves_rest_of_summary(self):
        result = build_commit_message("simplify", "add new helper function")
        assert result == "Add new helper function"


class TestActionVerbsHandling:
    @pytest.mark.parametrize(
        "verb",
        [
            "add",
            "fix",
            "update",
            "remove",
            "extract",
            "simplify",
            "refactor",
            "replace",
            "move",
            "rename",
            "clean",
        ],
    )
    def test_each_action_verb_uses_capitalized_form(self, verb):
        assert verb in ACTION_VERBS
        result = build_commit_message("review", f"{verb} the thing")
        assert result == f"{verb.capitalize()} the thing"

    def test_action_verbs_contains_exactly_expected_members(self):
        expected = {
            "add",
            "fix",
            "update",
            "remove",
            "extract",
            "simplify",
            "refactor",
            "replace",
            "move",
            "rename",
            "clean",
        }
        assert expected == ACTION_VERBS

    def test_non_action_verb_uses_phase_prefix(self):
        result = build_commit_message("review", "broken something")
        assert result.startswith("Fix")


class TestStripCodeFences:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("```\nSquash commit message\n```", "Squash commit message"),
            ("```markdown\nSquash commit message\n```", "Squash commit message"),
            ("Clean message without fences", "Clean message without fences"),
            ("```\nLine one\n\nLine two\n```", "Line one\n\nLine two"),
            ("", ""),
            ("  ```\n  msg\n  ```  ", "msg"),
            ("```python\ncode\n```", "code"),
            ("```\nno close", "no close"),
            ("no open\n```", "no open"),
            ("```\nab\n```", "ab"),
            ("```\nabc\n```", "abc"),
        ],
    )
    def test_strips_code_fences_from_claude_output(self, text, expected):
        assert strip_code_fences(text) == expected


class TestBuildCiFixPrompt:
    @pytest.mark.parametrize(
        "expected_substring",
        ["test failed: assert False", "Fix only", "SUMMARY:"],
    )
    def test_prompt_contains_expected_content(self, expected_substring):
        result = build_ci_fix_prompt("test failed: assert False")
        assert expected_substring in result


class TestBuildConflictPrompt:
    def test_includes_conflicted_file_names(self):
        result = build_conflict_prompt(["src/a.py", "src/b.py"])
        assert "src/a.py" in result
        assert "src/b.py" in result

    def test_includes_resolve_instructions(self):
        result = build_conflict_prompt(["file.py"])
        assert "conflict markers" in result

    def test_joins_files_with_newlines(self):
        result = build_conflict_prompt(["a.py", "b.py"])
        assert "a.py\nb.py" in result


class TestBuildSquashPrompt:
    def test_returns_prompt_and_fallback_for_single_result(self):
        results = [{"phase": "simplify", "summary": "Extracted helper"}]
        prompt, fallback = build_squash_prompt("diff content", results)
        assert "simplify" in prompt
        assert "Extracted helper" in prompt
        assert "Extracted helper" in fallback

    def test_returns_combined_fallback_for_multiple_results(self):
        results = [
            {"phase": "simplify", "summary": "Simplified code"},
            {"phase": "review", "summary": "Fixed bug"},
        ]
        _prompt, fallback = build_squash_prompt("diff", results)
        assert "Improve code (review, simplify)" in fallback

    def test_truncates_diff_at_8000_chars(self):
        long_diff = "x" * 20000
        prompt, _ = build_squash_prompt(long_diff, [{"phase": "simplify", "summary": "s"}])
        assert "x" * 8000 in prompt
        assert "x" * 8001 not in prompt

    def test_fallback_includes_all_summaries(self):
        results = [
            {"phase": "simplify", "summary": "Simplified A"},
            {"phase": "review", "summary": "Fixed B"},
        ]
        _, fallback = build_squash_prompt("diff", results)
        assert "Simplified A" in fallback
        assert "Fixed B" in fallback

    def test_prompt_includes_rules(self):
        results = [{"phase": "review", "summary": "s"}]
        prompt, _ = build_squash_prompt("diff", results)
        assert "50 chars" in prompt
        assert "imperative verb" in prompt


class TestTruncate:
    @pytest.mark.parametrize(
        "msg, limit, expected_len, ends_with_ellipsis",
        [
            ("Short msg", 50, None, False),
            ("x" * 50, 50, 50, False),
            ("x" * 51, 50, 50, True),
            ("a" * 60, 50, 50, True),
            ("a" * 60, 30, 30, True),
        ],
    )
    def test_length_boundaries(self, msg, limit, expected_len, ends_with_ellipsis):
        result = _truncate(msg, limit)
        if expected_len is not None:
            assert len(result) <= expected_len
        else:
            assert result == msg
        assert result.endswith("...") == ends_with_ellipsis

    def test_truncates_at_word_boundary_when_space_found(self):
        msg = "Fix the broken authentication middleware code"
        result = _truncate(msg, 40)
        assert result.endswith("...")
        assert len(result) <= 40

    def test_hard_truncates_when_no_space_after_20(self):
        msg = "a" * 60
        result = _truncate(msg, 50)
        assert result == "a" * 47 + "..."

    def test_space_at_position_21_uses_word_boundary(self):
        msg = "aaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        result = _truncate(msg, 50)
        assert result == "aaaaaaaaaaaaaaaaaaaaa..."

    def test_space_at_position_20_uses_hard_truncate(self):
        msg = "aaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        result = _truncate(msg, 50)
        assert result == "aaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbb..."

    def test_one_over_limit_gets_truncated(self):
        msg = "a" * 51
        result = _truncate(msg, 50)
        assert result.endswith("...")
        assert len(result) == 50

    def test_default_max_length_is_exactly_50(self):
        assert _truncate("a" * 50) == "a" * 50
        result = _truncate("a" * 51)
        assert result.endswith("...")
        assert len(result) == 50


class TestExtractSummarySlicePrecision:
    def test_summary_colon_immediately_followed_by_text_returns_text(self):
        assert extract_summary("SUMMARY:NoSpace") == "NoSpace"

    def test_summary_with_exactly_eight_char_prefix_strips_correctly(self):
        assert extract_summary("SUMMARY:X") == "X"


class TestTruncateConstants:
    def test_ellipsis_is_exactly_3_chars(self):
        result = _truncate("a" * 51, 50)
        assert result == "a" * 47 + "..."

    def test_word_boundary_threshold_is_exactly_20(self):
        msg = "a" * 19 + " " + "b" * 40
        result = _truncate(msg, 50)
        assert result == msg[:47] + "..."

    def test_word_boundary_threshold_at_21_truncates_at_space(self):
        msg = "a" * 21 + " " + "b" * 40
        result = _truncate(msg, 50)
        assert result == "a" * 21 + "..."

    def test_exact_limit_not_truncated(self):
        msg = "a" * 50
        assert _truncate(msg, 50) == msg

    def test_one_under_limit_not_truncated(self):
        msg = "a" * 49
        assert _truncate(msg, 50) == msg


class TestStripCodeFencesSlicePrecision:
    def test_removes_exactly_three_backticks_from_end(self):
        assert strip_code_fences("content\n```") == "content"

    def test_skips_language_tag_line_completely(self):
        assert strip_code_fences("```python\ncode\n```") == "code"

    def test_first_newline_plus_one_skips_backtick_line(self):
        assert strip_code_fences("```\ncontent") == "content"

    def test_no_newline_in_opening_fence_preserved(self):
        assert strip_code_fences("```") == ""

    def test_backtick_only_content_between_fences(self):
        assert strip_code_fences("```\na\n```") == "a"

    def test_exactly_three_chars_removed_from_end(self):
        result = strip_code_fences("```\nXYZ```\n```")
        assert result == "XYZ```"

    def test_opening_fence_without_newline_keeps_opening_backticks(self):
        result = strip_code_fences("``````")
        assert result == "```"

    def test_opening_fence_with_newline_at_position_zero_is_handled(self):
        result = strip_code_fences("```\nline1\nline2\n```")
        assert result == "line1\nline2"


class TestExtractSummaryEmptyColon:
    def test_summary_with_only_spaces_after_colon_returns_empty(self):
        assert extract_summary("SUMMARY:   ") == ""

    def test_returns_text_immediately_after_colon(self):
        assert extract_summary("SUMMARY:x") == "x"

    def test_fallback_not_used_when_summary_found(self):
        assert extract_summary("A long fallback line here\nSUMMARY: actual") == "actual"


class TestBuildCommitMessageBranches:
    def test_simplify_empty_uses_simplify_code_not_fix(self):
        result = build_commit_message("simplify", "")
        assert result == "Simplify code"
        assert "issues" not in result

    def test_review_empty_uses_fix_code_issues(self):
        result = build_commit_message("review", "")
        assert result == "Fix code issues"
        assert "code issues" in result

    def test_prefix_fix_gives_code_issues_fallback(self):
        result = build_commit_message("security", "")
        assert "issues" in result

    def test_prefix_simplify_gives_code_fallback_without_issues(self):
        result = build_commit_message("simplify", "  ")
        assert result == "Simplify code"


class TestBuildSquashPromptBranches:
    def test_single_result_uses_build_commit_message_for_subject(self):
        results = [{"phase": "simplify", "summary": "extract helper"}]
        _, fallback = build_squash_prompt("diff", results)
        assert fallback.startswith("Extract helper")

    def test_two_results_uses_improve_code_format(self):
        results = [
            {"phase": "simplify", "summary": "A"},
            {"phase": "review", "summary": "B"},
        ]
        _, fallback = build_squash_prompt("diff", results)
        assert fallback.startswith("Improve code (")

    def test_squash_prompt_phases_are_sorted(self):
        results = [
            {"phase": "review", "summary": "B"},
            {"phase": "simplify", "summary": "A"},
        ]
        _, fallback = build_squash_prompt("diff", results)
        assert "review, simplify" in fallback


class TestBuildPhasePrompt:
    @pytest.mark.parametrize(
        "phase, expected_substring",
        [
            ("review", "NO_CHANGES_NEEDED"),
            ("security", "SUMMARY:"),
            ("simplify", "simplification"),
            ("review", "NEW issues"),
            ("simplify", "do not use the Agent tool"),
            ("review", "lint/format/test"),
        ],
    )
    def test_prompt_contains_expected_content(self, phase, expected_substring):
        result = build_phase_prompt(phase, "diff", "None")
        assert expected_substring in result

    def test_includes_branch_diff_in_prompt(self):
        result = build_phase_prompt("simplify", "file.py", "None")
        assert "file.py" in result

    def test_includes_context_in_prompt(self):
        result = build_phase_prompt("simplify", "diff", "Previous fix applied")
        assert "Previous fix applied" in result

    def test_prompt_starts_with_role_description(self):
        result = build_phase_prompt("simplify", "diff", "None")
        assert result.startswith("You are an expert code simplification")

    def test_prompt_ends_with_summary_instruction(self):
        result = build_phase_prompt("review", "diff", "None")
        assert result.endswith("describing what you changed")


class TestBuildCommitMessagePrecision:
    def test_simplify_empty_returns_exact_fallback(self):
        assert build_commit_message("simplify", "") == "Simplify code"

    def test_review_empty_returns_exact_fallback(self):
        assert build_commit_message("review", "") == "Fix code issues"

    def test_security_empty_returns_exact_fallback(self):
        assert build_commit_message("security", "") == "Fix code issues"

    def test_unknown_phase_empty_returns_fix_fallback(self):
        assert build_commit_message("unknown", "") == "Fix code issues"

    def test_phase_prefix_simplify_is_simplify(self):
        result = build_commit_message("simplify", "some issue found")
        assert result == "Simplify some issue found"

    def test_preserves_rest_after_lowercase_first_char(self):
        result = build_commit_message("review", "Null check missing")
        assert result == "Fix null check missing"
