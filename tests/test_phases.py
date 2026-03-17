import pytest

from improve.phases import (
    AVAILABLE_PHASES,
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
        ],
    )
    def test_extracts_summary(self, output, expected):
        result = extract_summary(output)

        assert result == expected


class TestBuildCommitMessage:
    @pytest.mark.parametrize(
        "phase, summary, expected_prefix",
        [
            ("simplify", "Extract shared logic", "Extract"),
            ("simplify", "duplicated validation", "Simplify"),
            ("review", "missing null check", "Fix"),
            ("security", "exposed credentials in config", "Fix"),
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
        ],
    )
    def test_strips_code_fences_from_claude_output(self, text, expected):
        assert strip_code_fences(text) == expected


class TestBuildCiFixPrompt:
    def test_includes_error_logs_in_prompt(self):
        result = build_ci_fix_prompt("test failed: assert False")

        assert "test failed: assert False" in result

    def test_includes_fix_instruction(self):
        result = build_ci_fix_prompt("error")

        assert "Fix only" in result

    def test_includes_summary_instruction(self):
        result = build_ci_fix_prompt("error")

        assert "SUMMARY:" in result


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

    def test_truncates_diff_in_prompt(self):
        long_diff = "x" * 10000

        prompt, _ = build_squash_prompt(long_diff, [{"phase": "simplify", "summary": "s"}])

        assert len(prompt) < 10000


class TestTruncate:
    def test_returns_message_unchanged_when_within_limit(self):
        assert _truncate("Short msg", 50) == "Short msg"

    def test_returns_message_at_exact_limit(self):
        msg = "x" * 50
        assert _truncate(msg, 50) == msg

    def test_truncates_at_word_boundary_when_space_found(self):
        msg = "Fix the broken authentication middleware code"
        result = _truncate(msg, 40)

        assert result.endswith("...")
        assert len(result) <= 40

    def test_truncates_without_word_boundary_when_no_space_after_20(self):
        msg = "abcdefghijklmnopqrstuvwxyz_no_spaces_here_at_all_ever"
        result = _truncate(msg, 30)

        assert result.endswith("...")
        assert len(result) == 30


class TestBuildCommitMessageEdgeCases:
    def test_returns_fix_code_issues_for_empty_summary_with_fix_prefix(self):
        result = build_commit_message("review", "``****``")

        assert result == "Fix code issues"

    def test_returns_simplify_code_for_empty_summary_with_simplify_prefix(self):
        result = build_commit_message("simplify", "```")

        assert result == "Simplify code"

    def test_capitalizes_action_verb_from_summary(self):
        result = build_commit_message("simplify", "extract shared logic")

        assert result.startswith("Extract")

    def test_lowercases_first_char_when_not_action_verb(self):
        result = build_commit_message("review", "Broken null check")

        assert result.startswith("Fix broken")

    def test_uses_fallback_prefix_for_unknown_phase(self):
        result = build_commit_message("unknown_phase", "something")

        assert result.startswith("Fix")


class TestExtractSummaryBoundary:
    def test_uses_fallback_when_line_is_exactly_15_chars(self):
        result = extract_summary("123456789012345")

        assert result == "Code improvements"

    def test_uses_line_as_fallback_when_16_chars(self):
        result = extract_summary("1234567890123456")

        assert result == "1234567890123456"


class TestBuildPhasePrompt:
    @pytest.mark.parametrize("phase", AVAILABLE_PHASES)
    def test_includes_branch_diff_in_prompt(self, phase):
        result = build_phase_prompt(phase, "file.py", "None")

        assert "file.py" in result

    def test_includes_context_in_prompt(self):
        result = build_phase_prompt("simplify", "diff", "Previous fix applied")

        assert "Previous fix applied" in result

    def test_includes_no_changes_instruction(self):
        result = build_phase_prompt("review", "diff", "None")

        assert "NO_CHANGES_NEEDED" in result

    def test_includes_summary_instruction(self):
        result = build_phase_prompt("security", "diff", "None")

        assert "SUMMARY:" in result
