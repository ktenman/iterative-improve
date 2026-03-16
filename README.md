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
- [`claude`](https://claude.ai/code) CLI (Claude Code)
- [`gh`](https://cli.github.com/) CLI (GitHub)
- `git`

## Usage

```bash
# Default: run all phases (simplify + review + security)
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

# Resume after Ctrl+C
iterative-improve -n 5 --batch --resume

# Skip CI checks (local-only)
iterative-improve -n 3 --skip-ci
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `-n` | 10 | Number of iterations |
| `--phases` | `simplify,review,security` | Comma-separated phases to run |
| `--batch` | off | Run all phases then CI once per iteration (mutually exclusive with `--parallel`) |
| `--parallel` | off | Run phases concurrently in git worktrees (mutually exclusive with `--batch`) |
| `--squash` | off | Squash all branch commits into one after finishing |
| `--resume` | off | Resume from saved state after interruption |
| `--skip-ci` | off | Skip CI checks |
| `--ci-timeout` | 15 | CI timeout in minutes |

## Phases

| Phase | What it does |
|-------|-------------|
| **simplify** | Finds duplicated code, overly complex logic, inefficient patterns, dead code |
| **review** | Finds bugs, performance issues, missing edge cases, API contract violations |
| **security** | Finds injection attacks, auth flaws, data exposure, path traversal, dependency issues |

## How it works

```mermaid
flowchart LR
    S[Sync] --> P[Run Phase] --> C{Changes?}
    C -->|Yes| CI[Push + CI] -->|Pass| P
    CI -->|Fail| F[Auto-fix] --> CI
    C -->|No| D([Done])
```

Each iteration:

1. **Sync**: pulls latest `main`, auto-merges if needed
2. **Analyze**: Claude looks at the branch diff. Runs whatever phases you configured
3. **Push**: stages fixes, writes a commit message, pushes
4. **CI check**: sits and waits for GitHub Actions. If the build breaks, it grabs the logs and retries (up to 5 times)
5. **Done**: bails out once nothing changes

In **batch mode**, all phases run first, then CI checks once per iteration (faster for branches with many changes).

In **parallel mode**, each phase runs in its own git worktree simultaneously via `ThreadPoolExecutor`. Changes are merged back and committed as one. This is the fastest mode — 3x faster than sequential when running all 3 phases.

With **`--squash`**, all commits on the branch are squashed into a single commit with a summary after iterations complete.

All state lives in `.improve-loop/` (state.json + run.log). Hit Ctrl+C and pick up where you left off with `--resume`.

## Background & References

This tool combines two ideas: **iterative self-refinement** and a **quality ratchet**. The LLM critiques its own work and proposes fixes (self-refinement), while CI acts as a ratchet that prevents regressions — improvements accumulate and never slip back. Real error logs drive fixes (not blind retries), and it loops until CI goes green.

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
| **ML hyperparameter tuning** | Adjust learning rate, batch size, architecture | Run training, check validation metrics | Let the LLM propose next parameter set based on results |
| **Research experimentation** | Change experimental setup or variables | Run experiment, collect measurements | Analyze outcomes, hypothesize next change |
| **Prompt engineering** | Rewrite system prompt or few-shot examples | Evaluate on test suite | Feed failure cases back for revision |
| **Data pipeline tuning** | Modify transforms, filters, feature engineering | Run pipeline, check output quality metrics | Diagnose regressions from diff |

The core idea: replace manual trial-and-error with a structured loop where an LLM proposes changes, an automated check scores them, and the results feed back in. `iterative-improve` implements this for code + CI. Adapting it to other domains means swapping the "phase" prompts and the "verify" step — the orchestration loop stays the same.

## Architecture

```
src/improve/
├── cli.py      Argument parsing, logging setup, entry point
├── loop.py     IterationLoop class: orchestration, signal handling, phase execution
├── parallel.py Parallel phase execution using git worktrees
├── claude.py   Claude subprocess with streaming JSON output
├── ci.py       GitHub Actions polling via gh CLI
├── git.py      Git operations: diff, commit, push, sync, squash, worktrees, conflict resolution
├── process.py  Subprocess wrapper, tool validation
├── prompt.py   Phase prompts, summary extraction, commit messages
├── state.py    LoopState/PhaseResult dataclasses, JSON persistence
└── version.py  Update checker
```

## Development

```bash
# Install dev dependencies
uv sync --dev

# Lint
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/

# Test with coverage
uv run pytest -v --tb=short --cov=improve --cov-report=term-missing

# Auto-fix lint issues
uv run ruff check --fix src/ && uv run ruff format src/
```

## Releasing

Merge to `main`. The release workflow auto-bumps the patch version, creates a git tag, and publishes a GitHub release.

## License

[Creative Commons Attribution 4.0 International (CC BY 4.0)](LICENSE)
