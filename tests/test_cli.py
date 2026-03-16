from unittest.mock import patch

import pytest

from improve.cli import _parse_args, _validate_phases, main
from improve.state import LoopState


class TestParseArgs:
    def test_uses_default_values_when_no_args_given(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["iterative-improve"])
        args = _parse_args()
        assert args.iterations == 10
        assert args.ci_timeout == 15
        assert args.skip_ci is False
        assert args.batch is False
        assert args.resume is False
        assert "simplify" in args.phases
        assert "security" in args.phases
        assert args.squash is False
        assert args.ci_provider is None

    def test_parses_all_custom_values(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            [
                "iterative-improve",
                "-n",
                "5",
                "--ci-timeout",
                "20",
                "--skip-ci",
                "--batch",
                "--resume",
                "--phases",
                "simplify,security",
                "--squash",
            ],
        )
        args = _parse_args()
        assert args.iterations == 5
        assert args.ci_timeout == 20
        assert args.skip_ci is True
        assert args.batch is True
        assert args.resume is True
        assert args.phases == "simplify,security"
        assert args.squash is True

    @pytest.mark.parametrize("provider", ["github", "gitlab"])
    def test_parses_ci_provider(self, monkeypatch, provider):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "--ci-provider", provider])
        args = _parse_args()
        assert args.ci_provider == provider


class TestValidatePhases:
    def test_returns_valid_phases(self):
        assert _validate_phases("simplify,review") == ["simplify", "review"]

    def test_returns_single_phase(self):
        assert _validate_phases("security") == ["security"]

    def test_exits_on_invalid_phase(self):
        with pytest.raises(SystemExit):
            _validate_phases("invalid_phase")

    def test_exits_on_empty_phases(self):
        with pytest.raises(SystemExit):
            _validate_phases("")

    def test_exits_on_blank_comma_phases(self):
        with pytest.raises(SystemExit):
            _validate_phases(",")


class TestMain:
    @pytest.mark.parametrize("branch_name", ["main", "master"])
    def test_exits_with_code_1_when_on_protected_branch(self, monkeypatch, branch_name):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "-n", "1"])
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            patch("improve.cli.ci"),
            patch("improve.git.branch", return_value=branch_name),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_resumes_from_matching_saved_state(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "-n", "3", "--resume", "--skip-ci"])
        saved = LoopState(branch="feature", started_at="2025-01-01", iteration=2)
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            patch("improve.cli.ci"),
            patch("improve.git.branch", return_value="feature"),
            patch("improve.git.resolve_existing_conflicts", return_value=True),
            patch("improve.cli.run_preflight"),
            patch("improve.state.LoopState.load", return_value=saved),
            patch("improve.git.sync_with_main", return_value=True),
            patch("improve.loop.IterationLoop.run") as mock_run,
            patch("improve.loop.IterationLoop.install_signal_handlers"),
        ):
            main()
            mock_run.assert_called_once_with(3, 3)

    def test_starts_fresh_when_no_matching_resume_state(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "-n", "3", "--resume", "--skip-ci"])
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            patch("improve.cli.ci"),
            patch("improve.git.branch", return_value="feature"),
            patch("improve.git.resolve_existing_conflicts", return_value=True),
            patch("improve.cli.run_preflight"),
            patch("improve.state.LoopState.load", return_value=None),
            patch("improve.git.sync_with_main", return_value=True),
            patch("improve.loop.IterationLoop.run") as mock_run,
            patch("improve.loop.IterationLoop.install_signal_handlers"),
        ):
            main()
            mock_run.assert_called_once_with(1, 3)

    @pytest.mark.parametrize("n", ["0", "-3"])
    def test_exits_with_code_1_when_iterations_below_minimum(self, monkeypatch, n):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "-n", n])
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    @pytest.mark.parametrize("timeout", ["0", "-5"])
    def test_exits_with_code_1_when_ci_timeout_below_minimum(self, monkeypatch, timeout):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "-n", "1", "--ci-timeout", timeout])
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_exits_when_initial_sync_with_main_fails(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "-n", "1", "--skip-ci"])
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            patch("improve.cli.ci"),
            patch("improve.git.branch", return_value="feature"),
            patch("improve.git.resolve_existing_conflicts", return_value=True),
            patch("improve.git.sync_with_main", return_value=False),
            patch("improve.loop.IterationLoop.install_signal_handlers"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1
