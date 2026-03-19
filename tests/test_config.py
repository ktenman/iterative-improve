from improve.ci_gh import GitHubCI
from improve.config import Config


class TestConfigDefaults:
    def test_default_claude_timeout_is_900(self):
        config = Config()

        assert config.claude_timeout == 900

    def test_default_ci_timeout_is_900(self):
        config = Config()

        assert config.ci_timeout == 900

    def test_default_provider_is_github(self):
        config = Config()

        assert isinstance(config.ci_provider, GitHubCI)

    def test_accepts_custom_claude_timeout(self):
        config = Config(claude_timeout=300)

        assert config.claude_timeout == 300

    def test_accepts_custom_ci_timeout(self):
        config = Config(ci_timeout=600)

        assert config.ci_timeout == 600
