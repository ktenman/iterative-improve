import json
import logging
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import improve.codex
import improve.process
from improve.codex import (
    CodexEvents,
    CodexReply,
    check_model,
    parse_events,
    run_codex,
)
from tests.conftest import _test_config

SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}}
API_ERROR = json.dumps(
    {"type": "error", "status": 400, "error": {"message": "The x model is not supported"}}
)
THREAD_STARTED = json.dumps({"type": "thread.started", "thread_id": "thread-1"})
TURN_COMPLETED = json.dumps(
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}
)
TURN_FAILED = json.dumps({"type": "turn.failed", "error": {"message": API_ERROR}})


def _lines(*events: str) -> str:
    return "\n".join(events) + "\n"


def _fake_popen(stdout="", stderr="", returncode=0, reply='{"findings": []}', error=None):
    calls = []

    def popen(command, **kwargs):
        proc = MagicMock()
        proc.__enter__.return_value = proc
        proc.__exit__.return_value = False
        proc.returncode = returncode

        def communicate(prompt, timeout=None):
            call = {"command": command, "prompt": prompt, "timeout": timeout, "kwargs": kwargs}
            if "--output-schema" in command:
                schema_file = Path(command[command.index("--output-schema") + 1])
                call["schema"] = json.loads(schema_file.read_text())
            calls.append(call)
            if error is not None:
                raise error
            if reply is not None and "-o" in command:
                Path(command[command.index("-o") + 1]).write_text(reply)
            return stdout, stderr

        proc.communicate.side_effect = communicate
        return proc

    return popen, calls


def _run(popen, thread="", config=None, prompt="Review this"):
    with patch("improve.codex.subprocess.Popen", side_effect=popen):
        return run_codex(prompt, SCHEMA, config or _test_config(), thread=thread)


def _check(popen, config=None):
    with patch("improve.codex.subprocess.Popen", side_effect=popen):
        return check_model(config or _test_config())


