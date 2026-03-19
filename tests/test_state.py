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

    def test_context_formats_each_result_with_phase_and_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))
        state.add(PhaseResult(1, "review", True, ["b.py"], "Fixed bug", True, 0))
        context = state.context()
        assert "- [simplify] Extracted helper" in context
        assert "- [review] Fixed bug" in context

    def test_context_returns_only_changed_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        state.add(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))
        state.add(PhaseResult(1, "review", False, [], "No changes", True, 0))
        context = state.context()
        assert "Extracted helper" in context
        assert "No changes" not in context

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


class TestKeptResults:
    def test_excludes_no_change_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        state.add(PhaseResult(1, "simplify", True, ["a.py"], "Changed", True, 0))
        state.add(PhaseResult(1, "review", False, [], "No changes", True, 0))

        kept = state.kept_results()

        assert len(kept) == 1
        assert kept[0]["summary"] == "Changed"


class TestFormatSummary:
    @pytest.fixture(autouse=True)
    def _disable_color(self):
        color.enabled = False

    def test_includes_result_details(self):
        results = [asdict(PhaseResult(1, "simplify", True, ["a.py"], "Extracted helper", True, 0))]

        output = format_summary(results, 10.0)

        assert "Results" in output
        assert "Extracted helper" in output
        assert "PASS" in output

    def test_shows_zero_counts_for_empty_results(self):
        output = format_summary([], 0.0)

        assert "Phases run:     0" in output

    def test_shows_fail_label_when_ci_failed(self):
        result = PhaseResult(1, "review", True, ["a.py"], "Stuff", False, 0)
        results = [asdict(result)]

        output = format_summary(results, 5.0)

        assert "FAIL" in output

    def test_counts_ci_retries(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "S1", True, 2)),
            asdict(PhaseResult(1, "review", True, ["b.py"], "S2", True, 3)),
        ]

        output = format_summary(results, 10.0)

        assert "CI fixes:       5" in output

    def test_counts_phases_with_changes(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "Changed", True, 0)),
            asdict(PhaseResult(1, "review", False, [], "No changes", True, 0)),
        ]

        output = format_summary(results, 10.0)

        assert "With changes:   1" in output

    def test_shows_state_and_log_file_paths(self):
        output = format_summary([], 0.0)

        assert "State:" in output
        assert "Log:" in output

    def test_calculates_overhead_correctly(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "S", True, 0, 10.0, 3.0, 5.0)),
        ]
        output = format_summary(results, 20.0)

        assert "12.0s" in output

    def test_overhead_never_negative(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "S", True, 0, 10.0, 8.0, 8.0)),
        ]
        output = format_summary(results, 5.0)

        assert "0.0s" in output

    def test_sums_claude_time_across_results(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "S", True, 0, 10.0, 3.0)),
            asdict(PhaseResult(1, "review", True, ["b.py"], "S", True, 0, 10.0, 4.0)),
        ]
        output = format_summary(results, 20.0)

        assert "7.0s" in output

    def test_sums_ci_time_across_results(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "S", True, 0, 10.0, 1.0, 5.0)),
            asdict(PhaseResult(1, "review", True, ["b.py"], "S", True, 0, 10.0, 1.0, 3.0)),
        ]
        output = format_summary(results, 30.0)

        assert "8.0s" in output

    def test_duration_display_uses_format_duration(self):
        results = [asdict(PhaseResult(1, "simplify", True, ["a.py"], "S", True, 0, 45.0))]
        output = format_summary(results, 60.0)
        assert "45.0s" in output

    def test_each_result_has_status_mark(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "Changed", True, 0)),
            asdict(PhaseResult(1, "review", False, [], "No changes", True, 0)),
        ]
        output = format_summary(results, 10.0)
        assert "Changed" in output
        assert "No changes" in output


class TestLoopStateSaveEdgeCases:
    def test_save_writes_json_with_all_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feat", started_at="2025-01-01", iteration=5)
        state.save()

        data = json.loads((tmp_path / "state.json").read_text())
        assert data["branch"] == "feat"
        assert data["iteration"] == 5
        assert data["started_at"] == "2025-01-01"

    def test_save_uses_atomic_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feat", started_at="2025-01-01")
        state.save()

        assert (tmp_path / "state.json").exists()
        assert not (tmp_path / "state.json.tmp").exists()

    def test_load_handles_missing_optional_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        (tmp_path / "state.json").write_text(
            json.dumps({"branch": "feat", "started_at": "2025-01-01"})
        )
        loaded = LoopState.load()

        assert loaded is not None
        assert loaded.iteration == 0
        assert loaded.results == []


class TestPhaseResultDefaults:
    def test_ci_seconds_defaults_to_zero(self):
        result = PhaseResult(1, "review", False, [], "x", True, 0)
        assert result.ci_seconds == 0.0

    def test_no_changes_factory_ci_seconds_defaults_to_zero(self):
        result = PhaseResult.no_changes(1, "review")
        assert result.ci_seconds == 0.0

    def test_crashed_factory_duration_defaults_to_zero(self):
        result = PhaseResult.crashed(1, "review")
        assert result.duration_seconds == 0.0
        assert result.claude_seconds == 0.0
        assert result.ci_seconds == 0.0


class TestLoopStateSaveWarning:
    def test_save_creates_state_dir_if_missing(self, tmp_path, monkeypatch):
        state_dir = tmp_path / "new_dir"
        monkeypatch.setattr("improve.state.STATE_DIR", state_dir)
        monkeypatch.setattr("improve.state.STATE_FILE", state_dir / "state.json")
        state = LoopState(branch="feat", started_at="2025-01-01")
        state.save()

        assert state_dir.exists()
        assert (state_dir / "state.json").exists()


class TestFormatSummaryPrecision:
    @pytest.fixture(autouse=True)
    def _disable_color(self):
        color.enabled = False

    def test_phases_run_count_matches_results_length(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "S", True, 0)),
            asdict(PhaseResult(1, "review", True, ["b.py"], "S", True, 0)),
            asdict(PhaseResult(2, "simplify", False, [], "S", True, 0)),
        ]
        output = format_summary(results, 10.0)
        assert "Phases run:     3" in output

    def test_with_changes_counts_only_changed_results(self):
        results = [
            asdict(PhaseResult(1, "simplify", True, ["a.py"], "S", True, 0)),
            asdict(PhaseResult(1, "review", False, [], "S", True, 0)),
            asdict(PhaseResult(2, "simplify", True, ["b.py"], "S", True, 0)),
        ]
        output = format_summary(results, 10.0)
        assert "With changes:   2" in output


class TestCiLabel:
    @pytest.mark.parametrize(
        "ci_passed,expected_in,expected_not_in",
        [
            (True, "PASS", "FAIL"),
            (False, "FAIL", "PASS"),
        ],
    )
    def test_ci_label_reflects_pass_fail_status(self, ci_passed, expected_in, expected_not_in):
        from improve.state import _ci_label

        r = {"ci_passed": ci_passed}
        result = _ci_label(r)
        assert expected_in in result
        assert expected_not_in not in result
