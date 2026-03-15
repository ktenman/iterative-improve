from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("improve")


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    cmd_str = " ".join(cmd)
    logger.debug("cmd: %s", cmd_str)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("cmd timed out after %ds: %s", timeout, cmd_str)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="Timed out")
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


def require_tools() -> None:
    missing = [tool for tool in ["git", "claude", "gh"] if not shutil.which(tool)]
    if missing:
        logger.error("missing] Missing required tools: %s", ", ".join(missing))
        raise SystemExit(1)
