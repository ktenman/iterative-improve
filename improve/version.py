from __future__ import annotations

import http.client
import json
import logging
import re
import shutil
import subprocess
import urllib.request
from importlib.metadata import PackageNotFoundError, version

logger = logging.getLogger("improve")

GITHUB_REPO = "ktenman/iterative-improve"


def get_installed_version() -> str:
    try:
        return version("iterative-improve")
    except PackageNotFoundError:
        return "0.0.0"


def get_latest_version() -> str | None:
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.load(resp)
            tag = data.get("tag_name") or ""
            return tag.lstrip("v")
    except (json.JSONDecodeError, OSError, http.client.HTTPException, AttributeError, TypeError):
        return None


def _parse_version(v: str) -> tuple[int, ...]:
    parts = [int(m.group()) for seg in v.split(".") if (m := re.match(r"\d+", seg))]
    return tuple(parts) or (0,)


def _auto_upgrade(installed: str, latest: str) -> None:
    uv = shutil.which("uv")
    if not uv:
        logger.info(
            "update] New version available: %s → %s — run: uv tool upgrade iterative-improve",
            installed,
            latest,
        )
        return
    logger.info("update] Upgrading %s → %s …", installed, latest)
    try:
        result = subprocess.run(
            [uv, "tool", "upgrade", "iterative-improve"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("update] Upgrade timed out after 60s")
        return
    except OSError as exc:
        logger.warning("update] Upgrade failed to start: %s", exc)
        return
    if result.returncode == 0:
        logger.info("update] Upgraded to %s (takes effect on next run)", latest)
    else:
        logger.warning("update] Upgrade failed: %s", result.stderr.strip()[:200])


def check_for_update() -> None:
    try:
        installed = get_installed_version()
        latest = get_latest_version()
        if not latest:
            return
        if _parse_version(latest) > _parse_version(installed):
            _auto_upgrade(installed, latest)
    except Exception:
        logger.debug("update] Version check failed", exc_info=True)
