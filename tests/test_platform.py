from improve.platform import Platform


class TestPlatform:
    def test_github_value_is_github(self):
        assert Platform.GITHUB.value == "github"

    def test_gitlab_value_is_gitlab(self):
        assert Platform.GITLAB.value == "gitlab"

    def test_enum_has_exactly_two_members(self):
        assert len(Platform) == 2

    def test_can_construct_from_string_value(self):
        assert Platform("github") == Platform.GITHUB
        assert Platform("gitlab") == Platform.GITLAB
