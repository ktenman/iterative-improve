from __future__ import annotations

import json
import logging
import time

from improve.process import format_duration, run

logger = logging.getLogger("improve")

CI_POLL_INTERVAL = 10
CI_APPEAR_TIMEOUT = 180
CI_RUN_TIMEOUT = 900
CI_SETTLE_DELAY = 5
CI_SETTLE_CHECKS = 3
MAX_CANCELLED_RETRIES = 3
CI_WORKFLOW = "CI"


def set_timeout(minutes: int) -> None:
    global CI_RUN_TIMEOUT
    CI_RUN_TIMEOUT = minutes * 60


def get_latest_run_id(branch: str) -> int | None:
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
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return None


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


def _get_run_conclusion(run_id: int) -> str | None:
    result = run(["gh", "run", "view", str(run_id), "--json", "conclusion"])
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout).get("conclusion")
    except (json.JSONDecodeError, AttributeError):
        return None


def _watch_run(run_id: int) -> bool:
    result = run(
        ["gh", "run", "watch", str(run_id), "--exit-status"],
        timeout=CI_RUN_TIMEOUT,
    )
    return result.returncode == 0


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
        if _watch_run(run_id):
            elapsed = time.monotonic() - start
            logger.info("ci] Passed in %s", format_duration(elapsed))
            return True, "", elapsed

        if cancelled_retries >= MAX_CANCELLED_RETRIES or _get_run_conclusion(run_id) != "cancelled":
            break

        logger.info("ci] Run #%d was cancelled, looking for newer run...", run_id)
        newer_id = _wait_for_new_run(branch, run_id)
        if not newer_id:
            break
        run_id = newer_id
        cancelled_retries += 1

    elapsed = time.monotonic() - start
    logger.warning("ci] Failed after %s — fetching error logs...", format_duration(elapsed))
    logs = run(["gh", "run", "view", str(run_id), "--log-failed"], timeout=60)
    return False, (logs.stdout[-4000:] if logs.stdout else "No logs available"), elapsed
