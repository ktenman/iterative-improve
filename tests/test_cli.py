from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from improve.cli import _parse_args, _validate_phases, main
from improve.runner import IterationLoop
from improve.state import LoopState


@contextmanager
def _run_main(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], **overrides: Any
) -> Iterator[dict[str, MagicMock]]:
    monkeypatch.setattr("sys.argv", ["iterative-improve", *argv])
    defaults = {
        "improve.cli._setup_logging": {},
        "improve.cli.check_for_update": {},
        "improve.cli.require_tools": {},
        "improve.git.branch": {"return_value": "feature"},
        "improve.git.resolve_existing_conflicts": {"return_value": True},
        "improve.cli.run_preflight": {},
        "improve.git.sync_with_main": {"return_value": True},
        "improve.runner.IterationLoop.run": {},
        "improve.runner.IterationLoop.install_signal_handlers": {},
    }
    defaults.update(overrides)
    with ExitStack() as stack:
        mocks = {}
        for target, kwargs in defaults.items():
            mocks[target] = stack.enter_context(patch(target, **kwargs))
        yield mocks


class TestParseArgs:
    def test_uses_default_values_when_no_args_given(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["iterative-improve"])
        args = _parse_args()
        assert args.iterations is None
        assert args.ci_timeout == 15
        assert args.skip_ci is False
        assert args.batch is False
        assert args.resume is False
        assert "simplify" in args.phases
        assert "security" in args.phases
        assert args.squash is False
        assert args.ci_provider is None
        assert args.phase_timeout == 900

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
                "--phase-timeout",
                "300",
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
        assert args.phase_timeout == 300

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

    @pytest.mark.parametrize("raw", ["invalid_phase", "", ","])
    def test_exits_on_invalid_input(self, raw):
        with pytest.raises(SystemExit):
            _validate_phases(raw)


class TestValidatePhasesEdgeCases:
    def test_strips_whitespace_around_phases(self):
        assert _validate_phases(" simplify , review ") == ["simplify", "review"]

    def test_filters_empty_entries_from_consecutive_commas(self):
        assert _validate_phases("simplify,,review") == ["simplify", "review"]

    def test_all_three_phases_are_valid(self):
        result = _validate_phases("simplify,review,security")
        assert result == ["simplify", "review", "security"]


