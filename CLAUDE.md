# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CLI tool that runs iterative code improvement loops on feature branches using Claude Code and GitHub Actions CI. Each iteration runs configurable phases (`simplify`, `review`, `security`), commits fixes, pushes, and monitors CI until the code converges or the iteration limit is reached.

## Commands

```bash
# Install locally
uv tool install .

# Run the tool (must be on a feature branch, not main/master)
iterative-improve                           # runs continuously until convergence
iterative-improve -n 5                      # cap at 5 iterations
iterative-improve --batch                   # all phases then CI once (faster)
iterative-improve --parallel                # phases in parallel via git worktrees (fastest)
iterative-improve --resume                  # resume after interruption
iterative-improve --skip-ci                 # skip CI checks
iterative-improve --phases simplify,review  # specific phases only
iterative-improve --squash                  # squash commits when done
iterative-improve --revert-on-fail          # revert bad changes, keep going
iterative-improve --phase-timeout 300       # Claude subprocess timeout (default: 900s)

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Test with coverage
uv run pytest -v --tb=short --cov=improve --cov-report=term-missing

# Release: bump version in pyproject.toml and merge to main (auto-tagged)
```

## Architecture

All source is in `src/improve/`. Entry point: `improve.cli:main`.

- **cli.py** — Argument parsing, logging setup, phase validation, entry point
- **loop.py** — `IterationLoop` class: orchestration, signal handling (SIGINT/SIGTERM save state before exit), phase execution, batch/sequential/parallel iteration, squash, results summary
- **parallel.py** — Parallel phase execution using git worktrees and `ThreadPoolExecutor`; each phase runs `claude -p` in its own worktree, changes are merged back and committed as one
- **claude.py** — Spawns `claude -p` as subprocess with `stream-json` output format, parses streaming events, tracks active processes (thread-safe) for graceful shutdown
- **ci.py** — Polls GitHub Actions via `gh run list/watch`, waits for new CI runs by comparing run IDs, fetches failed logs with `gh run view --log-failed`
- **git.py** — Git operations: diff vs main, commit+push, sync/merge with main, squash branch, conflict resolution, worktree management (create/remove/apply changes)
- **process.py** — Thin `subprocess.run` wrapper, validates required external tools (`git`, `claude`, `gh`)
- **prompt.py** — Defines available phases (simplify/review/security) and their focus areas; builds prompts for phases and CI fix attempts; extracts `SUMMARY:` lines from Claude output; generates commit messages with verb detection
- **state.py** — `LoopState` and `PhaseResult` dataclasses, JSON persistence to `.improve-loop/state.json`
- **version.py** — Checks GitHub releases for newer versions, runs in background thread at startup

## Key Design Details

- No external Python dependencies — stdlib only
- Requires Python >= 3.10
- External tools required at runtime: `git`, `claude` (Claude Code CLI), `gh` (GitHub CLI)
- State persists to `.improve-loop/` directory (state.json + run.log)
- Three phases available: `simplify`, `review`, `security` — configurable via `--phases`
- Runs continuously by default (until convergence); use `-n` to cap iterations
- Claude subprocess timeout: configurable via `--phase-timeout` (default 900s)
- CI run timeout: configurable via `--ci-timeout` (default 15 min)
- CI fix retries capped at 5 attempts per phase
- `--revert-on-fail` reverts changes that fail CI (`git reset --hard` + force push) and continues
- Crash recovery: phase exceptions are caught, working tree is cleaned, loop continues
- `--batch` and `--parallel` are mutually exclusive (argparse enforced)
- `--parallel` runs all phases concurrently in git worktrees, merges changes, single commit+push
- `--squash` squashes all branch commits into one via `git reset --soft` + force push
- CI waits settle on run IDs (3 checks, 5s apart) to handle rapid re-triggers
- Cancelled CI runs are automatically retried (up to 3 times)
- Ruff configured with line-length=100, target py310

## Code Standards

- No comments — write self-documenting code with clear, descriptive naming
- Use guard clauses to exit early and reduce nesting
- Keep methods small with a single responsibility (< 20 lines ideal)
- Prefer immutability — avoid mutating shared state
- Keep files under 300 lines; refactor if exceeding 400

## Python Guidelines

- Type hints for all function signatures
- Use `logging` module, not print statements (except intentional user output)
- f-strings for formatting
- Use dataclasses for data structures
- Guard clauses over nested conditionals
- **Always run `uv run ruff check src/` and `uv run ruff format --check src/`** after making changes — fix all issues before committing
- Auto-fix: `uv run ruff check --fix src/` and `uv run ruff format src/`

## Testing Standards

- Every bug must be reproduced by a unit test before being fixed
- Test names should be full English sentences stating what is being tested
- Use Arrange/Act/Assert structure
- Each test should verify one specific behavior
- Tests must not rely on network access or default configurations

## Git Commit Conventions

- Start with uppercase imperative verb (e.g., "Add", "Fix", "Update", "Remove")
- **No prefixes** — never use `feat:`, `fix:`, `chore:`, etc.
- Subject line max 50 characters
- **Never** add "Co-Authored-By: Claude" or any AI attribution
- **Never** add "Generated with Claude Code" to commits or PRs
- Good: `Add resume support for interrupted loops`
- Bad: `feat: add resume support`

## Development Philosophy

Do what has been asked; nothing more, nothing less. Always prefer editing an existing file to creating a new one. Never proactively create documentation files unless explicitly requested.
