import ast
from pathlib import Path

import pytest

SRC_DIR = Path("src/improve")
TEST_DIR = Path("tests")
SOURCE_FILES = sorted(SRC_DIR.glob("*.py"))
TEST_FILES = sorted(TEST_DIR.glob("test_*.py"))

MAX_FILE_LINES = 300
MAX_NESTING_DEPTH = 3
NESTING_NODES = (ast.If, ast.For, ast.While, ast.Try, ast.With)

ALLOWED_IMPORTS = {
    "cli": {
        "improve",
        "improve.ci",
        "improve.ci_gitlab",
        "improve.claude",
        "improve.git",
        "improve.loop",
        "improve.preflight",
        "improve.process",
        "improve.prompt",
        "improve.state",
        "improve.version",
    },
    "loop": {
        "improve",
        "improve.ci",
        "improve.claude",
        "improve.git",
        "improve.parallel",
        "improve.process",
        "improve.prompt",
        "improve.state",
    },
    "parallel": {
        "improve",
        "improve.ci",
        "improve.claude",
        "improve.git",
        "improve.process",
        "improve.prompt",
        "improve.state",
    },
    "claude": {"improve.process"},
    "ci": {"improve.process"},
    "ci_gitlab": {"improve.process"},
    "git": {"improve.claude", "improve.process", "improve.prompt"},
    "preflight": {"improve.process"},
    "process": set(),
    "prompt": set(),
    "state": {"improve.process"},
    "version": set(),
}

ALLOWED_MUTABLE_GLOBALS = {}

SECRET_PATTERNS = ["password", "secret", "api_key", "token", "private_key"]


def _parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _get_imports(tree: ast.Module) -> set[str]:
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("improve"):
            imports.add(node.module)
    return imports


def _get_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _max_nesting(node: ast.AST, depth: int = 0) -> int:
    max_depth = depth
    for child in ast.iter_child_nodes(node):
        child_depth = depth
        if isinstance(child, NESTING_NODES):
            is_elif = (
                isinstance(child, ast.If)
                and isinstance(node, ast.If)
                and len(node.orelse) == 1
                and node.orelse[0] is child
            )
            if not is_elif:
                child_depth += 1
        max_depth = max(max_depth, _max_nesting(child, child_depth))
    return max_depth


class TestLayerBoundaries:
    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_module_only_imports_allowed_dependencies(self, source_file):
        module = source_file.stem
        if module == "__init__":
            return

        imports = _get_imports(_parse_module(source_file))
        allowed = ALLOWED_IMPORTS[module]
        illegal = imports - allowed

        assert not illegal, f"{module}.py imports {illegal} but only {allowed} are allowed"

    def test_lower_layers_never_import_upper_layers(self):
        lower = {"process", "prompt", "state"}
        upper = {"cli", "loop"}

        for path in SOURCE_FILES:
            module = path.stem
            if module not in lower:
                continue
            for imp in _get_imports(_parse_module(path)):
                assert imp.split(".")[-1] not in upper, (
                    f"{module}.py imports {imp} — lower layer must not depend on upper layer"
                )


class TestNestingDepth:
    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_no_deeply_nested_logic(self, source_file):
        tree = _parse_module(source_file)
        violations = []

        for func in _get_functions(tree):
            depth = _max_nesting(func)
            if depth > MAX_NESTING_DEPTH:
                violations.append(f"{func.name} (depth {depth})")

        assert not violations, (
            f"{source_file.name} has functions nested deeper than "
            f"{MAX_NESTING_DEPTH}: {', '.join(violations)}"
        )


