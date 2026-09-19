import json
import logging
from dataclasses import replace
from itertools import pairwise
from unittest.mock import MagicMock, call, patch

import pytest

import improve.claude
import improve.process
from improve.claude import (
    WRITE_TOOLS,
    ClaudeResult,
    Result,
    TextDelta,
    ToolInput,
    ToolStart,
    ToolStop,
    _classify_events,
    _summarize_tool_input,
    ask_claude,
    run_claude,
)
from tests.conftest import _test_config

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}}


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


def _structured_result(data: object) -> str:
    event = {"type": "result", "result": json.dumps(data), "structured_output": data}
    return json.dumps(event) + "\n"


def _ask(lines: list[str], session="abc", resume=False, config=None, returncode=0):
    proc = _make_process(lines, returncode=returncode)
    with (
        patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
        patch("improve.claude.threading.Timer"),
    ):
        result = ask_claude("prompt", SCHEMA, session, resume, config or _test_config())
    return result, mock_popen.call_args[0][0]


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

    def test_raises_with_the_error_reported_by_claude_when_stderr_is_empty(self):
        event = json.dumps(
            {
                "type": "result",
                "subtype": "error_max_turns",
                "is_error": True,
                "errors": ["Reached maximum number of turns (50)"],
            }
        )
        proc = _make_process([event + "\n"], returncode=1)
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            pytest.raises(RuntimeError, match="Reached maximum number of turns"),
        ):
            run_claude("prompt")

    def test_raises_when_the_result_is_flagged_as_an_error_even_if_it_has_text(self):
        event = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "There's an issue with the selected model (opus[1m]).",
            }
        )
        proc = _make_process([event + "\n"], returncode=1)
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc),
            patch("improve.claude.threading.Timer"),
            pytest.raises(RuntimeError, match="issue with the selected model"),
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
            run_claude("prompt")

        assert not improve.process._active_processes

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

        with patch("improve.claude.terminate") as mock_terminate:
            timeout_callback()

        mock_terminate.assert_called_once_with(proc, "claude")

    def test_passes_cwd_to_popen(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt", cwd="/some/path")

        assert mock_popen.call_args[1]["cwd"] == "/some/path"

    def test_does_not_cap_turns_so_a_long_phase_runs_to_completion(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt")

        assert "--max-turns" not in mock_popen.call_args[0][0]

    def test_passes_verbose_because_print_mode_rejects_stream_json_without_it(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt")

        assert "--verbose" in mock_popen.call_args[0][0]

    @pytest.mark.parametrize("flag, value", [("--model", "opus[1m]"), ("--effort", "max")])
    def test_always_starts_claude_with_the_pinned_model_and_effort(self, flag, value):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt")

        assert (flag, value) in pairwise(mock_popen.call_args[0][0])

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
        config = Config(agent_timeout=42, ci_timeout=60)
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

    def test_starts_claude_with_the_configured_effort(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt", config=replace(_test_config(), effort="medium"))

        assert ("--effort", "medium") in pairwise(mock_popen.call_args[0][0])

    def test_keeps_skipping_permissions_so_it_can_edit_files(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt")

        argv = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in argv
        assert "--permission-mode" not in argv

    def test_leaves_tools_and_output_unrestricted(self):
        proc = _make_process([_result("")])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            run_claude("prompt")

        argv = mock_popen.call_args[0][0]
        assert "--disallowedTools" not in argv
        assert "--json-schema" not in argv
        assert "--session-id" not in argv


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

    def test_yields_result_carrying_the_errors_reported_by_the_cli(self):
        line = json.dumps({"type": "result", "errors": ["out of turns", "gave up"]})
        events = list(_classify_events(iter([line + "\n"])))

        assert events[0].error == "out of turns; gave up"

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

    def test_result_carries_the_structured_output(self):
        line = json.dumps({"type": "result", "result": "{}", "structured_output": {"a": 1}})

        events = list(_classify_events(iter([line + "\n"])))

        assert events == [Result("{}", "", {"a": 1})]


class TestAskClaude:
    def test_returns_the_structured_output_and_elapsed_time(self):
        (data, elapsed), _ = _ask([_structured_result({"findings": []})])

        assert data == {"findings": []}
        assert isinstance(elapsed, float)

    def test_starts_a_new_session_with_the_given_id(self):
        _, argv = _ask([_structured_result({})], session="abc")

        assert ("--session-id", "abc") in pairwise(argv)
        assert "--resume" not in argv

    def test_resumes_the_given_session(self):
        _, argv = _ask([_structured_result({})], session="abc", resume=True)

        assert ("--resume", "abc") in pairwise(argv)
        assert "--session-id" not in argv

    def test_reviews_in_plan_mode_instead_of_skipping_permissions(self):
        _, argv = _ask([_structured_result({})])

        assert ("--permission-mode", "plan") in pairwise(argv)
        assert ("--permission-prompts", "none") in pairwise(argv)
        assert "--dangerously-skip-permissions" not in argv

    def test_turns_off_the_tools_that_edit_files(self):
        _, argv = _ask([_structured_result({})])

        assert WRITE_TOOLS == "Edit,Write,NotebookEdit"
        assert ("--disallowedTools", "Edit,Write,NotebookEdit") in pairwise(argv)

    def test_passes_the_schema_as_json(self):
        _, argv = _ask([_structured_result({})])

        assert ("--json-schema", json.dumps(SCHEMA)) in pairwise(argv)

    def test_keeps_the_pinned_model_and_uses_the_configured_effort(self):
        _, argv = _ask([_structured_result({})], config=replace(_test_config(), effort="medium"))

        assert ("--model", "opus[1m]") in pairwise(argv)
        assert ("--effort", "medium") in pairwise(argv)

    def test_runs_in_the_current_directory(self):
        proc = _make_process([_structured_result({})])
        with (
            patch("improve.claude.subprocess.Popen", return_value=proc) as mock_popen,
            patch("improve.claude.threading.Timer"),
        ):
            ask_claude("prompt", SCHEMA, "abc", False, _test_config())

        assert mock_popen.call_args[1]["cwd"] is None

    def test_raises_when_the_result_has_no_structured_output(self):
        with pytest.raises(RuntimeError, match="no structured output: plain text"):
            _ask([_result("plain text")])

    def test_raises_when_the_structured_output_is_not_an_object(self):
        with pytest.raises(RuntimeError, match="no structured output"):
            _ask([_structured_result([1, 2])])

    def test_raises_with_claudes_error_when_the_call_fails(self):
        failure = {"type": "result", "is_error": True, "result": "Usage limit reached"}

        with pytest.raises(RuntimeError, match="Usage limit reached"):
            _ask([json.dumps(failure) + "\n"], returncode=1)
