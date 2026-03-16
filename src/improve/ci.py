from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from improve.process import format_duration, run

logger = logging.getLogger("improve")

CI_POLL_INTERVAL = 10
CI_APPEAR_TIMEOUT = 180
CI_RUN_TIMEOUT = 900
CI_SETTLE_DELAY = 5
CI_SETTLE_CHECKS = 3
MAX_CANCELLED_RETRIES = 3
CI_WORKFLOW = "CI"


class CIProvider(Protocol):
    def get_latest_run_id(self, branch: str) -> int | None: ...
    def get_run_conclusion(self, run_id: int) -> str | None: ...
    def watch_run(self, run_id: int, timeout: int) -> bool: ...
    def get_failed_logs(self, run_id: int) -> str: ...


class GitHubCI:
    def get_latest_run_id(self, branch: str) -> int | None:
        result = run(
            [
                "gh",
                "run",
                "list",
                "--branch",
                branch,
                "--workflow",
                CI_WORKFLOW,
                "--limit",
                "1",
                "--json",
                "databaseId",
            ]
        )
        if result.returncode != 0:
            return None
        try:
            runs = json.loads(result.stdout)
            return runs[0]["databaseId"] if runs else None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            logger.debug("ci] Failed to parse run list: %s", exc)
            return None

    def get_run_conclusion(self, run_id: int) -> str | None:
        result = run(["gh", "run", "view", str(run_id), "--json", "conclusion"])
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout).get("conclusion")
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.debug("ci] Failed to parse run conclusion: %s", exc)
            return None

    def watch_run(self, run_id: int, timeout: int) -> bool:
        result = run(
            ["gh", "run", "watch", str(run_id), "--exit-status"],
            timeout=timeout,
        )
        return result.returncode == 0

    def get_failed_logs(self, run_id: int) -> str:
        logs = run(["gh", "run", "view", str(run_id), "--log-failed"], timeout=60)
        return logs.stdout[-4000:] if logs.stdout else "No logs available"


_provider: CIProvider = GitHubCI()


def set_provider(provider: CIProvider) -> None:
    global _provider
    _provider = provider


def set_timeout(minutes: int) -> None:
    global CI_RUN_TIMEOUT
    CI_RUN_TIMEOUT = minutes * 60


def get_latest_run_id(branch: str) -> int | None:
    return _provider.get_latest_run_id(branch)


def _wait_for_new_run(branch: str, previous_id: int | None) -> int | None:
    deadline = time.monotonic() + CI_APPEAR_TIMEOUT
    while time.monotonic() < deadline:
        current_id = get_latest_run_id(branch)
        if not current_id or current_id == previous_id:
            time.sleep(CI_POLL_INTERVAL)
            continue
        for _ in range(CI_SETTLE_CHECKS):
            time.sleep(CI_SETTLE_DELAY)
            latest = get_latest_run_id(branch)
            if not latest or latest == current_id:
                break
            current_id = latest
        return current_id
    return None


def wait_for_ci(
    branch: str,
    known_previous_id: int | None = None,
) -> tuple[bool, str, float]:
    start = time.monotonic()
    if known_previous_id is None:
        known_previous_id = get_latest_run_id(branch)
    logger.info("ci] Waiting for CI run...")

    run_id = _wait_for_new_run(branch, known_previous_id)
    if not run_id:
        logger.info("ci] No CI run detected, skipping")
        return True, "", time.monotonic() - start

    cancelled_retries = 0
    while True:
        logger.info("ci] Watching run #%d...", run_id)
        if _provider.watch_run(run_id, CI_RUN_TIMEOUT):
            elapsed = time.monotonic() - start
            logger.info("ci] Passed in %s", format_duration(elapsed))
            return True, "", elapsed

        conclusion = _provider.get_run_conclusion(run_id)
        if cancelled_retries >= MAX_CANCELLED_RETRIES or conclusion != "cancelled":
            break

        logger.info("ci] Run #%d was cancelled, looking for newer run...", run_id)
        newer_id = _wait_for_new_run(branch, run_id)
        if not newer_id:
            break
        run_id = newer_id
        cancelled_retries += 1

    elapsed = time.monotonic() - start
    logger.warning("ci] Failed after %s — fetching error logs...", format_duration(elapsed))
    return False, _provider.get_failed_logs(run_id), elapsed
