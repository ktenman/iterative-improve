import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

import improve.process
from improve.process import (
    _check_preflight,
    abort_on_first_failure,
    format_duration,
    require_tools,
    run,
    run_preflight,
    terminate,
    terminate_active,
    track,
    untrack,
)


def _raise(exc):
    raise exc


class TestRun:
    def test_returns_completed_process_on_success(self):
        with patch("improve.process.subprocess.run") as mock_subproc:
            mock_subproc.return_value = subprocess.CompletedProcess(
                args=["echo", "hello"], returncode=0, stdout="hello\n", stderr=""
            )
            result = run(["echo", "hello"])

        assert result.returncode == 0
        assert result.stdout.strip() == "hello"

    def test_returns_nonzero_exit_code_without_raising(self):
        with patch("improve.process.subprocess.run") as mock_subproc:
            mock_subproc.return_value = subprocess.CompletedProcess(
                args=["false"], returncode=1, stdout="", stderr=""
            )
            result = run(["false"])

        assert result.returncode == 1

    def test_returns_exit_code_1_on_timeout(self):
        with patch("improve.process.subprocess.run") as mock_subproc:
            mock_subproc.side_effect = subprocess.TimeoutExpired(cmd=["sleep"], timeout=1)
            result = run(["sleep", "10"], timeout=1)

        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr == "Timed out"

    def test_returns_exit_code_1_when_command_not_found(self):
        with patch("improve.process.subprocess.run") as mock_subproc:
            mock_subproc.side_effect = FileNotFoundError("No such file")
            result = run(["nonexistent_binary_xyz_12345"])

        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr != ""

    @pytest.mark.parametrize("timeout,expected", [(None, 120), (42, 42)])
    def test_uses_expected_timeout(self, timeout, expected):
        with patch("improve.process.subprocess.run") as mock_subproc:
            mock_subproc.return_value = subprocess.CompletedProcess(
                args=["x"], returncode=0, stdout="", stderr=""
            )
            if timeout is None:
                run(["x"])
            else:
                run(["x"], timeout=timeout)

        assert mock_subproc.call_args[1]["timeout"] == expected

    def test_truncates_stderr_to_500_chars_in_debug_log(self, caplog):
        with (
            caplog.at_level(logging.DEBUG, logger="improve"),
            patch("improve.process.subprocess.run") as mock_subproc,
        ):
            mock_subproc.return_value = subprocess.CompletedProcess(
                args=["x"], returncode=1, stdout="", stderr="e" * 1000
            )
            run(["x"])

        logged_stderr = [r for r in caplog.records if "exit=" in r.message]
        assert logged_stderr
        assert len(logged_stderr[0].message) < 600


class TestProcessTracking:
    def test_terminates_every_tracked_process_with_its_own_tag(self):
        claude_proc, codex_proc = MagicMock(), MagicMock()

        with (
            patch.object(
                improve.process,
                "_active_processes",
                {claude_proc: "claude", codex_proc: "codex"},
            ),
            patch("improve.process.terminate") as mock_terminate,
        ):
            terminate_active()

        terminated = {call.args for call in mock_terminate.call_args_list}
        assert terminated == {(claude_proc, "claude"), (codex_proc, "codex")}

    def test_does_nothing_when_no_process_is_tracked(self):
        with (
            patch.object(improve.process, "_active_processes", {}),
            patch("improve.process.terminate") as mock_terminate,
        ):
            terminate_active()

        mock_terminate.assert_not_called()

    def test_an_untracked_process_is_no_longer_terminated(self):
        proc = MagicMock()

        with (
            patch.object(improve.process, "_active_processes", {}),
            patch("improve.process.terminate") as mock_terminate,
        ):
            track(proc, "claude")
            untrack(proc)
            terminate_active()

        mock_terminate.assert_not_called()

    def test_untracking_a_process_that_was_never_tracked_is_harmless(self):
        with patch.object(improve.process, "_active_processes", {}):
            untrack(MagicMock())


class TestAbortOnFirstFailure:
    def test_returns_quietly_when_every_future_succeeds(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(lambda: 1), pool.submit(lambda: 2)]

            assert abort_on_first_failure(futures) is None

    def test_raises_the_original_failure(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_raise, RuntimeError("usage limit"))

            with pytest.raises(RuntimeError, match="usage limit"):
                abort_on_first_failure([future])

    def test_terminates_the_other_agents_without_waiting_for_them(self):
        release = threading.Event()

        with (
            patch("improve.process.terminate_active", side_effect=release.set) as terminated,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(_raise, RuntimeError("died")), pool.submit(release.wait, 10)]

            with pytest.raises(RuntimeError, match="died"):
                abort_on_first_failure(futures)

        terminated.assert_called_once_with()

    def test_warns_that_the_other_agents_are_being_terminated(self, caplog):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_raise, RuntimeError("died"))

            with (
                patch("improve.process.terminate_active"),
                caplog.at_level(logging.WARNING, logger="improve"),
                pytest.raises(RuntimeError),
            ):
                abort_on_first_failure([future])

        assert caplog.messages == ["One parallel agent failed, terminating the others"]


