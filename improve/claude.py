from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import NamedTuple

from improve.config import DEFAULT_EFFORT, Config
from improve.process import format_duration, terminate, track, untrack

logger = logging.getLogger("improve")

WRITE_TOOLS = "Edit,Write,NotebookEdit"
SKIP_PERMISSIONS = ["--dangerously-skip-permissions"]
READ_ONLY = ["--permission-mode", "plan", "--permission-prompts", "none"]


class ClaudeResult(NamedTuple):
    text: str
    elapsed: float


TOOL_SUMMARY_KEYS = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Agent": "description",
    "Skill": "skill",
}


def _summarize_tool_input(tool: str, raw_json: str) -> str:
    key = TOOL_SUMMARY_KEYS.get(tool)
    if not key or not raw_json:
        return tool
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return tool
    value = data.get(key, "")
    if not value:
        return tool
    truncated = (value[:80] + "...") if len(value) > 80 else value
    return f"{tool} > {truncated}"


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolStart:
    name: str


@dataclass
class ToolInput:
    partial_json: str


@dataclass
class ToolStop:
    pass


@dataclass
class Result:
    text: str
    error: str = ""
    data: object = None


def _classify_events(
    stdout: Iterator[str],
) -> Iterator[TextDelta | ToolStart | ToolInput | ToolStop | Result]:
    for line in stdout:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("claude] unparseable line")
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type", "")
        if event_type == "result":
            text = event.get("result", "")
            errors = "; ".join(str(e) for e in event.get("errors") or [])
            if event.get("is_error"):
                yield Result("", errors or text)
                continue
            yield Result(text, errors, event.get("structured_output"))
            continue
        if event_type != "stream_event":
            continue

        inner = event.get("event") or {}
        delta = inner.get("delta") or {}
        inner_type = inner.get("type", "")
        delta_type = delta.get("type")

        if delta_type == "text_delta":
            yield TextDelta(delta.get("text", ""))
        elif delta_type == "input_json_delta":
            yield ToolInput(delta.get("partial_json", ""))
        elif inner_type == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "tool_use":
                yield ToolStart(block.get("name", "?"))
        elif inner_type == "content_block_stop":
            yield ToolStop()


def _parse_stream(stdout: Iterator[str], quiet: bool = False) -> tuple[Result, bool]:
    result = Result("")
    has_streamed = False
    current_tool = ""
    tool_input_chunks: list[str] = []

    for event in _classify_events(stdout):
        if isinstance(event, Result):
            result = event
        elif isinstance(event, TextDelta):
            if not quiet:
                sys.stdout.write(event.text)
                sys.stdout.flush()
                has_streamed = True
        elif isinstance(event, ToolStart):
            if has_streamed:
                sys.stdout.write("\n")
                has_streamed = False
            current_tool = event.name
            tool_input_chunks = []
        elif isinstance(event, ToolInput):
            tool_input_chunks.append(event.partial_json)
        elif isinstance(event, ToolStop) and current_tool:
            detail = _summarize_tool_input(current_tool, "".join(tool_input_chunks))
            logger.info("claude] %s", detail)
            current_tool = ""

    return result, has_streamed


def _start_claude(prompt: str, cwd: str | None, extra_args: list[str]) -> subprocess.Popen:
    process = subprocess.Popen(
        [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--model",
            "opus[1m]",
            *extra_args,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    track(process, "claude")
    try:
        process.stdin.write(prompt)
    except OSError:
        logger.warning("claude] Process exited before accepting input")
    with contextlib.suppress(OSError):
        process.stdin.close()
    return process


def _setup_timeout(process: subprocess.Popen, timeout: int) -> threading.Timer:
    def _on_timeout() -> None:
        logger.warning("claude] Timeout after %ds, terminating", timeout)
        terminate(process, "claude")

    timer = threading.Timer(timeout, _on_timeout)
    timer.daemon = True
    timer.start()
    return timer


def _run(
    prompt: str, cwd: str | None, quiet: bool, config: Config | None, extra_args: list[str]
) -> tuple[Result, float]:
    logger.info("claude] Running...")
    logger.debug("claude] prompt length: %d chars", len(prompt))
    start = time.monotonic()
    timeout = config.agent_timeout if config else 900
    effort = config.effort if config else DEFAULT_EFFORT

    process = _start_claude(prompt, cwd, ["--effort", effort, *extra_args])
    timer = _setup_timeout(process, timeout)
    stderr_lines: list[str] = []
    stderr_thread = threading.Thread(
        target=lambda: stderr_lines.extend(process.stderr), daemon=True
    )
    stderr_thread.start()

    has_streamed = False
    try:
        result, has_streamed = _parse_stream(process.stdout, quiet=quiet)
    finally:
        timer.cancel()
        if has_streamed:
            with contextlib.suppress(OSError):
                sys.stdout.write("\n")
        stderr_thread.join(timeout=5)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("claude] Process did not exit, terminating")
            terminate(process, "claude")
        untrack(process)

    stderr = "".join(stderr_lines)
    elapsed = time.monotonic() - start

    if process.returncode != 0:
        if stderr:
            logger.warning("claude] stderr: %s", stderr[:300])
        if not result.text:
            detail = result.error or stderr
            raise RuntimeError(f"Claude exited with code {process.returncode}: {detail[:200]}")

    logger.info("claude] Done in %s", format_duration(elapsed))
    logger.debug("claude] output length: %d chars", len(result.text))
    return result, elapsed


def run_claude(
    prompt: str, cwd: str | None = None, quiet: bool = False, config: Config | None = None
) -> ClaudeResult:
    result, elapsed = _run(prompt, cwd, quiet, config, SKIP_PERMISSIONS)
    return ClaudeResult(result.text, elapsed)


def ask_claude(
    prompt: str, schema: dict, session: str, resume: bool, config: Config
) -> tuple[dict, float]:
    session_args = ["--resume", session] if resume else ["--session-id", session]
    structured_args = ["--disallowedTools", WRITE_TOOLS, "--json-schema", json.dumps(schema)]
    read_only_args = [*READ_ONLY, *structured_args, *session_args]
    result, elapsed = _run(prompt, None, False, config, read_only_args)
    if not isinstance(result.data, dict):
        detail = result.error or result.text
        raise RuntimeError(f"Claude returned no structured output: {detail[:200]}")
    return result.data, elapsed
