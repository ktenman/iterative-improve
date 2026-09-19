import pytest

from improve.findings import (
    AGENTS,
    KEEP_CONFIDENCE,
    LINE_WINDOW,
    OTHER_AGENT,
    PICKS_SCHEMA,
    POSITIONS_SCHEMA,
    Finding,
    Place,
    Report,
    findings_schema,
    merge_findings,
    normalize_path,
    same_place,
)

CHANGED = ["shelf/jobs.py", "shelf/client.py"]
STRING = {"type": "string"}


def _raw(
    file="shelf/jobs.py", symbol="run_job", line=10, severity="medium", confidence=90, title="t"
):
    return {
        "phase": "review",
        "file": file,
        "symbol": symbol,
        "line": line,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "detail": "d",
        "suggested_fix": "s",
    }


def _settled(
    outcome, file="shelf/jobs.py", symbol="run_job", line=10, phase="review", severity="medium"
):
    return {
        "outcome": outcome,
        "phase": phase,
        "severity": severity,
        "file": file,
        "symbol": symbol,
        "line": line,
    }


def _merge(claude=(), codex=(), ledger=(), root="/repo"):
    reports = {"claude": list(claude), "codex": list(codex)}
    return merge_findings(reports, CHANGED, list(ledger), root)


class TestConstants:
    def test_agents_and_their_counterparts(self):
        assert AGENTS == ("claude", "codex")
        assert OTHER_AGENT == {"claude": "codex", "codex": "claude"}

    def test_merge_thresholds_match_the_benchmark(self):
        assert (KEEP_CONFIDENCE, LINE_WINDOW) == (50, 3)


class TestSamePlace:
    def test_a_method_matches_the_same_function_reported_without_its_class(self):
        assert same_place(Place("a.py", "Loop.run_phase", 10), Place("a.py", " run_phase() ", 80))

    def test_different_methods_of_the_same_class_are_different_places(self):
        assert not same_place(Place("a.py", "Job.run", 10), Place("a.py", "Job.stop", 10))

    def test_dunder_methods_of_different_classes_are_different_places(self):
        assert not same_place(
            Place("a.py", "Job.__init__", 11), Place("a.py", "Queue.__init__", 11)
        )

    def test_the_same_dunder_method_of_the_same_class_is_one_place(self):
        assert same_place(Place("a.py", "pkg.Job.__init__", 11), Place("a.py", "Job.__init__", 40))

    def test_an_unqualified_dunder_matches_the_same_dunder_of_a_named_class(self):
        assert same_place(Place("a.py", "__init__", 11), Place("a.py", "Council.__init__", 90))

    def test_a_name_mangled_method_is_matched_by_its_name(self):
        assert same_place(Place("a.py", "Job.__secret", 11), Place("a.py", "__secret", 40))

    def test_the_same_method_name_in_two_classes_is_not_one_place(self):
        assert not same_place(Place("a.py", "Reader.run", 10), Place("a.py", "Writer.run", 12))

    def test_the_same_symbol_in_different_files_is_different_places(self):
        assert not same_place(Place("a.py", "run", 1), Place("b.py", "run", 1))

    @pytest.mark.parametrize(
        ("line", "expected"), [(29, True), (32, True), (26, True), (33, False), (25, False)]
    )
    def test_without_a_symbol_lines_at_most_three_apart_are_one_place(self, line, expected):
        assert same_place(Place("a.py", "", 29), Place("a.py", "run", line)) is expected

    def test_a_symbol_of_only_dots_counts_as_no_symbol(self):
        assert same_place(Place("a.py", "..", 29), Place("a.py", "run", 30))

    def test_an_unknown_line_never_matches_by_proximity(self):
        assert not same_place(Place("a.py", "", 0), Place("a.py", "run", 2))

    def test_two_unknown_lines_in_one_file_are_not_the_same_place(self):
        assert not same_place(Place("a.py", "", 0), Place("a.py", "", 0))


class TestNormalizePath:
    def test_strips_whitespace_and_a_leading_dot_slash(self):
        assert normalize_path(" ./shelf/jobs.py ", "/repo") == "shelf/jobs.py"

    def test_keeps_relative_paths(self):
        assert normalize_path("shelf/jobs.py", "/repo") == "shelf/jobs.py"

    def test_makes_absolute_paths_inside_the_repo_relative(self, tmp_path):
        path = str(tmp_path / "shelf" / "jobs.py")

        assert normalize_path(path, str(tmp_path)) == "shelf/jobs.py"

    def test_keeps_absolute_paths_outside_the_repo(self, tmp_path):
        outside = str(tmp_path.parent / "elsewhere.py")

        assert normalize_path(outside, str(tmp_path)) == outside


