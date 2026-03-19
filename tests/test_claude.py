import json
import logging
from unittest.mock import MagicMock, call, patch

import pytest

import improve.claude
from improve.claude import (
    ClaudeResult,
    Result,
    TextDelta,
    ToolInput,
    ToolStart,
    ToolStop,
    _classify_events,
    _summarize_tool_input,
    run_claude,
)


def _make_process(stdout_lines: list[str], returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.stdout = iter(stdout_lines)
    proc.stderr = iter(stderr.splitlines(keepends=True)) if stderr else iter([])
    proc.returncode = returncode
    return proc


def _text_delta(text: str) -> str:
    event = {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": text}}}
    return json.dumps(event) + "\n"


def _tool_start(name: str) -> str:
    block = {"type": "tool_use", "name": name}
    inner = {"type": "content_block_start", "content_block": block}
    return json.dumps({"type": "stream_event", "event": inner}) + "\n"


def _tool_input(partial_json: str) -> str:
    delta = {"type": "input_json_delta", "partial_json": partial_json}
    event = {"type": "stream_event", "event": {"delta": delta}}
    return json.dumps(event) + "\n"


def _tool_stop() -> str:
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_stop"}}) + "\n"


def _result(text: str) -> str:
    return json.dumps({"type": "result", "result": text}) + "\n"


class TestTerminateProcess:
    def test_skips_already_exited_process(self):
        proc = MagicMock()
        proc.poll.return_value = 0

        improve.claude._terminate_process(proc)

        proc.terminate.assert_not_called()

    def test_terminates_running_process(self):
        proc = MagicMock()
        proc.poll.return_value = None

        improve.claude._terminate_process(proc)

        proc.terminate.assert_called_once()

    def test_kills_process_when_terminate_times_out(self):
        import subprocess

        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 5), None]

        improve.claude._terminate_process(proc)

        proc.kill.assert_called_once()


class TestTerminateActive:
    def test_terminates_all_active_processes(self):
        proc1 = MagicMock()
        proc1.poll.return_value = None
        proc2 = MagicMock()
        proc2.poll.return_value = None

        with patch.object(improve.claude, "_active_processes", {proc1, proc2}):
            improve.claude.terminate_active()

        proc1.terminate.assert_called_once()
        proc2.terminate.assert_called_once()

    def test_does_nothing_when_no_active_processes(self):
        with patch.object(improve.claude, "_active_processes", set()):
            improve.claude.terminate_active()


class TestSummarizeToolInput:
    @pytest.mark.parametrize(
        "tool, raw_json, expected",
        [
            ("Unknown", '{"key": "val"}', "Unknown"),
            ("Bash", "", "Bash"),
            ("Bash", '{"command": "ls -la"}', "Bash > ls -la"),
            ("Read", '{"file_path": "/tmp/test.py"}', "Read > /tmp/test.py"),
            ("Bash", "not json", "Bash"),
            ("Bash", '{"command": ""}', "Bash"),
            ("Glob", '{"pattern": "**/*.py"}', "Glob > **/*.py"),
            ("Skill", '{"skill": "commit"}', "Skill > commit"),
        ],
    )
    def test_summarizes_tool_input(self, tool, raw_json, expected):
        result = _summarize_tool_input(tool, raw_json)

        assert result == expected

    def test_truncates_long_values_at_80_chars(self):
        result = _summarize_tool_input("Bash", '{"command": "' + "a" * 100 + '"}')

        assert result.endswith("...")
        assert len(result.split(" > ")[1]) == 83

    def test_does_not_truncate_value_at_exactly_80_chars(self):
        result = _summarize_tool_input("Bash", '{"command": "' + "a" * 80 + '"}')

        assert not result.endswith("...")
        assert len(result.split(" > ")[1]) == 80

    def test_truncates_value_at_81_chars(self):
        result = _summarize_tool_input("Bash", '{"command": "' + "a" * 81 + '"}')

        assert result.endswith("...")
        assert len(result.split(" > ")[1]) == 83


