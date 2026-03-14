from __future__ import annotations

import json
import logging
import time

from improve.process import run

logger = logging.getLogger("improve")

CI_POLL_INTERVAL = 10
CI_APPEAR_TIMEOUT = 180
CI_RUN_TIMEOUT = 900


def set_timeout(minutes: int):
    global CI_RUN_TIMEOUT
    CI_RUN_TIMEOUT = minutes * 60


def get_latest_run_id(branch: str) -> int | None:
    result = run(
        ["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId"]
    )
    if result.returncode != 0:
        return None
    runs = json.loads(result.stdout)
    return runs[0]["databaseId"] if runs else None


def _wait_for_new_run(branch: str, previous_id: int | None) -> int | None:
    deadline = time.time() + CI_APPEAR_TIMEOUT
    while time.time() < deadline:
        current_id = get_latest_run_id(branch)
        if current_id and current_id != previous_id:
            return current_id
        time.sleep(CI_POLL_INTERVAL)
    return None


def wait_for_ci(branch: str, known_previous_id: int | None = None) -> tuple[bool, str, float]:
    start = time.monotonic()
    previous_id = known_previous_id if known_previous_id is not None else get_latest_run_id(branch)
    logger.info("ci] Waiting for CI run...")

    run_id = _wait_for_new_run(branch, previous_id)
    if not run_id:
        logger.info("ci] No CI run detected, skipping")
        return True, "", time.monotonic() - start

    logger.info("ci] Watching run #%d...", run_id)
    result = run(
        ["gh", "run", "watch", str(run_id), "--exit-status"],
        timeout=CI_RUN_TIMEOUT,
    )

    elapsed = time.monotonic() - start
    if result.returncode == 0:
        logger.info("ci] Passed in %s", _format_duration(elapsed))
        return True, "", elapsed

    logger.warning("ci] Failed after %s — fetching error logs...", _format_duration(elapsed))
    logs = run(["gh", "run", "view", str(run_id), "--log-failed"], timeout=60)
    return False, (logs.stdout[-4000:] if logs.stdout else "No logs available"), elapsed


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"
