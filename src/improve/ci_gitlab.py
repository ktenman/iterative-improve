from __future__ import annotations

import json
import logging
import time

from improve.process import run

logger = logging.getLogger("improve")

POLL_INTERVAL = 10

_STATUS_MAP = {
    "success": "success",
    "canceled": "cancelled",
    "failed": "failure",
    "skipped": "failure",
}


class GitLabCI:
    def get_latest_run_id(self, branch: str) -> int | None:
        result = run(["glab", "ci", "list", "--branch", branch, "--per-page", "1", "-o", "json"])
        if result.returncode != 0:
            return None
        try:
            pipelines = json.loads(result.stdout)
            return pipelines[0]["id"] if pipelines else None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            logger.debug("ci-gitlab] Failed to parse pipeline list: %s", exc)
            return None

    def get_run_conclusion(self, run_id: int) -> str | None:
        result = run(["glab", "ci", "view", str(run_id), "-o", "json"])
        if result.returncode != 0:
            return None
        try:
            status = json.loads(result.stdout).get("status")
            return _STATUS_MAP.get(status)
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.debug("ci-gitlab] Failed to parse pipeline status: %s", exc)
            return None

    def watch_run(self, run_id: int, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            conclusion = self.get_run_conclusion(run_id)
            if conclusion == "success":
                return True
            if conclusion is not None:
                return False
            time.sleep(POLL_INTERVAL)
        logger.warning("ci-gitlab] Pipeline %d timed out after %ds", run_id, timeout)
        return False

    def get_failed_logs(self, run_id: int) -> str:
        result = run(["glab", "ci", "view", str(run_id), "--log"], timeout=60)
        return result.stdout[-4000:] if result.stdout else "No logs available"
