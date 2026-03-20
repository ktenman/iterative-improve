from __future__ import annotations

import json
import logging
import time

from improve.ci import CIConclusion
from improve.process import run

logger = logging.getLogger("improve")

POLL_INTERVAL = 10

_STATUS_MAP: dict[str, CIConclusion] = {
    "success": CIConclusion.SUCCESS,
    "canceled": CIConclusion.CANCELLED,
    "failed": CIConclusion.FAILURE,
    "skipped": CIConclusion.FAILURE,
}


class GitLabCI:
    """GitLab CI provider using the glab CLI."""

    def get_latest_run_id(self, branch: str) -> int | None:
        result = run(["glab", "ci", "list", "--ref", branch, "--per-page", "1", "-F", "json"])
        if result.returncode != 0:
            return None
        try:
            pipelines = json.loads(result.stdout)
            return pipelines[0]["id"] if pipelines else None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            logger.debug("ci] Failed to parse pipeline list: %s", exc)
            return None

    def get_run_conclusion(self, run_id: int) -> CIConclusion | None:
        result = run(["glab", "ci", "get", "-p", str(run_id), "-F", "json"])
        if result.returncode != 0:
            return None
        try:
            status = json.loads(result.stdout).get("status")
            return _STATUS_MAP.get(status)
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            logger.debug("ci] Failed to parse pipeline status: %s", exc)
            return None

    def watch_run(self, run_id: int, timeout: int) -> bool:
        start = time.monotonic()
        deadline = start + timeout
        while time.monotonic() < deadline:
            conclusion = self.get_run_conclusion(run_id)
            if conclusion == CIConclusion.SUCCESS:
                return True
            if conclusion is not None:
                return False
            elapsed = int(time.monotonic() - start)
            logger.info("ci] Polling pipeline #%d... (%ds elapsed)", run_id, elapsed)
            time.sleep(POLL_INTERVAL)
        logger.warning("ci] Pipeline %d timed out after %ds", run_id, timeout)
        return False

    def get_failed_logs(self, run_id: int) -> str:
        detail = run(["glab", "ci", "get", "-p", str(run_id), "-d", "-F", "json"], timeout=60)
        if detail.returncode != 0 or not detail.stdout:
            return "No logs available"

        failed_job_ids = self._extract_failed_job_ids(detail.stdout)
        if not failed_job_ids:
            return "No failed jobs found"

        logs: list[str] = []
        for job_id in failed_job_ids:
            trace = run(["glab", "ci", "trace", str(job_id)], timeout=60)
            if trace.returncode == 0 and trace.stdout:
                logs.append(trace.stdout)

        if not logs:
            return "No logs available"
        return "\n".join(logs)[-4000:]

    @staticmethod
    def _extract_failed_job_ids(stdout: str) -> list[int]:
        try:
            data = json.loads(stdout)
            jobs = data.get("jobs", [])
            return [j["id"] for j in jobs if j.get("status") == "failed"]
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            logger.debug("ci] Failed to extract failed job IDs: %s", exc)
            return []
