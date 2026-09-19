from __future__ import annotations

import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from improve.config import Config
from improve.process import format_duration, terminate, track, untrack

logger = logging.getLogger("improve")

MODEL_CHECK_TIMEOUT = 60
MODEL_CHECK_PROMPT = "Reply with the word OK."
PROJECT_DOC_FALLBACK = 'project_doc_fallback_filenames=["CLAUDE.md"]'


class CodexReply(NamedTuple):
    data: dict
    thread: str
    elapsed: float


class CodexEvents(NamedTuple):
    thread: str
    tokens: int
    error: str
    failed: bool


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _api_message(message: str) -> str:
    inner = _json_object(message).get("error")
    if isinstance(inner, dict) and inner.get("message"):
        return str(inner["message"])
    return message


def _event_error(event: dict) -> str:
    detail = event.get("error") if event.get("type") == "turn.failed" else event
    message = detail.get("message") if isinstance(detail, dict) else detail
    return _api_message(str(message or "unknown error"))


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_events(stdout: str) -> CodexEvents:
    thread, used, error, failed = "", 0, "", False
    for line in stdout.splitlines():
        event = _json_object(line)
        kind = event.get("type")
        if kind == "thread.started":
            thread = str(event.get("thread_id") or "")
        elif kind == "turn.completed":
            usage = event.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            used += _to_int(usage.get("input_tokens")) + _to_int(usage.get("output_tokens"))
        elif kind in ("error", "turn.failed"):
            error = _event_error(event)
            failed = failed or kind == "turn.failed"
    return CodexEvents(thread, used, error, failed)


def _communicate(command: list[str], prompt: str, timeout: int) -> tuple[str, str, int]:
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        track(process, "codex")
        try:
            stdout, stderr = process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("codex] Timeout after %ds, terminating", timeout)
            terminate(process, "codex")
            raise RuntimeError(f"Codex timed out after {timeout}s") from None
        finally:
            untrack(process)
    return stdout, stderr, process.returncode


def _model_flags(config: Config) -> list[str]:
    return ["-m", config.codex_model, "-c", f"model_reasoning_effort={config.effort}"]


def _command(config: Config, thread: str, schema_file: Path, reply_file: Path) -> list[str]:
    start = ["codex", "exec", "resume", thread] if thread else ["codex", "exec"]
    sandbox = ["-c", "sandbox_mode=read-only"] if thread else ["-s", "read-only"]
    return [
        *start,
        "--json",
        *sandbox,
        *_model_flags(config),
        "-c",
        PROJECT_DOC_FALLBACK,
        "--output-schema",
        str(schema_file),
        "-o",
        str(reply_file),
        "-",
    ]


def run_codex(prompt: str, schema: dict, config: Config, thread: str = "") -> CodexReply:
    logger.info("codex] %s...", "Resuming" if thread else "Running")
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="improve-codex-") as folder:
        schema_file, reply_file = Path(folder, "schema.json"), Path(folder, "reply.json")
        schema_file.write_text(json.dumps(schema))
        command = _command(config, thread, schema_file, reply_file)
        stdout, stderr, returncode = _communicate(command, prompt, config.agent_timeout)
        reply = reply_file.read_text() if reply_file.exists() else ""
    elapsed = time.monotonic() - start
    events = parse_events(stdout)
    if returncode != 0 or events.failed:
        detail = events.error or stderr.strip() or f"exit code {returncode}"
        raise RuntimeError(f"Codex failed: {detail[:300]}")
    data = _json_object(reply)
    if not data:
        detail = reply.strip() or events.error or stderr.strip() or "empty reply"
        raise RuntimeError(f"Codex returned no structured output: {detail[:300]}")
    logger.info("codex] Done in %s (%d tokens)", format_duration(elapsed), events.tokens)
    return CodexReply(data, events.thread or thread, elapsed)


def check_model(config: Config) -> str:
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "-s",
        "read-only",
        *_model_flags(config),
        "-",
    ]
    try:
        stdout, stderr, returncode = _communicate(command, MODEL_CHECK_PROMPT, MODEL_CHECK_TIMEOUT)
    except (OSError, RuntimeError) as exc:
        return str(exc)
    events = parse_events(stdout)
    if returncode == 0 and not events.failed:
        return ""
    return events.error or stderr.strip() or f"codex exited with code {returncode}"
