import logging
from unittest.mock import MagicMock, patch

import pytest

from improve.version import (
    _notify_upgrade,
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

    def test_returns_none_when_api_returns_non_dict_json(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("improve.version.urllib.request.urlopen", return_value=mock_resp):
            result = get_latest_version()

        assert result is None


class TestNotifyUpgrade:
    def test_logs_upgrade_notification(self, caplog):
        with caplog.at_level(logging.INFO, logger="improve"):
            _notify_upgrade("0.1.0", "0.2.0")

        assert "New version available: 0.1.0" in caplog.text
        assert "0.2.0" in caplog.text
        assert "uv tool upgrade iterative-improve" in caplog.text


class TestCheckForUpdate:
    def test_triggers_notification_when_newer_version_available(self):
        with (
            patch("improve.version.get_installed_version", return_value="0.1.0"),
            patch("improve.version.get_latest_version", return_value="0.2.0"),
            patch("improve.version._notify_upgrade") as mock_notify,
        ):
            check_for_update()

        mock_notify.assert_called_once_with("0.1.0", "0.2.0")

    def test_does_not_notify_when_up_to_date(self):
        with (
            patch("improve.version.get_installed_version", return_value="0.2.0"),
            patch("improve.version.get_latest_version", return_value="0.2.0"),
            patch("improve.version._notify_upgrade") as mock_notify,
        ):
            check_for_update()

        mock_notify.assert_not_called()

    def test_does_not_notify_when_latest_unavailable(self):
        with (
            patch("improve.version.get_installed_version", return_value="0.1.0"),
            patch("improve.version.get_latest_version", return_value=None),
            patch("improve.version._notify_upgrade") as mock_notify,
        ):
            check_for_update()

        mock_notify.assert_not_called()

    def test_swallows_unexpected_exceptions_in_daemon_thread(self):
        with patch("improve.version.get_installed_version", side_effect=RuntimeError("boom")):
            check_for_update()
