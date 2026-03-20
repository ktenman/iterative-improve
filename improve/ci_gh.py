from __future__ import annotations

import json
import logging
from enum import Enum

from improve.ci import CIConclusion
from improve.process import run

logger = logging.getLogger("improve")


class _CIWorkflowPattern(Enum):
    """Known CI workflow names in priority order."""

    CI = "ci"
    BUILD = "build"
    TEST = "test"
    TESTS = "tests"
    PIPELINE = "pipeline"


class GitHubCI:
    """GitHub Actions CI provider using the gh CLI."""

    def __init__(self, workflow: str | None = None) -> None:
        self._workflow = workflow
        self._discovered = workflow is not None

    def _discover_workflow(self) -> str | None:
        if self._discovered:
            return self._workflow
        self._discovered = True
        result = run(["gh", "workflow", "list", "--json", "name,state"])
        if result.returncode != 0:
            return self._workflow
        try:
            workflows = json.loads(result.stdout)
            active = [w["name"] for w in workflows if w.get("state") == "active"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.debug("ci] Failed to parse workflow list: %s", exc)
            return self._workflow
        for pattern in _CIWorkflowPattern:
            for name in active:
                if name.lower() == pattern.value:
                    self._workflow = name
                    logger.info("ci] Discovered workflow: %s", name)
                    return self._workflow
        logger.debug("ci] No CI workflow found, listing runs without filter")
        return self._workflow

    def get_latest_run_id(self, branch: str) -> int | None:
        cmd = ["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId"]
        workflow = self._discover_workflow()
        if workflow:
            cmd.extend(["--workflow", workflow])
        result = run(cmd)
        if result.returncode != 0:
            return None
        try:
            runs = json.loads(result.stdout)
            return runs[0]["databaseId"] if runs else None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            logger.debug("ci] Failed to parse run list: %s", exc)
            return None

    def get_run_conclusion(self, run_id: int) -> CIConclusion | None:
        result = run(["gh", "run", "view", str(run_id), "--json", "conclusion"])
        if result.returncode != 0:
            return None
        try:
            conclusion = json.loads(result.stdout).get("conclusion")
            if not conclusion:
                return None
            return CIConclusion(conclusion)
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.debug("ci] Failed to parse run conclusion: %s", exc)
            return None
        except ValueError:
            logger.debug("ci] Unknown run conclusion %r, treating as failure", conclusion)
            return CIConclusion.FAILURE

    def watch_run(self, run_id: int, timeout: int) -> bool:
        result = run(
            ["gh", "run", "watch", str(run_id), "--exit-status"],
            timeout=timeout,
        )
        return result.returncode == 0

    def get_failed_logs(self, run_id: int) -> str:
        logs = run(["gh", "run", "view", str(run_id), "--log-failed"], timeout=60)
        return logs.stdout[-4000:] if logs.stdout else "No logs available"
