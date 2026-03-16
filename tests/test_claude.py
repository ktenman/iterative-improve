import json
import logging
from unittest.mock import MagicMock, call, patch

import pytest

import improve.claude
from improve.claude import _summarize_tool_input, run_claude, set_timeout


def _make_process(stdout_lines, returncode=0, stderr=""):
    proc = MagicMock()
    proc.stdout = iter(stdout_lines)
    proc.stderr = iter(stderr.splitlines(keepends=True)) if stderr else iter([])
    proc.returncode = returncode
    return proc


def _text_delta(text):
    event = {"type": "stream_event", "event": {"delta": {"type": "text_delta", "text": text}}}
    return json.dumps(event) + "\n"


def _tool_start(name):
    block = {"type": "tool_use", "name": name}
    inner = {"type": "content_block_start", "content_block": block}
    return json.dumps({"type": "stream_event", "event": inner}) + "\n"


def _tool_input(partial_json):
    delta = {"type": "input_json_delta", "partial_json": partial_json}
    event = {"type": "stream_event", "event": {"delta": delta}}
    return json.dumps(event) + "\n"


def _tool_stop():
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_stop"}}) + "\n"


def _result(text):
    return json.dumps({"type": "result", "result": text}) + "\n"


class TestSetTimeout:
    def test_updates_global_timeout(self, monkeypatch):
        monkeypatch.setattr(improve.claude, "CLAUDE_TIMEOUT", 0)

        set_timeout(300)

        assert improve.claude.CLAUDE_TIMEOUT == 300


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


class TestRunClaude:
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

    def test_logs_tool_use_with_summarized_input(self, caplog):
        lines = [
            _tool_start("Bash"),
            _tool_input('{"command":'),
            _tool_input(' "ls"}'),
            _tool_stop(),
            _result(""),
        ]
        proc = _make_process(lines)
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            run_claude("prompt")

        assert "Bash > ls" in caplog.text

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

    def test_logs_stderr_on_nonzero_return_code(self, caplog):
        proc = _make_process([], returncode=1, stderr="something broke")
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            run_claude("prompt")

        assert "something broke" in caplog.text

    def test_does_not_log_stderr_on_success(self, caplog):
        proc = _make_process([_result("")], returncode=0, stderr="")
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            caplog.at_level(logging.WARNING, logger="improve"),
        ):
            run_claude("prompt")

        assert "stderr" not in caplog.text

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

    def test_does_not_terminate_process_during_normal_run(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt")

        proc.terminate.assert_not_called()

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

    def test_defaults_cwd_to_none(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt")

        assert mock_popen.call_args[1]["cwd"] is None

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

    def test_still_returns_result_in_quiet_mode(self):
        proc = _make_process([_text_delta("Hi"), _result("Final")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
        ):
            text, _ = run_claude("prompt", quiet=True)

        assert text == "Final"
