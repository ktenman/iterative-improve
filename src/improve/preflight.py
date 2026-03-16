from __future__ import annotations

import logging
import sys

from improve.process import run

logger = logging.getLogger("improve")

TIMEOUT = 15


def _check(cmd: list[str], error_msg: str, *args: str) -> None:
    result = run(cmd, timeout=TIMEOUT)
    if result.returncode != 0:
        logger.error(error_msg, *args)
        sys.exit(1)


def run_preflight(branch: str, ci_tool: str, skip_ci: bool) -> None:
    _check(
        ["git", "ls-remote", "--heads", "origin"],
        "preflight] Git remote 'origin' is not reachable. Verify URL: git remote get-url origin",
    )
    _check(
        ["git", "push", "--dry-run", "origin", branch],
        "preflight] Cannot push to origin/%s. Check repo permissions or SSH key: ssh-add -l",
        branch,
    )
    if skip_ci:
        return
    _check(
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
    _check(
        repo_cmd,
        "preflight] %s cannot access this repository. "
        "Verify you have read access and the remote is correct",
        ci_tool,
    )
