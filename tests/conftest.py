import subprocess
from unittest.mock import MagicMock

from improve.config import Config


def _cp(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _test_config(provider: object | None = None) -> Config:
    return Config(claude_timeout=900, ci_timeout=900, ci_provider=provider or MagicMock())
