# CI Workflow Discovery & GitLab Watch Improvement

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hard-coded `CI_WORKFLOW = "CI"` with auto-discovery, and document the GitLab polling limitation.

**Architecture:** Add `discover_workflow()` to `GitHubCI` that queries `gh workflow list` to find the CI workflow by name heuristic, with CLI override via `--ci-workflow`. GitLab polling is inherent to `glab` (no native blocking watch) so we improve it with adaptive polling.

**Tech Stack:** Python stdlib, `gh` CLI, `glab` CLI

---

### Task 1: Auto-discover GitHub CI workflow name

**Files:**
- Modify: `improve/ci_gh.py:11-31` (remove hard-coded constant, add discovery)
- Modify: `improve/ci.py:32-38` (add `discover_workflow` to CIProvider protocol)
- Modify: `improve/ci_glab.py:22` (add no-op `discover_workflow` for GitLab)
- Modify: `improve/cli.py:41-85` (add `--ci-workflow` arg)
- Modify: `improve/config.py` (add `ci_workflow` field)
- Test: `tests/test_ci_gh.py`
- Test: `tests/test_ci_glab.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests for workflow discovery**
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement `discover_workflow` in GitHubCI**
- [ ] **Step 4: Add `--ci-workflow` CLI argument**
- [ ] **Step 5: Wire discovery into `get_latest_run_id`**
- [ ] **Step 6: Add no-op for GitLab**
- [ ] **Step 7: Run full test suite + lint**
- [ ] **Step 8: Commit**

### Task 2: Improve GitLab CI polling with progress logging

**Files:**
- Modify: `improve/ci_glab.py:47-57` (add progress logging during poll)
- Test: `tests/test_ci_glab.py`

- [ ] **Step 1: Write failing test for poll logging**
- [ ] **Step 2: Implement progress logging in watch_run**
- [ ] **Step 3: Run full test suite + lint**
- [ ] **Step 4: Commit**