class TestRequireTools:
    def test_raises_system_exit_1_when_tool_missing(self):
        with (
            patch("improve.process.shutil.which", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            require_tools(["git", "claude", "gh"])

        assert exc_info.value.code == 1

    def test_checks_every_tool_it_is_given(self):
        calls = []

        def tracking_which(tool):
            calls.append(tool)
            return f"/usr/bin/{tool}"

        with patch("improve.process.shutil.which", side_effect=tracking_which):
            require_tools(["git", "claude", "gh", "codex"])

        assert calls == ["git", "claude", "gh", "codex"]

    def test_passes_when_all_tools_found(self):
        with patch("improve.process.shutil.which", return_value="/usr/bin/git"):
            require_tools(["git", "claude", "gh"])

    def test_raises_system_exit_when_glab_missing(self):
        def selective_which(tool):
            if tool == "glab":
                return None
            return f"/usr/bin/{tool}"

        with (
            patch("improve.process.shutil.which", side_effect=selective_which),
            pytest.raises(SystemExit) as exc_info,
        ):
            require_tools(["git", "claude", "glab"])

        assert exc_info.value.code == 1

    def test_exits_when_codex_is_missing(self, caplog):
        def which(tool):
            return None if tool == "codex" else f"/usr/bin/{tool}"

        with (
            patch("improve.process.shutil.which", side_effect=which),
            caplog.at_level(logging.ERROR, logger="improve"),
            pytest.raises(SystemExit) as exc_info,
        ):
            require_tools(["git", "claude", "gh", "codex"])

        assert exc_info.value.code == 1
        assert caplog.messages == ["preflight] Missing required tools: codex"]


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0, "0.0s"),
            (1.0, "1.0s"),
            (30.5, "30.5s"),
            (59.0, "59.0s"),
            (59.9, "59.9s"),
            (60, "1m 0s"),
            (61, "1m 1s"),
            (125, "2m 5s"),
            (3599, "59m 59s"),
            (3600, "1h 0m 0s"),
            (3601, "1h 0m 1s"),
            (3725, "1h 2m 5s"),
            (7261, "2h 1m 1s"),
        ],
    )
    def test_formats_duration(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestCheckPreflight:
    def test_exits_with_code_1_on_command_failure(self):
        with (
            patch("improve.process.run") as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            )
            _check_preflight(["git", "status"], "preflight] Git failed")

        assert exc_info.value.code == 1

    def test_does_not_exit_on_successful_command(self):
        with patch("improve.process.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            _check_preflight(["git", "status"], "preflight] Git failed")


class TestRunPreflight:
    @pytest.mark.parametrize(
        ("ci_tool", "skip_ci", "expected_count"),
        [
            ("gh", False, 4),
            ("gh", True, 2),
        ],
    )
    def test_runs_expected_number_of_checks(self, ci_tool, skip_ci, expected_count):
        calls = []

        def fake_check(cmd, error_msg, *args):
            calls.append(cmd)

        with patch("improve.process._check_preflight", side_effect=fake_check):
            run_preflight("feature", ci_tool, skip_ci=skip_ci)

        assert len(calls) == expected_count

    @pytest.mark.parametrize(
        ("ci_tool", "has_json"),
        [
            ("gh", True),
            ("glab", False),
        ],
    )
    def test_repo_view_command_format(self, ci_tool, has_json):
        calls = []

        def fake_check(cmd, error_msg, *args):
            calls.append(cmd)

        with patch("improve.process._check_preflight", side_effect=fake_check):
            run_preflight("feat", ci_tool, skip_ci=False)

        repo_cmd = calls[-1]
        assert ci_tool in repo_cmd
        assert ("--json" in repo_cmd) == has_json

    def test_stops_at_first_failing_check(self):
        with (
            patch(
                "improve.process.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr=""
                ),
            ),
            pytest.raises(SystemExit, match="1"),
        ):
            run_preflight("feature-x", "gh", skip_ci=False)

    def test_passes_branch_to_push_check(self):
        calls = []

        def fake_check(cmd, error_msg, *args):
            calls.append(cmd)

        with patch("improve.process._check_preflight", side_effect=fake_check):
            run_preflight("my-feature", "gh", skip_ci=True)

        push_cmd = calls[1]
        assert "my-feature" in push_cmd


class TestTerminate:
    def test_skips_a_process_that_already_exited(self):
        proc = MagicMock()
        proc.poll.return_value = 0

        terminate(proc, "claude")

        proc.terminate.assert_not_called()

    def test_terminates_a_running_process(self):
        proc = MagicMock()
        proc.poll.return_value = None

        terminate(proc, "claude")

        proc.terminate.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5)
        proc.kill.assert_not_called()

    def test_kills_the_process_when_it_ignores_terminate(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), None]

        terminate(proc, "claude")

        proc.kill.assert_called_once()
        assert proc.wait.call_count == 2

    def test_still_kills_the_process_when_terminate_raises(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.terminate.side_effect = OSError("gone")
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), None]

        terminate(proc, "claude")

        proc.kill.assert_called_once()

    def test_logs_which_agent_is_being_terminated(self, caplog):
        proc = MagicMock()
        proc.poll.return_value = None

        with caplog.at_level(logging.INFO, logger="improve"):
            terminate(proc, "codex")

        assert caplog.messages == ["codex] Terminating subprocess..."]
