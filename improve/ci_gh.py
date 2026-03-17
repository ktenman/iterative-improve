from __future__ import annotations

import json
import logging

from improve.process import run

logger = logging.getLogger("improve")

CI_WORKFLOW = "CI"


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
