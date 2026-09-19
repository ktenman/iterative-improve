from improve.ci_gh import GitHubCI
from improve.config import EFFORT_TIMEOUTS, Config


class TestConfigDefaults:
    def test_default_agent_timeout_matches_the_default_effort(self):
        config = Config()

        assert config.agent_timeout == EFFORT_TIMEOUTS[config.effort]

    def test_default_ci_timeout_is_900(self):
        config = Config()

        assert config.ci_timeout == 900

    def test_default_provider_is_github(self):
        config = Config()

        assert isinstance(config.ci_provider, GitHubCI)

    def test_accepts_custom_agent_timeout(self):
        config = Config(agent_timeout=300)

        assert config.agent_timeout == 300

    def test_accepts_custom_ci_timeout(self):
        config = Config(ci_timeout=600)

        assert config.ci_timeout == 600

    def test_default_effort_is_max(self):
        config = Config()

        assert config.effort == "max"

    def test_default_codex_model_is_gpt_6_astra(self):
        config = Config()

        assert config.codex_model == "gpt-6-astra"


class TestEffortTimeouts:
    def test_max_effort_gets_2700_seconds_and_medium_900(self):
        assert EFFORT_TIMEOUTS == {"max": 2700, "medium": 900}