class TestMainBoundaryValues:
    def test_phase_timeout_exactly_30_is_accepted(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--phase-timeout", "30"]) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once()

    def test_ci_timeout_exactly_1_is_accepted(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--ci-timeout", "1"]) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once()

    def test_iterations_exactly_1_is_accepted(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci"]) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once_with(1, 1)


class TestMain:
    @pytest.mark.parametrize("branch_name", ["main", "master"])
    def test_exits_with_code_1_when_on_protected_branch(self, monkeypatch, branch_name):
        with (
            _run_main(
                monkeypatch,
                ["-n", "1"],
                **{"improve.git.branch": {"return_value": branch_name}},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_resumes_from_matching_saved_state(self, monkeypatch):
        saved = LoopState(branch="feature", started_at="2025-01-01", iteration=2)
        with _run_main(
            monkeypatch,
            ["-n", "3", "--resume", "--skip-ci"],
            **{"improve.state.LoopState.load": {"return_value": saved}},
        ) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once_with(3, 3)

    def test_starts_fresh_when_no_matching_resume_state(self, monkeypatch):
        with _run_main(
            monkeypatch,
            ["-n", "3", "--resume", "--skip-ci"],
            **{"improve.state.LoopState.load": {"return_value": None}},
        ) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once_with(1, 3)

    @pytest.mark.parametrize(
        "extra_args",
        [
            pytest.param(["-n", "0"], id="iterations_zero"),
            pytest.param(["-n", "-3"], id="iterations_negative"),
            pytest.param(["--phase-timeout", "10"], id="phase_timeout_too_low"),
            pytest.param(["--phase-timeout", "29"], id="phase_timeout_below_min"),
            pytest.param(["--ci-timeout", "0"], id="ci_timeout_zero"),
            pytest.param(["--ci-timeout", "-5"], id="ci_timeout_negative"),
        ],
    )
    def test_exits_with_code_1_when_argument_below_minimum(self, monkeypatch, extra_args):
        with (
            _run_main(monkeypatch, extra_args),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_runs_in_continuous_mode_by_default(self, monkeypatch):
        with _run_main(monkeypatch, ["--skip-ci"]) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once_with(1, 1000)

    def test_exits_when_initial_sync_with_main_fails(self, monkeypatch):
        with (
            _run_main(
                monkeypatch,
                ["-n", "1", "--skip-ci"],
                **{"improve.git.sync_with_main": {"return_value": False}},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_exits_when_detached_head(self, monkeypatch):
        with (
            _run_main(
                monkeypatch,
                ["-n", "1"],
                **{"improve.git.branch": {"return_value": ""}},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_exits_when_unresolved_merge_conflicts(self, monkeypatch):
        with (
            _run_main(
                monkeypatch,
                ["-n", "1"],
                **{"improve.git.resolve_existing_conflicts": {"return_value": False}},
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_accepts_minimum_valid_iterations(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci"]) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once_with(1, 1)

    def test_passes_phase_timeout_to_config(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["iterative-improve", "-n", "1", "--skip-ci", "--phase-timeout", "30"]
        )
        mock_loop = MagicMock(spec=IterationLoop)
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            patch("improve.git.branch", return_value="feature"),
            patch("improve.git.resolve_existing_conflicts", return_value=True),
            patch("improve.cli.run_preflight"),
            patch("improve.git.sync_with_main", return_value=True),
            patch("improve.cli.IterationLoop", return_value=mock_loop) as mock_cls,
        ):
            main()

        assert mock_cls.call_args[1]["config"].claude_timeout == 30

    def test_passes_ci_timeout_to_config(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["iterative-improve", "-n", "1", "--skip-ci", "--ci-timeout", "1"]
        )
        mock_loop = MagicMock(spec=IterationLoop)
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools"),
            patch("improve.git.branch", return_value="feature"),
            patch("improve.git.resolve_existing_conflicts", return_value=True),
            patch("improve.cli.run_preflight"),
            patch("improve.git.sync_with_main", return_value=True),
            patch("improve.cli.IterationLoop", return_value=mock_loop) as mock_cls,
        ):
            main()

        assert mock_cls.call_args[1]["config"].ci_timeout == 60

    def test_uses_gitlab_provider_when_specified(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["iterative-improve", "-n", "1", "--skip-ci", "--ci-provider", "gitlab"],
        )
        mock_loop = MagicMock(spec=IterationLoop)
        with (
            patch("improve.cli._setup_logging"),
            patch("improve.cli.check_for_update"),
            patch("improve.cli.require_tools") as mock_require,
            patch("improve.git.branch", return_value="feature"),
            patch("improve.git.resolve_existing_conflicts", return_value=True),
            patch("improve.cli.run_preflight"),
            patch("improve.git.sync_with_main", return_value=True),
            patch("improve.cli.IterationLoop", return_value=mock_loop) as mock_cls,
        ):
            main()
            from improve.ci_glab import GitLabCI

            config = mock_cls.call_args[1]["config"]
            assert isinstance(config.ci_provider, GitLabCI)
            mock_require.assert_called_once_with("glab")

    def test_uses_gh_tool_for_github_provider(self, monkeypatch):
        with _run_main(
            monkeypatch,
            ["-n", "1", "--skip-ci", "--ci-provider", "github"],
        ) as mocks:
            main()
            mocks["improve.cli.require_tools"].assert_called_once_with("gh")

    def test_passes_no_color_flag_to_color_init(self, monkeypatch):
        with _run_main(
            monkeypatch,
            ["-n", "1", "--skip-ci", "--no-color"],
            **{"improve.cli.color": {}},
        ) as mocks:
            main()
            mocks["improve.cli.color"].init.assert_called_once_with(force_no_color=True)

    def test_calls_install_signal_handlers(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci"]) as mocks:
            main()
            mocks["improve.runner.IterationLoop.install_signal_handlers"].assert_called_once()

    @pytest.mark.parametrize(
        "extra_flags,expected",
        [
            (["--skip-ci", "--batch"], "batch"),
            (["--skip-ci", "--parallel"], "parallel"),
            (["--skip-ci"], "skip"),
            (["--ci-timeout", "20"], "20m timeout"),
            (["--skip-ci", "--squash"], "Squash:"),
        ],
    )
    def test_header_shows_mode_flag(self, monkeypatch, capsys, extra_flags, expected):
        with _run_main(monkeypatch, ["-n", "1", *extra_flags]):
            main()
        assert expected in capsys.readouterr().out
