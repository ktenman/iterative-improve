from __future__ import annotations

import json
import logging
import subprocess
import sys
import time

logger = logging.getLogger("improve")

CLAUDE_TIMEOUT = 600

active_process: subprocess.Popen | None = None

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
        value = data.get(key, "")
        if not value:
            return tool
        truncated = (value[:80] + "...") if len(value) > 80 else value
        return f"{tool} > {truncated}"
    except json.JSONDecodeError:
        return tool


def run_claude(prompt: str) -> tuple[str, float]:
    global active_process
    logger.info("claude] Running...")
    logger.debug("claude] prompt length: %d chars", len(prompt))
    start = time.monotonic()
    process = subprocess.Popen(
        [
            "claude", "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--dangerously-skip-permissions",
            "--effort", "max",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    active_process = process
    process.stdin.write(prompt)
    process.stdin.close()

    result_text = ""
    has_streamed = False
    current_tool = ""
    tool_input_chunks: list[str] = []

    try:
        for line in process.stdout:
            if time.monotonic() - start > CLAUDE_TIMEOUT:
                logger.warning("claude] Timeout after %ds, terminating", CLAUDE_TIMEOUT)
                process.terminate()
                break

            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                event_type = event.get("type", "")

                if event_type == "stream_event":
                    inner = event.get("event", {})
                    delta = inner.get("delta", {})
                    inner_type = inner.get("type", "")

                    if delta.get("type") == "text_delta":
                        sys.stdout.write(delta["text"])
                        sys.stdout.flush()
                        has_streamed = True

                    elif inner_type == "content_block_start":
                        block = inner.get("content_block", {})
                        if block.get("type") == "tool_use":
                            if has_streamed:
                                sys.stdout.write("\n")
                                has_streamed = False
                            current_tool = block.get("name", "?")
                            tool_input_chunks = []

                    elif delta.get("type") == "input_json_delta":
                        tool_input_chunks.append(delta.get("partial_json", ""))

                    elif inner_type == "content_block_stop" and current_tool:
                        detail = _summarize_tool_input(current_tool, "".join(tool_input_chunks))
                        logger.info("claude] %s", detail)
                        current_tool = ""

                elif event_type == "result":
                    result_text = event.get("result", "")

            except json.JSONDecodeError:
                logger.debug("claude] unparseable line")
    finally:
        active_process = None

    if has_streamed:
        sys.stdout.write("\n")
    stderr = process.stderr.read()
    process.wait()
    elapsed = time.monotonic() - start

    if process.returncode != 0 and stderr:
        logger.warning("claude] stderr: %s", stderr[:300])

    logger.info("claude] Done in %s", _format_duration(elapsed))
    logger.debug("claude] output length: %d chars", len(result_text))
    return result_text, elapsed


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"
