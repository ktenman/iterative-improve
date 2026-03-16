from __future__ import annotations

import logging
import sys

from improve.process import run

logger = logging.getLogger("improve")

TIMEOUT = 15


def _check_git_remote() -> None:
    result = run(["git", "ls-remote", "--heads", "origin"], timeout=TIMEOUT)
    if result.returncode != 0:
        logger.error(
            "preflight] Git remote 'origin' is not reachable. Verify URL: git remote get-url origin"
        )
        sys.exit(1)


def _check_git_push(branch: str) -> None:
    result = run(["git", "push", "--dry-run", "origin", branch], timeout=TIMEOUT)
    if result.returncode != 0:
        logger.error(
            "preflight] Cannot push to origin/%s. Check repo permissions or SSH key: ssh-add -l",
            branch,
        )
        sys.exit(1)


def _check_ci_auth(ci_tool: str) -> None:
    result = run([ci_tool, "auth", "status"], timeout=TIMEOUT)
    if result.returncode != 0:
        logger.error(
            "preflight] %s CLI not authenticated. Run: %s auth login",
            ci_tool,
            ci_tool,
        )
        sys.exit(1)


def _check_ci_repo_access(ci_tool: str) -> None:
    cmd = (
        [ci_tool, "repo", "view", "--json", "name"]
        if ci_tool == "gh"
        else [ci_tool, "repo", "view"]
    )
    result = run(cmd, timeout=TIMEOUT)
    if result.returncode != 0:
        logger.error(
            "preflight] %s cannot access this repository. "
            "Verify you have read access and the remote is correct",
            ci_tool,
        )
        sys.exit(1)


def run_preflight(branch: str, ci_tool: str, skip_ci: bool) -> None:
    _check_git_remote()
    _check_git_push(branch)
    if skip_ci:
        return
    _check_ci_auth(ci_tool)
    _check_ci_repo_access(ci_tool)
