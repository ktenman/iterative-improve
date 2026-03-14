from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger("improve")


def run(
    cmd: list[str], timeout: int = 120, check: bool = False
) -> subprocess.CompletedProcess:
    logger.debug("cmd: %s", " ".join(cmd))
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=check
    )
    if result.returncode != 0:
        logger.debug("exit=%d stderr=%s", result.returncode, result.stderr[:500])
    return result


def require_tools():
    missing = [tool for tool in ["git", "claude", "gh"] if run(["which", tool]).returncode != 0]
    if missing:
        logger.error("missing] Missing required tools: %s", ", ".join(missing))
        raise SystemExit(1)
