import itertools
from unittest.mock import MagicMock, patch

from improve.ci import CIProvider
from improve.config import Config
from improve.mode import Mode
from improve.runner import IterationLoop
from improve.state import LoopState


def _integration_config(provider=None):
    return Config(
        claude_timeout=10, ci_timeout=10, ci_provider=provider or MagicMock(spec=CIProvider)
    )


def _integration_loop(tmp_path, monkeypatch, mode=Mode.SEQUENTIAL, skip_ci=True):
    monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
    monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
    state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
    config = _integration_config()
    return IterationLoop(
        state=state,
        skip_ci=skip_ci,
        mode=mode,
        phases=["simplify"],
        config=config,
        squash=False,
    )


class TestBoundaryIntegration:
    def test_phase_with_changes_commits_and_returns_result(self, tmp_path, monkeypatch):
        loop = _integration_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude") as mock_claude,
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.commit_and_push", return_value=True),
        ):
            mock_claude.return_value = ("SUMMARY: Extracted helper", 1.5)
            result = loop.run_phase("simplify", 1, skip_ci=True)

        assert result.changes_made is True
        assert result.files == ["file.py"]
        assert result.claude_seconds == 1.5

    def test_full_sequential_iteration_converges(self, tmp_path, monkeypatch):
        loop = _integration_loop(tmp_path, monkeypatch)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude") as mock_claude,
            patch("improve.git.changed_files", side_effect=[["file.py"], []]),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.git.sync_with_main", return_value=True),
        ):
            mock_claude.return_value = ("SUMMARY: Fixed", 1.0)
            loop.run(1, 2)

        assert loop.state.iteration == 2

    def test_ci_failure_stops_sequential_iteration(self, tmp_path, monkeypatch):
        provider = MagicMock(spec=CIProvider)
        provider.get_latest_run_id.side_effect = [100, 200, 200, 200]
        provider.watch_run.return_value = False
        provider.get_run_conclusion.return_value = "failure"
        provider.get_failed_logs.return_value = "Error: test failed"

        config = Config(claude_timeout=10, ci_timeout=10, ci_provider=provider)
        monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
        monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
        state = LoopState(branch="feature", started_at="2025-01-01T00:00:00")
        loop = IterationLoop(
            state=state,
            skip_ci=False,
            mode=Mode.SEQUENTIAL,
            phases=["simplify"],
            config=config,
        )

        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude") as mock_claude,
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.git.has_changes", return_value=False),
            patch("improve.git.sync_with_main", return_value=True),
            patch("time.sleep"),
            patch("time.monotonic", side_effect=itertools.count()),
        ):
            mock_claude.return_value = ("SUMMARY: Fix", 1.0)
            loop.run(1, 1)

        assert loop.state.results[0]["ci_passed"] is False

    def test_batch_mode_runs_all_phases_then_checks_ci(self, tmp_path, monkeypatch):
        loop = _integration_loop(tmp_path, monkeypatch, mode=Mode.BATCH)
        with (
            patch("improve.git.diff_vs_main", return_value="file.py"),
            patch("improve.claude.run_claude") as mock_claude,
            patch("improve.git.changed_files", return_value=["file.py"]),
            patch("improve.git.commit_and_push", return_value=True),
            patch("improve.git.sync_with_main", return_value=True),
        ):
            mock_claude.return_value = ("SUMMARY: Cleaned", 1.0)
            loop.run(1, 1)

        assert len(loop.state.results) == 1
        assert loop.state.results[0]["changes_made"] is True
