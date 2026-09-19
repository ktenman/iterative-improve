from __future__ import annotations

import contextlib
import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from concurrent.futures import FIRST_EXCEPTION, Future, wait

logger = logging.getLogger("improve")

PREFLIGHT_TIMEOUT = 16

_active_processes: dict[subprocess.Popen, str] = {}
_process_lock = threading.RLock()


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    cmd_str = " ".join(cmd)
    logger.debug("cmd: %s", cmd_str)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("cmd timed out after %ds: %s", timeout, cmd_str)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="Timed out")
    except OSError as exc:
        logger.warning("cmd failed to execute: %s: %s", cmd_str, exc)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr=str(exc))
    if result.returncode != 0:
        logger.debug("exit=%d stderr=%s", result.returncode, result.stderr[:500])
    return result


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s"


def terminate(proc: subprocess.Popen, tag: str) -> None:
    if proc.poll() is not None:
        return
    logger.info("%s] Terminating subprocess...", tag)
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def track(proc: subprocess.Popen, tag: str) -> None:
    with _process_lock:
        _active_processes[proc] = tag


def untrack(proc: subprocess.Popen) -> None:
    with _process_lock:
        _active_processes.pop(proc, None)


def terminate_active() -> None:
    with _process_lock:
        active = list(_active_processes.items())
    for proc, tag in active:
        terminate(proc, tag)


def abort_on_first_failure(futures: Iterable[Future]) -> None:
    done, _pending = wait(list(futures), return_when=FIRST_EXCEPTION)
    failure = next((future.exception() for future in done if future.exception()), None)
    if failure is None:
        return
    logger.warning("One parallel agent failed, terminating the others")
    terminate_active()
    raise failure


def require_tools(tools: list[str]) -> None:
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        logger.error("preflight] Missing required tools: %s", ", ".join(missing))
        sys.exit(1)


def _check_preflight(cmd: list[str], error_msg: str, *args: str) -> None:
    result = run(cmd, timeout=PREFLIGHT_TIMEOUT)
    if result.returncode != 0:
        logger.error(error_msg, *args)
        sys.exit(1)


def run_preflight(branch: str, ci_tool: str, skip_ci: bool) -> None:
    _check_preflight(
        ["git", "ls-remote", "--heads", "origin"],
        "preflight] Unable to connect to remote repository."
        " Please check your internet connection and try again",
    )
    _check_preflight(
        ["git", "push", "--dry-run", "origin", branch],
        "preflight] Cannot push to origin/%s. Check repo permissions or SSH key: ssh-add -l",
        branch,
    )
    if skip_ci:
        return
    _check_preflight(
        [ci_tool, "auth", "status"],
        "preflight] %s CLI not authenticated. Run: %s auth login",
        ci_tool,
        ci_tool,
    )
    repo_cmd = (
        [ci_tool, "repo", "view", "--json", "name"]
        if ci_tool == "gh"
        else [ci_tool, "repo", "view"]
    )
    _check_preflight(
        repo_cmd,
        "preflight] %s cannot access this repository. "
        "Verify you have read access and the remote is correct",
        ci_tool,
    )
