import logging
from unittest.mock import MagicMock, patch

import pytest

from improve.version import (
    _parse_version,
    check_for_update,
    get_installed_version,
    get_latest_version,
)


class TestGetInstalledVersion:
    def test_returns_version_from_importlib(self):
        with patch("improve.version.version", return_value="1.2.3"):
            result = get_installed_version()

        assert result == "1.2.3"

    def test_returns_fallback_when_package_not_found(self):
        from importlib.metadata import PackageNotFoundError

        with patch("improve.version.version", side_effect=PackageNotFoundError("not found")):
            result = get_installed_version()

        assert result == "0.0.0"


class TestParseVersion:
    @pytest.mark.parametrize(
        "version_str, expected",
        [
            ("1.2.3", (1, 2, 3)),
            ("1.0", (1, 0)),
            ("abc", (0,)),
            ("0.1.0.dev1", (0, 1, 0)),
            ("1.2.3rc1", (1, 2, 3)),
        ],
    )
    def test_parses_version_string(self, version_str, expected):
        result = _parse_version(version_str)

        assert result == expected


class TestGetLatestVersion:
    def test_returns_version_from_github_api(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"tag_name": "v1.2.3"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("improve.version.urllib.request.urlopen", return_value=mock_resp):
            result = get_latest_version()

        assert result == "1.2.3"

    def test_returns_none_on_network_error(self):
        import urllib.error

        with patch(
            "improve.version.urllib.request.urlopen",
            side_effect=urllib.error.URLError("timeout"),
        ):
            result = get_latest_version()

        assert result is None


class TestCheckForUpdate:
    def test_logs_when_newer_version_available(self, caplog):
        with (
            patch("improve.version.get_installed_version", return_value="0.1.0"),
            patch("improve.version.get_latest_version", return_value="0.2.0"),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            check_for_update()

        assert "0.2.0" in caplog.text

    def test_does_not_log_when_up_to_date(self, caplog):
        with (
            patch("improve.version.get_installed_version", return_value="0.2.0"),
            patch("improve.version.get_latest_version", return_value="0.2.0"),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            check_for_update()

        assert "New version" not in caplog.text

    def test_does_not_log_when_latest_unavailable(self, caplog):
        with (
            patch("improve.version.get_installed_version", return_value="0.1.0"),
            patch("improve.version.get_latest_version", return_value=None),
            caplog.at_level(logging.INFO, logger="improve"),
        ):
            check_for_update()

        assert "New version" not in caplog.text