class TestMergeFindings:
    def test_findings_on_the_same_function_merge_even_when_line_numbers_disagree(self):
        items = _merge(claude=[_raw(line=155)], codex=[_raw(file="./shelf/jobs.py", line=27)])

        assert [(i.id, i.sources) for i in items] == [("F1", ["claude", "codex"])]

    def test_a_merged_finding_keeps_the_first_reporters_place_and_both_reports(self):
        items = _merge(claude=[_raw(line=155, title="a")], codex=[_raw(line=27, title="b")])

        assert items[0] == Finding(
            phase="review",
            file="shelf/jobs.py",
            symbol="run_job",
            line=155,
            severity="medium",
            confidence=90,
            reports=[Report("claude", "a", "d", "s"), Report("codex", "b", "d", "s")],
            id="F1",
        )

    def test_one_reviewer_cannot_confirm_its_own_finding(self):
        items = _merge(claude=[_raw(title="a"), _raw(title="b")])

        assert [i.sources for i in items] == [["claude"], ["claude"]]

    def test_a_second_reviewer_joins_the_first_matching_finding(self):
        items = _merge(claude=[_raw(title="a"), _raw(title="b")], codex=[_raw(title="c")])

        assert [[r.title for r in i.reports] for i in items] == [["a", "c"], ["b"]]

    def test_findings_in_files_the_branch_did_not_change_are_dropped(self):
        assert _merge(claude=[_raw(file="shelf/storage.py")]) == []

    def test_absolute_paths_inside_the_repo_count_as_changed_files(self, tmp_path):
        items = _merge(codex=[_raw(file=str(tmp_path / "shelf/client.py"))], root=str(tmp_path))

        assert [i.file for i in items] == ["shelf/client.py"]

    @pytest.mark.parametrize(("confidence", "kept"), [(50, 1), (49, 0)])
    def test_a_single_reviewer_finding_is_kept_from_confidence_50(self, confidence, kept):
        assert len(_merge(claude=[_raw(confidence=confidence)])) == kept

    @pytest.mark.parametrize(("severity", "kept"), [("critical", 1), ("high", 1), ("low", 0)])
    def test_a_single_reviewer_finding_is_kept_when_its_severity_is_high_or_critical(
        self, severity, kept
    ):
        assert len(_merge(codex=[_raw(severity=severity, confidence=10)])) == kept

    def test_a_low_confidence_finding_is_kept_when_both_reviewers_report_it(self):
        items = _merge(
            claude=[_raw(severity="low", confidence=10)],
            codex=[_raw(severity="low", confidence=20)],
        )

        assert [(i.severity, i.confidence) for i in items] == [("low", 20)]

    def test_a_merged_finding_keeps_the_highest_severity_and_confidence(self):
        items = _merge(
            claude=[_raw(severity="medium", confidence=60)],
            codex=[_raw(severity="critical", confidence=55)],
        )

        assert (items[0].severity, items[0].confidence) == ("critical", 60)

    def test_a_merged_finding_keeps_its_severity_when_the_second_report_is_lower(self):
        items = _merge(claude=[_raw(severity="high")], codex=[_raw(severity="low")])

        assert items[0].severity == "high"

    def test_a_merged_finding_keeps_its_severity_when_both_are_equal(self):
        items = _merge(claude=[_raw(severity="high")], codex=[_raw(severity="high")])

        assert items[0].severity == "high"

    def test_findings_the_ledger_skipped_are_dropped(self):
        ledger = [_settled("skipped", symbol="Worker.run_job", line=99)]

        assert _merge(claude=[_raw()], ledger=ledger) == []

    def test_findings_the_ledger_left_disputed_are_dropped(self):
        ledger = [_settled("disputed", symbol="Worker.run_job", line=99)]

        assert _merge(claude=[_raw()], ledger=ledger) == []

    def test_a_finding_the_ledger_records_as_fixed_can_be_reported_again(self):
        ledger = [_settled("fixed", symbol="Worker.run_job", line=99)]

        assert len(_merge(claude=[_raw()], ledger=ledger)) == 1

    def test_a_settled_finding_does_not_hide_other_functions_in_the_same_file(self):
        ledger = [_settled("skipped", symbol="stop_job", line=10)]

        assert len(_merge(claude=[_raw()], ledger=ledger)) == 1

    def test_a_skip_recorded_for_one_class_does_not_hide_the_same_method_of_another(self):
        ledger = [_settled("skipped", symbol="Reader.run", line=10)]

        assert len(_merge(claude=[_raw(symbol="Writer.run", line=12)], ledger=ledger)) == 1

    def test_a_finding_from_another_phase_survives_a_settled_one_at_the_same_symbol(self):
        ledger = [_settled("skipped", phase="security")]

        assert len(_merge(claude=[_raw()], ledger=ledger)) == 1

    def test_a_worse_finding_survives_a_settled_milder_one_at_the_same_symbol(self):
        ledger = [_settled("skipped", severity="low")]

        assert len(_merge(claude=[_raw(severity="high")], ledger=ledger)) == 1

    def test_an_equally_severe_finding_is_still_hidden_by_the_ledger(self):
        ledger = [_settled("skipped", severity="high")]

        assert _merge(claude=[_raw(severity="high")], ledger=ledger) == []

    def test_a_settled_entry_from_an_older_ledger_without_a_severity_hides_only_low(self):
        ledger = [
            {
                "outcome": "skipped",
                "phase": "review",
                "file": "shelf/jobs.py",
                "symbol": "run_job",
                "line": 10,
            }
        ]

        assert _merge(claude=[_raw(severity="low", confidence=90)], ledger=ledger) == []
        assert len(_merge(claude=[_raw(severity="medium")], ledger=ledger)) == 1

    def test_a_settled_entry_without_a_line_or_symbol_does_not_hide_the_whole_file(self):
        ledger = [_settled("skipped", symbol="", line=0)]

        assert len(_merge(claude=[_raw(symbol="", line=0)], ledger=ledger)) == 1

    def test_findings_are_sorted_by_file_and_line_and_numbered_from_f1(self):
        items = _merge(
            claude=[_raw(symbol="b", line=30), _raw(file="shelf/client.py", symbol="c", line=50)],
            codex=[_raw(symbol="a", line=5)],
        )

        assert [(i.id, i.file, i.line) for i in items] == [
            ("F1", "shelf/client.py", 50),
            ("F2", "shelf/jobs.py", 5),
            ("F3", "shelf/jobs.py", 30),
        ]

    def test_an_unknown_severity_counts_as_low(self):
        items = _merge(claude=[_raw(severity="urgent", confidence=90)])

        assert items[0].severity == "low"

    def test_missing_fields_get_safe_defaults(self):
        items = _merge(claude=[{"file": "shelf/jobs.py", "confidence": 70}])

        assert items == [
            Finding(
                phase="review",
                file="shelf/jobs.py",
                symbol="",
                line=0,
                severity="low",
                confidence=70,
                reports=[Report("claude", "", "", "")],
                id="F1",
            )
        ]

    def test_values_are_converted_to_their_types(self):
        raw = {**_raw(), "line": "12", "confidence": "80", "symbol": "  run_job  ", "title": 5}

        items = _merge(claude=[raw])

        assert (items[0].line, items[0].confidence, items[0].symbol) == (12, 80, "run_job")
        assert items[0].title == "5"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("line", "12-15"),
            ("line", "12.5"),
            ("line", float("nan")),
            ("line", [12]),
            ("confidence", "80%"),
        ],
    )
    def test_an_unparsable_numeric_field_is_coerced_to_zero_without_raising(self, field, value):
        items = _merge(claude=[_raw(severity="high", **{field: value})])

        assert getattr(items[0], field) == 0

    @pytest.mark.parametrize("bad_entry", ["oops", None])
    def test_a_non_dict_entry_is_skipped_while_a_valid_sibling_is_still_merged(self, bad_entry):
        items = _merge(claude=[bad_entry, _raw()])

        assert [i.file for i in items] == ["shelf/jobs.py"]


