import json
from dataclasses import asdict

import pytest

from improve import color
from improve.state import CRASHED_SUMMARY, LedgerEntry, LoopState, PhaseResult, format_summary
from tests.conftest import _state


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


def _entry(outcome="fixed", symbol="load"):
    return LedgerEntry(
        iteration=1,
        outcome=outcome,
        phase="review",
        severity="high",
        file="app.py",
        symbol=symbol,
        line=12,
        title="Unchecked None",
        reason="r",
    )


class TestLedger:
    def test_settle_appends_entries_as_dicts_and_saves(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)

        state.settle([_entry(), _entry("skipped")])

        assert state.ledger == [asdict(_entry()), asdict(_entry("skipped"))]
        saved = json.loads((tmp_path / "state.json").read_text())
        assert saved["ledger"] == state.ledger

    def test_save_and_load_round_trips_the_ledger(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.settle([_entry("disputed")])

        loaded = LoopState.load()

        assert loaded is not None
        assert loaded.ledger == [asdict(_entry("disputed"))]

    def test_state_files_without_a_ledger_still_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        (tmp_path / "state.json").write_text(
            json.dumps({"branch": "feat", "started_at": "2025-01-01", "results": []})
        )

        loaded = LoopState.load()

        assert loaded is not None
        assert loaded.ledger == []

    def test_a_new_state_starts_with_an_empty_ledger(self):
        assert LoopState(branch="f", started_at="s").ledger == []


class TestCrashedLast:
    def test_is_false_without_results(self):
        assert LoopState(branch="f", started_at="s").crashed_last("council") is False

    def test_is_true_when_the_last_result_is_a_crash_of_that_phase(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.crashed(1, "council"))

        assert state.crashed_last("council") is True

    def test_is_false_when_the_crash_was_in_another_phase(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.crashed(1, "review"))

        assert state.crashed_last("council") is False

    def test_is_false_when_the_last_result_did_not_crash(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.crashed(1, "council"))
        state.add(PhaseResult.no_changes(2, "council"))

        assert state.crashed_last("council") is False

    def test_crashed_results_use_the_shared_summary(self):
        assert PhaseResult.crashed(1, "council").summary == CRASHED_SUMMARY == "Phase crashed"


class TestUnchangedLast:
    def test_is_false_without_results(self):
        assert LoopState(branch="f", started_at="s").unchanged_last("council") is False

    def test_is_true_when_the_last_pass_of_that_phase_changed_nothing(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.no_changes(1, "council"))

        assert state.unchanged_last("council") is True

    def test_is_false_when_the_last_pass_changed_files(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.no_changes(1, "council"))
        state.add(PhaseResult(2, "council", True, ["app.py"], "Guard empty input", True, 0))

        assert state.unchanged_last("council") is False

    def test_is_false_when_the_unchanged_pass_was_another_phase(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.no_changes(1, "review"))

        assert state.unchanged_last("council") is False

    def test_a_crash_is_not_a_pass_that_changed_nothing(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.crashed(1, "council"))

        assert state.unchanged_last("council") is False

    def test_looks_past_crashes_to_the_last_pass_that_finished(self, tmp_path, monkeypatch):
        state = _state(tmp_path, monkeypatch)
        state.add(PhaseResult.no_changes(1, "council"))
        state.add(PhaseResult.crashed(2, "council"))

        assert state.unchanged_last("council") is True


class TestCodexSeconds:
    def test_defaults_to_zero(self):
        assert PhaseResult(1, "review", False, [], "x", True, 0).codex_seconds == 0.0

    def test_no_changes_factory_leaves_it_at_zero(self):
        assert PhaseResult.no_changes(1, "review").codex_seconds == 0.0


class TestFormatSummaryCouncil:
    @pytest.fixture(autouse=True)
    def _disable_color(self):
        color.enabled = False

    def test_shows_codex_time_after_claude_time_when_codex_ran(self):
        result = asdict(
            PhaseResult(1, "council", True, ["a.py"], "Fixed", True, 0, codex_seconds=75.0)
        )

        output = format_summary([result], 100.0)

        assert "  Claude time:    0.0s\n  Codex time:     1m 15s\n  CI time:" in output

    def test_hides_codex_time_when_codex_did_not_run(self):
        result = asdict(PhaseResult(1, "review", True, ["a.py"], "Fixed", True, 0))

        assert "Codex time" not in format_summary([result], 100.0)

    def test_leaves_codex_time_out_of_the_overhead(self):
        result = asdict(
            PhaseResult(
                1, "council", True, ["a.py"], "F", True, 0, claude_seconds=30.0, codex_seconds=20.0
            )
        )

        assert "Overhead:       1m 10s" in format_summary([result], 100.0)

    def test_lists_disputed_findings_before_the_file_paths(self):
        ledger = [asdict(_entry("disputed")), asdict(_entry("fixed", symbol="save"))]

        output = format_summary([], 1.0, ledger)

        assert (
            "\n\n  Disputed (left for you to decide):\n    - app.py:load  Unchecked None"
            "\n\n  State:" in output
        )
        assert "app.py:save" not in output

    def test_shows_the_line_of_a_disputed_finding_without_a_symbol(self):
        output = format_summary([], 1.0, [asdict(_entry("disputed", symbol=""))])

        assert "    - app.py:12  Unchecked None" in output

    def test_omits_the_disputed_section_when_nothing_is_disputed(self):
        assert "Disputed" not in format_summary([], 1.0, [asdict(_entry("skipped"))])

    def test_formats_results_saved_before_codex_time_was_recorded(self):
        result = asdict(PhaseResult(1, "review", True, ["a.py"], "Fixed", True, 0))
        del result["codex_seconds"]

        assert "Codex time" not in format_summary([result], 1.0)

    def test_highlights_the_disputed_heading_when_color_is_on(self):
        color.enabled = True

        output = format_summary([], 1.0, [asdict(_entry("disputed"))])

        assert f"{color.DARK_YELLOW}Disputed (left for you to decide):{color.RESET}" in output