class TestRunClaude:
    def test_returns_claude_result_named_tuple(self):
        proc = _make_process([_result("output")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            result = run_claude("prompt")

        assert isinstance(result, ClaudeResult)
        assert result.text == "output"
        assert isinstance(result.elapsed, float)

    def test_returns_result_text_from_result_event(self):
        proc = _make_process([_result("Final output")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            text, elapsed = run_claude("test prompt")

        assert text == "Final output"
        assert isinstance(elapsed, float)

    def test_writes_prompt_to_stdin_and_closes(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("hello world")

        proc.stdin.write.assert_called_once_with("hello world")
        proc.stdin.close.assert_called_once()

    def test_streams_text_delta_to_stdout(self):
        proc = _make_process([_text_delta("Hello"), _result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            patch("improve.claude.sys.stdout") as mock_stdout,
        ):
            run_claude("prompt")

        mock_stdout.write.assert_any_call("Hello")

    def test_adds_trailing_newline_after_streamed_text(self):
        proc = _make_process([_text_delta("Hi"), _result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            patch("improve.claude.sys.stdout") as mock_stdout,
        ):
            run_claude("prompt")

        mock_stdout.write.assert_called_with("\n")

    def test_adds_newline_before_tool_use_when_text_was_streamed(self):
        proc = _make_process([_text_delta("Hi"), _tool_start("Bash"), _tool_stop(), _result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            patch("improve.claude.sys.stdout") as mock_stdout,
        ):
            run_claude("prompt")

        mock_stdout.write.assert_has_calls([call("Hi"), call("\n")])

    def test_skips_blank_lines(self):
        proc = _make_process(["\n", "   \n", _result("ok")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            text, _ = run_claude("prompt")

        assert text == "ok"

    def test_handles_unparseable_json_lines(self):
        proc = _make_process(["not valid json\n", _result("ok")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            text, _ = run_claude("prompt")

        assert text == "ok"

    def test_skips_non_dict_json_values_without_crashing(self):
        proc = _make_process(["null\n", "123\n", '"hello"\n', "[1,2]\n", _result("ok")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            text, _ = run_claude("prompt")

        assert text == "ok"

    def test_raises_on_nonzero_return_code_with_no_result(self):
        proc = _make_process([], returncode=1, stderr="something broke")
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            pytest.raises(RuntimeError, match="something broke"),
        ):
            run_claude("prompt")

    def test_returns_result_on_nonzero_return_code_when_result_present(self, caplog):
        proc = _make_process([_result("partial output")], returncode=1, stderr="warning")
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            text, _ = run_claude("prompt")

        assert text == "partial output"
        assert "warning" in caplog.text

    def test_clears_active_process_after_completion(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            import improve.claude

            run_claude("prompt")

        assert not improve.claude._active_processes

    def test_cancels_timer_after_completion(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer") as MockTimer,
        ):
            run_claude("prompt")

        MockTimer.return_value.cancel.assert_called_once()

    def test_timeout_callback_terminates_only_its_own_process(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer") as MockTimer,
        ):
            run_claude("prompt")
            timeout_callback = MockTimer.call_args[0][1]

        with patch("improve.claude._terminate_process") as mock_terminate:
            timeout_callback()

        mock_terminate.assert_called_once_with(proc)

    def test_passes_cwd_to_popen(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt", cwd="/some/path")

        assert mock_popen.call_args[1]["cwd"] == "/some/path"

    def test_suppresses_stdout_in_quiet_mode(self):
        proc = _make_process([_text_delta("Hello"), _result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            patch("improve.claude.sys.stdout") as mock_stdout,
        ):
            run_claude("prompt", quiet=True)

        for call_args in mock_stdout.write.call_args_list:
            assert call_args[0][0] != "Hello"

    def test_logs_stderr_truncated_to_300_chars_on_nonzero_exit(self, caplog):
        long_stderr = "e" * 500
        proc = _make_process([_result("partial")], returncode=1, stderr=long_stderr)
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            text, _ = run_claude("prompt")

        assert text == "partial"
        stderr_records = [r for r in caplog.records if "stderr" in r.message]
        assert stderr_records
        assert len(stderr_records[0].message) < 400

    def test_raises_runtime_error_with_stderr_on_failure_no_result(self):
        proc = _make_process([], returncode=2, stderr="specific error msg")
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            pytest.raises(RuntimeError, match="specific error msg"),
        ):
            run_claude("prompt")

    def test_does_not_raise_when_nonzero_exit_but_has_result(self):
        proc = _make_process([_result("got result")], returncode=1)
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            text, _ = run_claude("prompt")

        assert text == "got result"

    def test_uses_config_timeout_when_provided(self):
        from improve.config import Config

        proc = _make_process([_result("")])
        config = Config(claude_timeout=42, ci_timeout=60)
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer") as MockTimer,
        ):
            run_claude("prompt", config=config)

        assert MockTimer.call_args[0][0] == 42

    def test_uses_default_timeout_when_no_config(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer") as MockTimer,
        ):
            run_claude("prompt")

        assert MockTimer.call_args[0][0] == 900

    def test_handles_stdin_write_oserror_gracefully(self, caplog):
        proc = _make_process([_result("ok")])
        proc.stdin.write.side_effect = OSError("broken pipe")
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            text, _ = run_claude("prompt")

        assert text == "ok"
        assert "exited before accepting input" in caplog.text

    def test_still_returns_result_in_quiet_mode(self):
        proc = _make_process([_text_delta("Hi"), _result("Final")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            text, _ = run_claude("prompt", quiet=True)

        assert text == "Final"


class TestClassifyEvents:
    def test_yields_text_delta(self):
        line = json.dumps(
            {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": "hi"}}}
        )
        events = list(_classify_events(iter([line + "\n"])))

        assert len(events) == 1
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "hi"

    def test_yields_result(self):
        line = json.dumps({"type": "result", "result": "output"})
        events = list(_classify_events(iter([line + "\n"])))

        assert isinstance(events[0], Result)
        assert events[0].text == "output"

    def test_yields_tool_lifecycle(self):
        lines = [
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": "Bash"},
                    },
                }
            )
            + "\n",
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {"delta": {"type": "input_json_delta", "partial_json": '{"cmd":'}},
                }
            )
            + "\n",
            json.dumps({"type": "stream_event", "event": {"type": "content_block_stop"}}) + "\n",
        ]
        events = list(_classify_events(iter(lines)))

        assert isinstance(events[0], ToolStart)
        assert events[0].name == "Bash"
        assert isinstance(events[1], ToolInput)
        assert isinstance(events[2], ToolStop)

    def test_skips_blank_and_unparseable_lines(self):
        events = list(_classify_events(iter(["\n", "not json\n", "null\n"])))

        assert events == []

    def test_skips_non_stream_events(self):
        line = json.dumps({"type": "other", "data": "ignored"}) + "\n"
        events = list(_classify_events(iter([line])))

        assert events == []