class TestRunCodex:
    def test_returns_the_reply_file_as_data_with_the_thread_and_elapsed_time(self):
        popen, _ = _fake_popen(
            stdout=_lines(THREAD_STARTED, TURN_COMPLETED), reply='{"findings": [{"title": "t"}]}'
        )

        with patch("improve.codex.time.monotonic", side_effect=[10.0, 12.5]):
            reply = _run(popen)

        assert reply == CodexReply({"findings": [{"title": "t"}]}, "thread-1", 2.5)

    def test_logs_the_start_and_the_token_usage(self, caplog):
        popen, _ = _fake_popen(stdout=_lines(THREAD_STARTED, TURN_COMPLETED))

        with (
            patch("improve.codex.time.monotonic", side_effect=[10.0, 12.5]),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            _run(popen)

        assert caplog.messages == ["codex] Running...", "codex] Done in 2.5s (120 tokens)"]

    def test_starts_a_new_read_only_session_with_the_model_effort_and_project_rules(self):
        popen, calls = _fake_popen(stdout=_lines(THREAD_STARTED))
        config = replace(_test_config(), effort="medium", codex_model="some-model")

        _run(popen, config=config)

        command = calls[0]["command"]
        schema_file = command[command.index("--output-schema") + 1]
        reply_file = command[command.index("-o") + 1]
        assert command == [
            "codex",
            "exec",
            "--json",
            "-s",
            "read-only",
            "-m",
            "some-model",
            "-c",
            "model_reasoning_effort=medium",
            "-c",
            'project_doc_fallback_filenames=["CLAUDE.md"]',
            "--output-schema",
            schema_file,
            "-o",
            reply_file,
            "-",
        ]
        assert Path(schema_file).name == "schema.json"
        assert Path(reply_file).name == "reply.json"

    def test_resumes_the_thread_with_the_read_only_sandbox_setting(self, caplog):
        popen, calls = _fake_popen()

        with caplog.at_level(logging.INFO, logger="improve"):
            reply = _run(popen, thread="thread-9")

        command = calls[0]["command"]
        assert command[:6] == ["codex", "exec", "resume", "thread-9", "--json", "-c"]
        assert command[6] == "sandbox_mode=read-only"
        assert "-s" not in command
        assert reply.thread == "thread-9"
        assert caplog.messages[0] == "codex] Resuming..."

    def test_keeps_the_reply_files_outside_the_repository(self):
        popen, calls = _fake_popen()

        _run(popen)

        command = calls[0]["command"]
        reply_file = Path(command[command.index("-o") + 1])
        assert reply_file.parent.name.startswith("improve-codex-")
        assert not reply_file.exists()

    def test_sends_the_prompt_on_stdin_and_applies_the_phase_timeout(self):
        popen, calls = _fake_popen()

        _run(popen, config=replace(_test_config(), agent_timeout=123), prompt="Review this")

        assert calls[0]["prompt"] == "Review this"
        assert calls[0]["timeout"] == 123
        assert calls[0]["kwargs"] == {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
        }

    def test_passes_the_schema_in_the_output_schema_file(self):
        popen, calls = _fake_popen()

        _run(popen)

        assert calls[0]["schema"] == SCHEMA

    def test_raises_with_the_api_message_when_the_turn_fails(self):
        popen, _ = _fake_popen(stdout=_lines(TURN_FAILED), returncode=1, reply=None)

        with pytest.raises(RuntimeError, match=r"^Codex failed: The x model is not supported$"):
            _run(popen)

    def test_raises_when_the_turn_fails_even_if_codex_exits_cleanly(self):
        popen, _ = _fake_popen(stdout=_lines(TURN_FAILED))

        with pytest.raises(RuntimeError, match="The x model is not supported"):
            _run(popen)

    def test_raises_with_stderr_when_codex_exits_without_an_error_event(self):
        popen, _ = _fake_popen(stderr="  Not inside a trusted directory\n", returncode=1)

        with pytest.raises(RuntimeError, match=r"^Codex failed: Not inside a trusted directory$"):
            _run(popen)

    def test_raises_with_the_exit_code_when_nothing_explains_the_failure(self):
        popen, _ = _fake_popen(returncode=2, reply=None)

        with pytest.raises(RuntimeError, match=r"^Codex failed: exit code 2$"):
            _run(popen)

    def test_shortens_a_long_failure_message(self):
        popen, _ = _fake_popen(stderr="x" * 500, returncode=1)

        with pytest.raises(RuntimeError) as exc_info:
            _run(popen)

        assert str(exc_info.value) == "Codex failed: " + "x" * 300

    @pytest.mark.parametrize("reply", [None, "", "not json", "[1, 2]", "{}"])
    def test_raises_when_the_reply_is_not_a_json_object(self, reply):
        popen, _ = _fake_popen(reply=reply)

        with pytest.raises(RuntimeError, match=r"^Codex returned no structured output"):
            _run(popen)

    def test_reports_the_error_event_when_codex_exits_cleanly_without_a_reply(self):
        disconnected = json.dumps({"type": "error", "message": "Reconnecting... 5/5 gave up"})
        popen, _ = _fake_popen(stdout=_lines(THREAD_STARTED, disconnected), reply=None)

        with pytest.raises(RuntimeError) as exc_info:
            _run(popen)

        assert str(exc_info.value) == (
            "Codex returned no structured output: Reconnecting... 5/5 gave up"
        )

    def test_reports_stderr_when_codex_exits_cleanly_without_a_reply_or_an_error_event(self):
        popen, _ = _fake_popen(stderr="  codex: nothing to do\n", reply=None)

        with pytest.raises(RuntimeError) as exc_info:
            _run(popen)

        assert str(exc_info.value) == "Codex returned no structured output: codex: nothing to do"

    def test_an_error_event_without_a_failed_turn_does_not_fail_the_call(self):
        reconnecting = json.dumps({"type": "error", "message": "Reconnecting... 1/5"})
        popen, _ = _fake_popen(stdout=_lines(THREAD_STARTED, reconnecting, TURN_COMPLETED))

        reply = _run(popen)

        assert reply.data == {"findings": []}

    def test_terminates_codex_and_raises_when_it_times_out(self, caplog):
        popen, _ = _fake_popen(error=subprocess.TimeoutExpired("codex", 900))

        with (
            patch("improve.codex.terminate") as mock_terminate,
            caplog.at_level(logging.WARNING, logger="improve"),
            pytest.raises(RuntimeError, match=r"^Codex timed out after 900s$"),
        ):
            _run(popen)

        assert mock_terminate.call_args[0][1] == "codex"
        assert caplog.messages == ["codex] Timeout after 900s, terminating"]

    def test_forgets_the_process_after_it_finishes(self):
        popen, _ = _fake_popen()

        _run(popen)

        assert not improve.process._active_processes

    def test_forgets_the_process_after_a_timeout(self):
        popen, _ = _fake_popen(error=subprocess.TimeoutExpired("codex", 900))

        with patch("improve.codex.terminate"), pytest.raises(RuntimeError):
            _run(popen)

        assert not improve.process._active_processes

    def test_tracks_the_process_while_codex_runs(self):
        seen = []
        popen, _ = _fake_popen()

        def tracking_popen(command, **kwargs):
            proc = popen(command, **kwargs)
            original = proc.communicate.side_effect

            def communicate(prompt, timeout=None):
                seen.append(proc in improve.process._active_processes)
                return original(prompt, timeout=timeout)

            proc.communicate.side_effect = communicate
            return proc

        _run(tracking_popen)

        assert seen == [True]


class TestParseEvents:
    def test_reads_the_thread_and_sums_token_usage_across_turns(self):
        events = parse_events(_lines(THREAD_STARTED, TURN_COMPLETED, TURN_COMPLETED))

        assert events == CodexEvents(thread="thread-1", tokens=240, error="", failed=False)

    def test_ignores_blank_and_non_json_lines(self):
        events = parse_events("\nnot json\n[1]\n" + THREAD_STARTED + "\n")

        assert events == CodexEvents(thread="thread-1", tokens=0, error="", failed=False)

    def test_keeps_an_error_message_that_is_not_json(self):
        events = parse_events(_lines(json.dumps({"type": "error", "message": "stream closed"})))

        assert events == CodexEvents(thread="", tokens=0, error="stream closed", failed=False)

    def test_extracts_the_api_message_from_a_failed_turn(self):
        events = parse_events(_lines(TURN_FAILED))

        assert events == CodexEvents(
            thread="", tokens=0, error="The x model is not supported", failed=True
        )

    def test_keeps_an_api_payload_without_a_message_as_it_is(self):
        payload = json.dumps({"error": {"code": 400}})
        events = parse_events(_lines(json.dumps({"type": "error", "message": payload})))

        assert events.error == payload

    def test_describes_a_failed_turn_without_details(self):
        events = parse_events(_lines(json.dumps({"type": "turn.failed"})))

        assert events == CodexEvents(thread="", tokens=0, error="unknown error", failed=True)

    def test_reads_a_failed_turn_whose_error_is_plain_text(self):
        events = parse_events(_lines(json.dumps({"type": "turn.failed", "error": "overloaded"})))

        assert events.error == "overloaded"

    def test_counts_missing_usage_as_zero(self):
        events = parse_events(_lines(json.dumps({"type": "turn.completed"})))

        assert events.tokens == 0

    def test_counts_a_non_dict_usage_as_zero(self):
        events = parse_events(_lines(json.dumps({"type": "turn.completed", "usage": ["x"]})))

        assert events.tokens == 0

    def test_counts_a_non_numeric_token_as_zero_while_the_valid_one_still_counts(self):
        usage = {"input_tokens": "n/a", "output_tokens": 7}
        events = parse_events(_lines(json.dumps({"type": "turn.completed", "usage": usage})))

        assert events.tokens == 7

    def test_ignores_a_thread_event_without_an_id(self):
        events = parse_events(_lines(json.dumps({"type": "thread.started"})))

        assert events.thread == ""


class TestCheckModel:
    def test_returns_an_empty_string_when_the_model_answers(self):
        popen, _ = _fake_popen(stdout=_lines(THREAD_STARTED, TURN_COMPLETED), reply=None)

        assert _check(popen) == ""

    def test_sends_one_ephemeral_read_only_prompt_with_the_configured_model_and_effort(self):
        popen, calls = _fake_popen(reply=None)

        _check(popen, replace(_test_config(), effort="medium", codex_model="some-model"))

        assert calls[0]["command"] == [
            "codex",
            "exec",
            "--json",
            "--ephemeral",
            "-s",
            "read-only",
            "-m",
            "some-model",
            "-c",
            "model_reasoning_effort=medium",
            "-",
        ]
        assert calls[0]["prompt"] == "Reply with the word OK."
        assert calls[0]["timeout"] == 60

    def test_returns_the_api_message_when_the_model_is_rejected(self):
        popen, _ = _fake_popen(stdout=_lines(TURN_FAILED), returncode=1, reply=None)

        assert _check(popen) == "The x model is not supported"

    def test_rejects_a_failed_turn_even_when_codex_exits_cleanly(self):
        popen, _ = _fake_popen(stdout=_lines(TURN_FAILED), reply=None)

        assert _check(popen) == "The x model is not supported"

    def test_falls_back_to_stderr(self):
        popen, _ = _fake_popen(stderr="  login required\n", returncode=1, reply=None)

        assert _check(popen) == "login required"

    def test_falls_back_to_the_exit_code(self):
        popen, _ = _fake_popen(returncode=3, reply=None)

        assert _check(popen) == "codex exited with code 3"

    def test_reports_a_timeout(self):
        popen, _ = _fake_popen(error=subprocess.TimeoutExpired("codex", 60), reply=None)

        with patch("improve.codex.terminate"):
            assert _check(popen) == "Codex timed out after 60s"

    def test_reports_a_codex_binary_that_cannot_start(self):
        with patch("improve.codex.subprocess.Popen", side_effect=FileNotFoundError("no codex")):
            assert check_model(_test_config()) == "no codex"
