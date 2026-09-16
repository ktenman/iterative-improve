# iterative-improve

[![CI](https://github.com/ktenman/iterative-improve/actions/workflows/ci.yml/badge.svg)](https://github.com/ktenman/iterative-improve/actions/workflows/ci.yml)
[![CodeQL](https://github.com/ktenman/iterative-improve/actions/workflows/codeql.yml/badge.svg)](https://github.com/ktenman/iterative-improve/actions/workflows/codeql.yml)

Automate the feedback loop between Claude Code and your CI pipeline.

Researchers tweak parameters in small steps until results improve. Same idea, but for code. One-shot Claude is hit or miss, so `iterative-improve` puts it in a loop. It runs your branch through multiple passes (cleanup, review, security), commits fixes, and waits for CI. Build breaks? Error logs go back to Claude for another try. One command, walk away, green build.

The approach combines iterative self-refinement ([Self-Refine](https://arxiv.org/abs/2303.17651), [Reflexion](https://arxiv.org/abs/2303.11366)) with a [quality ratchet](https://leaddev.com/software-quality/introducing-quality-ratchets-tool-managing-complex-systems) — the LLM drives improvements, CI prevents regressions. More in [Background & References](#background--references).

## Install

```bash
uv tool install git+https://github.com/ktenman/iterative-improve
```

Or clone and install locally:

```bash
git clone https://github.com/ktenman/iterative-improve
cd iterative-improve
uv tool install .
```

## Update

```bash
uv tool upgrade iterative-improve
```

## Requirements

- Python >= 3.10
- [`uv`](https://docs.astral.sh/uv/) (package manager)
- [`claude`](https://claude.ai/code) CLI (Claude Code), logged in, with access to [Claude Opus with 1M context](https://code.claude.com/docs/en/model-config#extended-context-with-1m) (included on Max, Team and Enterprise plans; Pro plans need usage credits)
- [`gh`](https://cli.github.com/) CLI (GitHub) or [`glab`](https://gitlab.com/gitlab-org/cli) CLI (GitLab), logged in
- `git`, with an `origin` remote you can push to

The repository's base branch must be called `main`: the tool compares your branch with it, merges `origin/main` into your branch, and squashes onto it.

## Usage

Run it from the root of the repository, on the feature branch you want to improve:

```bash
cd path/to/repo
git switch my-feature     # not main/master, and the working tree must be clean
iterative-improve -n 3
```

More examples:

```bash
# Default: run until an iteration changes nothing (all phases)
iterative-improve

# Cap at 5 iterations
iterative-improve -n 5

# Batch mode: run all phases, then CI once (faster)
iterative-improve -n 5 --batch

# Parallel mode: run phases concurrently in git worktrees (fastest)
iterative-improve -n 5 --parallel

# Run specific phases only
iterative-improve -n 5 --phases simplify,review
iterative-improve -n 5 --phases security

# Squash all branch commits into one when done
iterative-improve -n 5 --squash

# Resume after Ctrl+C (pass the same flags again)
iterative-improve -n 5 --batch --resume

# Don't wait for CI (commits are still pushed)
iterative-improve -n 3 --skip-ci

# Use GitLab CI instead of GitHub Actions
iterative-improve -n 5 --ci-provider gitlab

# Watch a specific GitHub Actions workflow
iterative-improve -n 5 --ci-workflow "Build and Test"

# Adjust timeouts
iterative-improve -n 5 --phase-timeout 300 --ci-timeout 20

# Disable colored output
iterative-improve -n 5 --no-color
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `-n`, `--iterations` | until nothing changes | Max iterations |
| `--phases` | `simplify,review,security` | Comma-separated phases to run, in this order |
| `--batch` | off | Run all phases, then check CI once per iteration. Can't be combined with `--parallel` |
| `--parallel` | off | Run all phases at the same time in git worktrees, then check CI once. Can't be combined with `--batch` |
| `--squash` | off | Squash all branch commits into one after finishing (force-pushes) |
| `--resume` | off | Continue from saved state after an interruption |
| `--skip-ci` | off | Don't wait for CI and skip the `gh`/`glab` login checks. Commits are still pushed |
| `--ci-timeout` | 15 | Minutes to wait for a CI run to finish (minimum 1) |
| `--ci-provider` | auto-detect | `github` or `gitlab`. Auto-detect picks `gitlab` if the `origin` URL contains "gitlab", otherwise `github` |
| `--ci-workflow` | auto-detect | GitHub Actions workflow to watch. Auto-detect looks for an active workflow named `ci`, then `build`, `test`, `tests`, `pipeline` (any case). If none matches, it watches the latest run of any workflow |
| `--phase-timeout` | 900 | Seconds before a Claude session is killed (minimum 30). Merge-conflict resolution always uses 900 |
| `--no-color` | off | Disable colored output (also respects `NO_COLOR` env var) |

## How Claude is run

Every Claude call starts a fresh, non-interactive Claude Code session. That covers each phase, each CI fix, merge-conflict resolution, and the `--squash` commit message:

```bash
claude -p --output-format stream-json --verbose --include-partial-messages \
  --dangerously-skip-permissions --model 'opus[1m]' --effort max
```

- **Model `opus[1m]`**: the newest Claude Opus with the 1M-token context window.
- **Effort `max`**: the highest reasoning effort, which also uses the most tokens.
- **Overrides**: both flags take precedence over your own choices (`/model` and the `model` and `effortLevel` settings). The exception is the `CLAUDE_CODE_EFFORT_LEVEL` environment variable, which replaces `--effort max` if it's set. Limits still apply: a `maxEffortLevel` setting caps the effort, and an organization's model allowlist can block the model.
- **No permission prompts**: `--dangerously-skip-permissions` lets Claude edit files and run any command in your repo without asking. Only run the tool on code you trust.
- **Your Claude Code setup still applies**: your login, your `CLAUDE.md` files and the rest of your Claude Code configuration. Put project rules in `CLAUDE.md`.
- **Output**: the prompt is sent on stdin. Claude's reply is streamed to your terminal (except in `--parallel` mode and for the squash message), and each tool call is logged as one line.

## Phases

| Phase | What Claude is asked to do |
|-------|----------------------------|
| **simplify** | Make the changed code simpler without changing its behavior: less nesting and duplication, clearer names, the conventions in `CLAUDE.md` |
| **review** | Fix real bugs (logic errors, null handling, race conditions, leaks, performance problems) and `CLAUDE.md` violations |
| **security** | Fix security holes (injection, auth flaws, exposed secrets, insecure deserialization, path traversal, vulnerable dependencies) and error-handling problems (silent failures, catch blocks that are too broad) |

Each phase is given the names of the files your branch changed compared with `main`, plus summaries of what earlier iterations already fixed. Claude edits the files directly, runs the project's lint/format/test commands where appropriate, and ends with a one-line summary that becomes the commit message. `review` and `security` only fix issues they're highly confident about, and they ignore problems that existed before your changes. The full prompts are in [`improve/phases.py`](improve/phases.py).

## How it works

Sequential mode (the default):

```mermaid
flowchart LR
    S[Sync with main] --> P[Run next phase]
    P -->|files changed| C[Commit, push, wait for CI]
    P -->|no changes| L{Last phase?}
    C -->|green| L
    C -->|red| F[Claude fixes from CI logs] --> C
    C -->|still red after last fix| X([Stop])
    L -->|no| P
    L -->|yes, something changed| S
    L -->|yes, nothing changed| D([Done])
```

Before the first iteration, the tool checks that:

- `git`, `claude` and `gh`/`glab` are installed
- you're on a branch other than `main`/`master`
- there are no unresolved merge conflicts (if there are, Claude tries to resolve and commit them)
- the working tree is clean (`.improve-loop/` doesn't count)
- the remote is reachable and you can push to it
- `gh`/`glab` is logged in and can see the repository (skipped with `--skip-ci`)

In the background, it also checks GitHub for a newer release and prints an upgrade hint if there is one.

Each iteration:

1. **Sync**: fetches `origin/main`. If your branch is behind, the tool merges `origin/main` into it and pushes. If the merge conflicts, Claude resolves the conflicts; if that fails, the loop stops.
2. **Phases**: runs each active phase. If Claude changed files, only those files are committed and pushed.
3. **CI**: waits for the new CI run and watches it for up to `--ci-timeout` minutes.
   - If no new run appears within 3 minutes, the tool assumes the push didn't trigger CI and moves on.
   - If the run was cancelled (for example, replaced by a newer push), the tool follows the newer run, up to 3 times.
   - If the run fails or times out, the end of the failure logs goes to Claude, with values like `API_TOKEN=...` redacted. Claude's fix is committed and pushed, and CI runs again, for up to 5 attempts.
4. **Stop or continue**:
   - If CI still fails after Claude's last fix attempt, or a push fails, the loop stops.
   - A phase that changed nothing is dropped from later iterations.
   - A phase that crashes is logged, its changes to tracked files are discarded, and it runs again next iteration.
   - The loop ends when an iteration changes nothing or the `-n` limit is reached. It then prints a summary of the results.

### Modes

- **Sequential (default)**: each phase is committed, pushed and checked by CI before the next phase starts.
- **`--batch`**: phases run one after another and each one still commits and pushes, but CI is checked only once, on the newest run after the last phase.
- **`--parallel`**: all active phases run at the same time, each in its own temporary git worktree. Changed files are copied back into your working tree and committed as one commit, then CI is checked once. If two phases edit the same file, the later phase in `--phases` order wins and a warning is logged.

### Squash

With `--squash`, once the loop finishes, Claude writes one commit message from the phase summaries. The branch is then soft-reset to its merge base with `origin/main`, committed as a single commit, and force-pushed with `--force-with-lease`. This squashes **every** commit on the branch, including your own. Squashing doesn't run if you stop the loop with Ctrl+C.

### State and resume

- `.improve-loop/state.json` holds the branch, the current iteration and every phase result.
- `.improve-loop/run.log` is a detailed log that includes every git and CI command the tool ran.
- Ctrl+C (or SIGTERM) stops Claude, saves the state, prints the summary and exits.
- `--resume` continues with the next iteration if the saved state belongs to the current branch. Pass the same flags as before.

Add `.improve-loop/` to your `.gitignore` if you don't want it to show up in `git status`.

## Background & References

This tool combines two ideas: **iterative self-refinement** and a **quality ratchet**. The LLM critiques its own work and proposes fixes (self-refinement), while CI acts as a ratchet that prevents regressions — improvements accumulate and never slip back. Real error logs drive fixes (not blind retries), and it loops until CI goes green.

### Autonomous AI Research

- [autoresearch](https://github.com/karpathy/autoresearch) (Karpathy, 2025): autonomous AI research on a single GPU — an agent modifies code, trains for a fixed time budget, evaluates against a single metric, keeps improvements, discards regressions, and iterates overnight. Several features in `iterative-improve` were directly inspired by this pattern: continuous mode (fire-and-forget), crash recovery (phase failures don't kill the run), and configurable timeouts

### Quality Ratchet Pattern

- [Introducing Quality Ratchets](https://leaddev.com/software-quality/introducing-quality-ratchets-tool-managing-complex-systems) (Ball, LeadDev): the ratchet as a tool for managing complex systems — fully automated (linters, type systems), semi-automated (tests), and process-based ratchets
- [Ratchets in Software Development](https://qntm.org/ratchet) (qntm): things that are fixed stay fixed, improvements accumulate over time

### Iterative Self-Refinement

- [Self-Refine](https://arxiv.org/abs/2303.17651) (Madaan et al., 2023): LLMs critique and revise their own output in loops — ~20% average improvement across tasks
- [Reflexion](https://arxiv.org/abs/2303.11366) (Shinn et al., 2023): verbal feedback from failures drives better retries
- [LLMLOOP](https://valerio-terragni.github.io/assets/pdf/ravi-icsme-2025.pdf) (Ravi et al., ICSME 2025): iterative feedback loops for improving LLM-generated Java code

### Automated Program Repair

- [Automated Program Repair](https://doi.org/10.1145/3318162) (Le Goues et al., 2019): the broader field this builds on
- [RepairAgent](https://arxiv.org/abs/2403.17134) (Bouzenia et al., 2024): autonomous LLM-based agent that plans and executes repair actions — fixed 164 bugs on Defects4J including 39 not fixed by prior techniques
- [Code Repair with LLMs gives an Exploration-Exploitation Tradeoff](https://proceedings.neurips.cc/paper_files/paper/2024/file/d5c56ec4f69c9a473089b16000d3f8cd-Paper-Conference.pdf) (Tang et al., NeurIPS 2024): formalizes the tradeoff between exploring diverse fixes and exploiting known patterns
- [RePair: Automated Program Repair with Process-based Feedback](https://aclanthology.org/2024.findings-acl.973.pdf) (ACL Findings 2024): iterative refinement using compiler and test feedback until convergence

## Beyond Code: ML & Research Workflows

The iterate → evaluate → refine loop isn't specific to code quality. The same pattern works anywhere you have a measurable objective and tunable inputs:

| Domain | Tweak | Verify | Refine |
|--------|-------|--------|--------|
| **Code quality** (this tool) | Run simplify/review/security phases | Push and wait for CI | Feed error logs back to Claude |
| **ML training** ([autoresearch](https://github.com/karpathy/autoresearch)) | Modify model architecture, optimizer, hyperparams | Train for 5 min, check val_bpb | Keep improvements, discard regressions, iterate overnight |
| **ML hyperparameter tuning** | Adjust learning rate, batch size, architecture | Run training, check validation metrics | Let the LLM propose next parameter set based on results |
| **Research experimentation** | Change experimental setup or variables | Run experiment, collect measurements | Analyze outcomes, hypothesize next change |
| **Prompt engineering** | Rewrite system prompt or few-shot examples | Evaluate on test suite | Feed failure cases back for revision |
| **Data pipeline tuning** | Modify transforms, filters, feature engineering | Run pipeline, check output quality metrics | Diagnose regressions from diff |

The core idea: replace manual trial-and-error with a structured loop where an LLM proposes changes, an automated check scores them, and the results feed back in. `iterative-improve` implements this for code + CI. Adapting it to other domains means swapping the "phase" prompts and the "verify" step — the orchestration loop stays the same.

## Architecture

```
improve/
├── cli.py         Argument parsing, logging setup, startup checks, entry point
├── config.py      Config dataclass for runtime settings
├── mode.py        Mode enum (sequential, batch, parallel)
├── platform.py    Platform enum (github, gitlab)
├── runner.py      IterationLoop: orchestration, signal handling, phase execution
├── parallel.py    Parallel phase execution using git worktrees
├── claude.py      Claude subprocess with streaming JSON output
├── ci.py          CI orchestration: polling, retries, provider abstraction
├── ci_gh.py       GitHub Actions CI provider (gh CLI)
├── ci_glab.py     GitLab CI provider (glab CLI)
├── git.py         Git operations: diff, commit, push, sync, squash, worktrees, conflict resolution
├── process.py     Subprocess wrapper, tool validation, preflight checks
├── phases.py      Phase prompts, summary extraction, commit messages
├── state.py       LoopState/PhaseResult dataclasses, JSON persistence
├── color.py       ANSI color support for terminal output
└── version.py     Update checker (background thread at startup)
```

## Development

```bash
# Install dev dependencies
uv sync --dev

# Lint
uv run ruff check improve/ tests/
uv run ruff format --check improve/ tests/

# Test with coverage
uv run pytest -v --tb=short --cov=improve --cov-report=term-missing

# Auto-fix lint issues
uv run ruff check --fix improve/ && uv run ruff format improve/
```

## Releasing

Merge to `main`. The release workflow auto-bumps the patch version, creates a git tag, and publishes a GitHub release.

## License

[Creative Commons Attribution 4.0 International (CC BY 4.0)](LICENSE)
