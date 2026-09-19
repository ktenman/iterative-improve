import logging
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from improve.ci_gh import GitHubCI
from improve.ci_glab import GitLabCI
from improve.cli import _parse_args, _validate_phases, main
from improve.config import Config
from improve.mode import Mode
from improve.runner import IterationLoop
from improve.state import LoopState


def _config_of(mocks: dict[str, MagicMock]) -> Config:
    return mocks["improve.cli.IterationLoop"].call_args[1]["config"]


@contextmanager
def _run_main(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], **overrides: Any
) -> Iterator[dict[str, MagicMock]]:
    monkeypatch.setattr("sys.argv", ["iterative-improve", *argv])
    defaults = {
        "improve.cli._setup_logging": {},
        "improve.cli.check_for_update": {},
        "improve.cli.require_tools": {},
        "improve.codex.check_model": {"return_value": ""},
        "improve.git.branch": {"return_value": "feature"},
        "improve.git.resolve_existing_conflicts": {"return_value": True},
        "improve.git.changed_files": {"return_value": []},
        "improve.cli.run_preflight": {},
        "improve.git.sync_with_main": {"return_value": True},
        "improve.cli.IterationLoop": {"wraps": IterationLoop},
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
        assert args.ci_workflow is None
        assert args.phase_timeout is None
        assert args.effort == "max"
        assert args.codex_model == "gpt-6-astra"
        assert args.council is False

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

    def test_parses_ci_workflow(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "--ci-workflow", "Build"])
        args = _parse_args()
        assert args.ci_workflow == "Build"


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
            pytest.param(["--phase-timeout", "0"], id="phase_timeout_zero"),
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

    def test_exits_when_working_tree_has_uncommitted_changes(self, monkeypatch):
        with (
            _run_main(
                monkeypatch,
                ["-n", "1", "--skip-ci"],
                **{
                    "improve.git.changed_files": {
                        "return_value": ["docs/plans/notes.md", "scripts/scratch.ipynb"]
                    }
                },
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()
        assert exc_info.value.code == 1

    def test_resolves_pre_existing_conflicts_before_rejecting_a_dirty_tree(self, monkeypatch):
        with (
            _run_main(
                monkeypatch,
                ["-n", "1", "--skip-ci"],
                **{"improve.git.changed_files": {"return_value": ["conflicted.py"]}},
            ) as mocks,
            pytest.raises(SystemExit),
        ):
            main()
        mocks["improve.git.resolve_existing_conflicts"].assert_called_once()

    def test_accepts_minimum_valid_iterations(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci"]) as mocks:
            main()
            mocks["improve.runner.IterationLoop.run"].assert_called_once_with(1, 1)

    def test_passes_phase_timeout_to_config(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--phase-timeout", "30"]) as mocks:
            main()

        assert _config_of(mocks).agent_timeout == 30

    def test_passes_ci_timeout_to_config(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--ci-timeout", "1"]) as mocks:
            main()

        assert _config_of(mocks).ci_timeout == 60

    def test_passes_ci_workflow_to_github_provider(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--ci-workflow", "Build"]) as mocks:
            main()

        provider = _config_of(mocks).ci_provider
        assert isinstance(provider, GitHubCI)
        assert provider._workflow == "Build"

    def test_uses_gitlab_provider_when_specified(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--ci-provider", "gitlab"]) as mocks:
            main()

        assert isinstance(_config_of(mocks).ci_provider, GitLabCI)
        mocks["improve.cli.require_tools"].assert_called_once_with(["git", "claude", "glab"])

    def test_uses_gh_tool_for_github_provider(self, monkeypatch):
        with _run_main(
            monkeypatch,
            ["-n", "1", "--skip-ci", "--ci-provider", "github"],
        ) as mocks:
            main()
            mocks["improve.cli.require_tools"].assert_called_once_with(["git", "claude", "gh"])

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
            (["--skip-ci", "--council"], "Mode:       council\n  Codex:      gpt-6-astra\n"),
            (["--skip-ci"], "Effort:     max (2700s timeout)\n"),
            (["--skip-ci", "--effort", "medium"], "Effort:     medium (900s timeout)\n"),
        ],
    )
    def test_header_shows_mode_flag(self, monkeypatch, capsys, extra_flags, expected):
        with _run_main(monkeypatch, ["-n", "1", *extra_flags]):
            main()
        assert expected in capsys.readouterr().out


class TestCouncilOptions:
    def test_council_flag_runs_the_loop_in_council_mode(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--council"]) as mocks:
            main()

        assert mocks["improve.cli.IterationLoop"].call_args[1]["mode"] == Mode.COUNCIL

    @pytest.mark.parametrize("other", ["--batch", "--parallel"])
    def test_council_cannot_be_combined_with_batch_or_parallel(self, monkeypatch, other):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "--council", other])

        with pytest.raises(SystemExit) as exc_info:
            _parse_args()

        assert exc_info.value.code == 2

    def test_rejects_an_unknown_effort(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "--effort", "high"])

        with pytest.raises(SystemExit) as exc_info:
            _parse_args()

        assert exc_info.value.code == 2

    @pytest.mark.parametrize(("effort", "timeout"), [("max", 2700), ("medium", 900)])
    def test_phase_timeout_follows_the_effort_by_default(self, monkeypatch, effort, timeout):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--effort", effort]) as mocks:
            main()

        assert _config_of(mocks).agent_timeout == timeout

    def test_an_explicit_phase_timeout_wins_over_the_effort(self, monkeypatch):
        argv = ["-n", "1", "--skip-ci", "--effort", "medium", "--phase-timeout", "1200"]
        with _run_main(monkeypatch, argv) as mocks:
            main()

        assert _config_of(mocks).agent_timeout == 1200

    def test_passes_effort_and_codex_model_to_config(self, monkeypatch):
        argv = ["-n", "1", "--skip-ci", "--effort", "medium", "--codex-model", "m-1"]
        with _run_main(monkeypatch, argv) as mocks:
            main()

        config = _config_of(mocks)
        assert (config.effort, config.codex_model) == ("medium", "m-1")

    def test_requires_codex_only_in_council_mode(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--council"]) as mocks:
            main()

        mocks["improve.cli.require_tools"].assert_called_once_with(["git", "claude", "gh", "codex"])

    def test_checks_the_codex_model_in_council_mode(self, monkeypatch, caplog):
        with (
            _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--council"]) as mocks,
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            main()

        mocks["improve.codex.check_model"].assert_called_once_with(_config_of(mocks))
        assert "preflight] Checking Codex model gpt-6-astra..." in caplog.messages

    def test_does_not_check_the_codex_model_outside_council_mode(self, monkeypatch):
        with _run_main(monkeypatch, ["-n", "1", "--skip-ci"]) as mocks:
            main()

        mocks["improve.codex.check_model"].assert_not_called()

    def test_exits_when_codex_cannot_use_the_model(self, monkeypatch, caplog):
        failing = {"improve.codex.check_model": {"return_value": "model not supported"}}
        with (
            _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--council"], **failing) as mocks,
            caplog.at_level(logging.ERROR, logger="improve"),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1
        assert caplog.messages == [
            "preflight] Codex cannot use model gpt-6-astra: model not supported"
        ]
        mocks["improve.cli.IterationLoop"].assert_not_called()

    def test_logs_the_effort_when_starting(self, monkeypatch, caplog):
        with (
            _run_main(monkeypatch, ["-n", "1", "--skip-ci", "--council"]),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            main()

        assert (
            "loop] Started: branch=feature iterations=1-1 phases=simplify,review,security "
            "mode=council effort=max skip_ci=True"
        ) in caplog.messages

    def test_help_documents_the_council_options(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["iterative-improve", "--help"])

        with pytest.raises(SystemExit):
            _parse_args()

        text = " ".join(capsys.readouterr().out.split())
        assert (
            "--council Claude and Codex review together; Claude fixes only what both agree on"
            in text
        )
        assert "--effort {max,medium} Reasoning effort for every Claude and Codex call" in text
        assert "(default: max)" in text
        assert "--codex-model CODEX_MODEL Codex model for --council (default: gpt-6-astra)" in text
        assert (
            "--phase-timeout PHASE_TIMEOUT Seconds before a Claude or Codex call is killed "
            "(default: 2700 at max effort, 900 at medium)"
        ) in text
