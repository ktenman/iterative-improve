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

from improve.config import Config
from improve.process import format_duration

logger = logging.getLogger("improve")


class ClaudeResult(NamedTuple):
    text: str
    elapsed: float


_active_processes: set[subprocess.Popen] = set()
_process_lock = threading.RLock()


def _terminate_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    logger.info("claude] Terminating subprocess...")
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def terminate_active() -> None:
    with _process_lock:
        processes = list(_active_processes)
    for proc in processes:
        _terminate_process(proc)


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
            yield Result(event.get("result", ""))
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


def _parse_stream(stdout: Iterator[str], quiet: bool = False) -> tuple[str, bool]:
    result_text = ""
    has_streamed = False
    current_tool = ""
    tool_input_chunks: list[str] = []

    for event in _classify_events(stdout):
        if isinstance(event, Result):
            result_text = event.text
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

    return result_text, has_streamed


def _start_claude(prompt: str, cwd: str | None) -> subprocess.Popen:
    process = subprocess.Popen(
        [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            "--effort",
            "max",
            "--max-turns",
            "50",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    with _process_lock:
        _active_processes.add(process)
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
        _terminate_process(process)

    timer = threading.Timer(timeout, _on_timeout)
    timer.daemon = True
    timer.start()
    return timer


def run_claude(
    prompt: str, cwd: str | None = None, quiet: bool = False, config: Config | None = None
) -> ClaudeResult:
    logger.info("claude] Running...")
    logger.debug("claude] prompt length: %d chars", len(prompt))
    start = time.monotonic()
    timeout = config.claude_timeout if config else 900

    process = _start_claude(prompt, cwd)
    timer = _setup_timeout(process, timeout)
    stderr_lines: list[str] = []
    stderr_thread = threading.Thread(
        target=lambda: stderr_lines.extend(process.stderr), daemon=True
    )
    stderr_thread.start()

    result_text = ""
    has_streamed = False
    try:
        result_text, has_streamed = _parse_stream(process.stdout, quiet=quiet)
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
            _terminate_process(process)
        with _process_lock:
            _active_processes.discard(process)

    stderr = "".join(stderr_lines)
    elapsed = time.monotonic() - start

    if process.returncode != 0:
        if stderr:
            logger.warning("claude] stderr: %s", stderr[:300])
        if not result_text:
            raise RuntimeError(f"Claude exited with code {process.returncode}: {stderr[:200]}")

    logger.info("claude] Done in %s", format_duration(elapsed))
    logger.debug("claude] output length: %d chars", len(result_text))
    return ClaudeResult(result_text, elapsed)
