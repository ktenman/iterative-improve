import pytest

from improve.prompt import (
    AVAILABLE_PHASES,
    build_commit_message,
    build_phase_prompt,
    extract_summary,
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


class TestAvailablePhases:
    def test_contains_all_expected_phases(self):
        assert set(AVAILABLE_PHASES) == {"simplify", "review", "security"}


class TestBuildPhasePrompt:
    @pytest.mark.parametrize("phase", AVAILABLE_PHASES)
    def test_includes_branch_diff_in_prompt(self, phase):
        result = build_phase_prompt(phase, "file.py", "None")

        assert "file.py" in result
