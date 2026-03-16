import json
from dataclasses import asdict

import pytest

from improve import color
from improve.state import LoopState, PhaseResult, format_summary


class TestPhaseResult:
    def test_uses_safe_defaults_for_optional_fields(self):
        result = PhaseResult(1, "review", False, [], "No changes", True, 0)

        assert result.duration_seconds == 0.0
        assert result.claude_seconds == 0.0
        assert result.ci_seconds == 0.0
        assert result.reverted is False

    def test_no_changes_factory_returns_inactive_result_with_timing(self):
        result = PhaseResult.no_changes(2, "simplify", duration=5.0, claude_seconds=3.0)
        assert result.iteration == 2
        assert result.phase == "simplify"
        assert result.changes_made is False
        assert result.files == []
        assert result.summary == "No changes needed"
        assert result.ci_passed is True
        assert result.ci_retries == 0
        assert result.duration_seconds == 5.0
        assert result.claude_seconds == 3.0

    def test_crashed_factory_returns_inactive_result(self):
        result = PhaseResult.crashed(3, "review")
        assert result.iteration == 3
        assert result.phase == "review"
        assert result.changes_made is False
        assert result.files == []
        assert result.summary == "Phase crashed"
        assert result.ci_passed is True
        assert result.ci_retries == 0


class TestLoopState:
    def test_adds_phase_result_as_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        result = PhaseResult(1, "simplify", True, ["a.py"], "Cleaned up", True, 0)
        state.add(result)
        assert len(state.results) == 1
        assert state.results[0]["phase"] == "simplify"
        assert state.results[0]["changes_made"] is True

    def test_context_returns_none_message_when_no_results(self):
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        assert state.context() == "None (first iteration)"

    def test_context_returns_only_changed_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))
        state.add(PhaseResult(1, "review", False, [], "No changes", True, 0))
        context = state.context()
        assert "Extracted helper" in context
        assert "No changes" not in context

    def test_context_excludes_reverted_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))
        state.add(
            PhaseResult(1, "review", True, ["b.py"], "Reverted change", False, 1, reverted=True)
        )
        context = state.context()
        assert "Extracted helper" in context
        assert "Reverted change" not in context

    def test_save_and_load_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feat-x", started_at="2025-01-01T00:00:00", iteration=3)
        state.add(PhaseResult(1, "simplify", True, ["b.py"], "Removed duplication", True, 0))

        loaded = LoopState.load()
        assert loaded is not None
        assert loaded.branch == "feat-x"
        assert loaded.iteration == 3
        assert len(loaded.results) == 1
        assert loaded.results[0]["summary"] == "Removed duplication"

    def test_load_returns_none_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "nonexistent.json")
        assert LoopState.load() is None

    def test_load_returns_none_on_invalid_json(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "state.json"
        bad_file.write_text("not json")
        monkeypatch.setattr("improve.state.STATE_FILE", bad_file)
        assert LoopState.load() is None

    def test_load_returns_none_on_missing_keys(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "state.json"
        bad_file.write_text(json.dumps({"iteration": 1}))
        monkeypatch.setattr("improve.state.STATE_FILE", bad_file)
        assert LoopState.load() is None


class TestFormatSummary:
    @pytest.fixture(autouse=True)
    def _disable_color(self):
        color.enabled = False

    def test_includes_result_details(self):
        results = [asdict(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))]

        output = format_summary(results, 10.0)

        assert "RESULTS" in output
        assert "Extracted helper" in output
        assert "CI:PASS" in output

    def test_shows_reverted_status(self):
        result = PhaseResult(1, "simplify", True, ["a.py"], "Stuff", False, 1, reverted=True)
        results = [asdict(result)]

        output = format_summary(results, 10.0)

        assert "CI:REVT" in output
        assert "Reverted:       1" in output

    def test_shows_zero_counts_for_empty_results(self):
        output = format_summary([], 0.0)

        assert "Phases run:     0" in output
        assert "Reverted:       0" in output
