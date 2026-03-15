import json

from improve.state import LoopState, PhaseResult


class TestPhaseResult:
    def test_creates_phase_result_with_all_fields(self):
        result = PhaseResult(
            iteration=1,
            phase="simplify",
            changes_made=True,
            files=["a.py"],
            summary="Extracted helper",
            ci_passed=True,
            ci_retries=0,
        )
        assert result.iteration == 1
        assert result.phase == "simplify"
        assert result.changes_made is True
        assert result.files == ["a.py"]
        assert result.ci_passed is True

    def test_defaults_duration_fields_to_zero(self):
        result = PhaseResult(1, "review", False, [], "No changes", True, 0)
        assert result.duration_seconds == 0.0
        assert result.claude_seconds == 0.0
        assert result.ci_seconds == 0.0


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
