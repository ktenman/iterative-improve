from __future__ import annotations

import logging
import time
from enum import Enum
from typing import NamedTuple, Protocol

from improve.config import Config
from improve.process import format_duration

logger = logging.getLogger("improve")

CI_POLL_INTERVAL = 10
CI_APPEAR_TIMEOUT = 180
CI_SETTLE_DELAY = 5
CI_SETTLE_CHECKS = 3
MAX_CANCELLED_RETRIES = 3


class CIConclusion(Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class CIResult(NamedTuple):
    passed: bool
    errors: str
    elapsed: float


class CIProvider(Protocol):
    """Interface for CI providers (GitHub Actions, GitLab CI)."""

    def get_latest_run_id(self, branch: str) -> int | None: ...
    def get_run_conclusion(self, run_id: int) -> CIConclusion | None: ...
    def watch_run(self, run_id: int, timeout: int) -> bool: ...
    def get_failed_logs(self, run_id: int) -> str: ...


def get_latest_run_id(branch: str, config: Config) -> int | None:
    return config.ci_provider.get_latest_run_id(branch)


def _wait_for_new_run(branch: str, previous_id: int | None, config: Config) -> int | None:
    deadline = time.monotonic() + CI_APPEAR_TIMEOUT
    while time.monotonic() < deadline:
        current_id = get_latest_run_id(branch, config)
        if not current_id or current_id == previous_id:
            time.sleep(CI_POLL_INTERVAL)
            continue
        for _ in range(CI_SETTLE_CHECKS):
            time.sleep(CI_SETTLE_DELAY)
            latest = get_latest_run_id(branch, config)
            if not latest or latest == current_id:
                break
            current_id = latest
        return current_id
    return None


def wait_for_ci(
    branch: str,
    config: Config,
    known_previous_id: int | None = None,
) -> CIResult:
    start = time.monotonic()
    if known_previous_id is None:
        known_previous_id = get_latest_run_id(branch, config)
    logger.info("ci] Waiting for CI run...")

    run_id = _wait_for_new_run(branch, known_previous_id, config)
    if not run_id:
        logger.info("ci] No CI run detected, skipping")
        return CIResult(True, "", time.monotonic() - start)

    cancelled_retries = 0
    while True:
        logger.info("ci] Watching run #%d...", run_id)
        if config.ci_provider.watch_run(run_id, config.ci_timeout):
            elapsed = time.monotonic() - start
            logger.info("ci] Passed in %s", format_duration(elapsed))
            return CIResult(True, "", elapsed)

        conclusion = config.ci_provider.get_run_conclusion(run_id)
        if cancelled_retries >= MAX_CANCELLED_RETRIES or conclusion != CIConclusion.CANCELLED:
            break

        logger.info("ci] Run #%d was cancelled, looking for newer run...", run_id)
        newer_id = _wait_for_new_run(branch, run_id, config)
        if not newer_id:
            break
        run_id = newer_id
        cancelled_retries += 1

    elapsed = time.monotonic() - start
    logger.warning("ci] Failed after %s — fetching error logs...", format_duration(elapsed))
    return CIResult(False, config.ci_provider.get_failed_logs(run_id), elapsed)