class TestCodeHygiene:
    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_source_file_under_max_lines(self, source_file):
        line_count = len(source_file.read_text().splitlines())

        assert line_count <= MAX_FILE_LINES, (
            f"{source_file.name} has {line_count} lines (max {MAX_FILE_LINES})"
        )

    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_no_bare_except(self, source_file):
        for node in ast.walk(_parse_module(source_file)):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                pytest.fail(
                    f"{source_file.name}:{node.lineno} uses bare 'except:' without exception type"
                )

    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_no_print_in_library_modules(self, source_file):
        if source_file.stem in ("cli", "loop", "__init__"):
            return

        for node in ast.walk(_parse_module(source_file)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                pytest.fail(f"{source_file.name}:{node.lineno} uses print() — use logging")

    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_no_todo_or_fixme_in_source(self, source_file):
        for i, line in enumerate(source_file.read_text().splitlines(), 1):
            upper = line.upper()
            if "TODO" in upper or "FIXME" in upper or "HACK" in upper:
                pytest.fail(f"{source_file.name}:{i} contains TODO/FIXME/HACK")

    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_no_mutable_module_level_globals(self, source_file):
        module = source_file.stem
        if module == "__init__":
            return

        allowed = ALLOWED_MUTABLE_GLOBALS.get(module, set())
        tree = _parse_module(source_file)
        violations = []

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                if name.startswith("_") or name.isupper() or name == "logger":
                    continue
                if name in allowed:
                    continue
                violations.append(name)

        assert not violations, (
            f"{source_file.name} has mutable module-level globals: {', '.join(violations)} "
            f"— encapsulate in a class or add to ALLOWED_MUTABLE_GLOBALS"
        )


class TestSecurity:
    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_no_shell_equals_true(self, source_file):
        content = source_file.read_text()

        assert "shell=True" not in content, f"{source_file.name} uses shell=True (security risk)"

    @pytest.mark.parametrize("source_file", SOURCE_FILES, ids=lambda p: p.stem)
    def test_no_hardcoded_secrets(self, source_file):
        tree = _parse_module(source_file)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                name_lower = target.id.lower()
                for pattern in SECRET_PATTERNS:
                    if pattern in name_lower:
                        pytest.fail(
                            f"{source_file.name}:{node.lineno} assigns to '{target.id}' "
                            f"— possible hardcoded secret"
                        )


class TestDeadCode:
    def test_no_unused_public_functions(self):
        all_files = list(SOURCE_FILES) + list(TEST_FILES)
        corpus = "\n".join(f.read_text() for f in all_files)

        dead = []
        for source_file in SOURCE_FILES:
            module = source_file.stem
            if module == "__init__":
                continue
            tree = _parse_module(source_file)
            source_text = source_file.read_text()
            other_corpus = corpus.replace(source_text, "")

            for node in ast.iter_child_nodes(tree):
                if not isinstance(node, ast.FunctionDef | ast.ClassDef):
                    continue
                name = node.name
                if name.startswith("_") or name == "main":
                    continue
                if name not in other_corpus:
                    dead.append(f"{module}.py:{name}")

        assert not dead, f"Unused public functions/classes: {', '.join(dead)}"


class TestTestDiscipline:
    def test_every_source_module_has_a_test_file(self):
        source_modules = {p.stem for p in SOURCE_FILES if p.stem != "__init__"}
        test_modules = {p.stem.removeprefix("test_") for p in TEST_FILES}
        untested = source_modules - test_modules

        assert not untested, f"Source modules without test files: {untested}"

    @pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.stem)
    def test_test_classes_start_with_test(self, test_file):
        bad = [
            node.name
            for node in ast.walk(_parse_module(test_file))
            if isinstance(node, ast.ClassDef) and not node.name.startswith("Test")
        ]

        assert not bad, f"{test_file.name} has classes not starting with 'Test': {', '.join(bad)}"

    @pytest.mark.parametrize("test_file", TEST_FILES, ids=lambda p: p.stem)
    def test_test_files_do_not_import_from_other_test_files(self, test_file):
        for node in ast.walk(_parse_module(test_file)):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("tests.test_")
            ):
                pytest.fail(
                    f"{test_file.name} imports from {node.module} "
                    f"— test files should be independent"
                )
