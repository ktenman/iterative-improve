import subprocess
from unittest.mock import MagicMock

from improve.config import Config
from improve.state import LoopState


def _cp(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _test_config(provider: object | None = None) -> Config:
    return Config(agent_timeout=900, ci_timeout=900, ci_provider=provider or MagicMock())


def _state(tmp_path, monkeypatch) -> LoopState:
    monkeypatch.setattr("improve.state.STATE_DIR", tmp_path)
    monkeypatch.setattr("improve.state.STATE_FILE", tmp_path / "state.json")
    return LoopState(branch="feature", started_at="2026-09-17T00:00:00")