class TestFinding:
    def test_title_sources_and_place_come_from_its_fields(self):
        finding = Finding(
            "review",
            "a.py",
            "run",
            3,
            "high",
            90,
            [Report("codex", "First", "", ""), Report("claude", "Second", "", "")],
        )

        assert finding.title == "First"
        assert finding.sources == ["codex", "claude"]
        assert finding.place == Place("a.py", "run", 3)
        assert finding.id == ""


class TestSchemas:
    def test_positions_schema_asks_for_a_decision_approach_and_reason(self):
        assert POSITIONS_SCHEMA == {
            "type": "object",
            "additionalProperties": False,
            "required": ["positions"],
            "properties": {
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "decision", "approach", "reason"],
                        "properties": {
                            "id": STRING,
                            "decision": {"type": "string", "enum": ["fix", "skip"]},
                            "approach": STRING,
                            "reason": STRING,
                        },
                    },
                }
            },
        }

    def test_picks_schema_asks_for_mine_or_theirs_with_a_reason(self):
        assert PICKS_SCHEMA == {
            "type": "object",
            "additionalProperties": False,
            "required": ["picks"],
            "properties": {
                "picks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "pick", "reason"],
                        "properties": {
                            "id": STRING,
                            "pick": {"type": "string", "enum": ["mine", "theirs"]},
                            "reason": STRING,
                        },
                    },
                }
            },
        }

    def test_findings_schema_lists_the_active_phases_and_every_finding_field(self):
        assert findings_schema(["review", "security"]) == {
            "type": "object",
            "additionalProperties": False,
            "required": ["findings"],
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "phase",
                            "file",
                            "symbol",
                            "line",
                            "severity",
                            "confidence",
                            "title",
                            "detail",
                            "suggested_fix",
                        ],
                        "properties": {
                            "phase": {"type": "string", "enum": ["review", "security"]},
                            "file": STRING,
                            "symbol": STRING,
                            "line": {"type": "integer"},
                            "severity": {
                                "type": "string",
                                "enum": ["low", "medium", "high", "critical"],
                            },
                            "confidence": {"type": "integer"},
                            "title": STRING,
                            "detail": STRING,
                            "suggested_fix": STRING,
                        },
                    },
                }
            },
        }
