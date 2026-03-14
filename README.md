# iterative-improve

Iterative code improvement loop using Claude and GitHub Actions CI.

Runs `/simplify` and `/review` passes on your feature branch, commits fixes, pushes, and monitors CI. Repeats until the code converges (no more changes) or the iteration limit is reached.

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

## Requirements

- `claude` CLI (Claude Code)
- `gh` CLI (GitHub)
- `git`

## Usage

```bash
# Default: simplify + CI, review + CI per iteration
iterative-improve -n 5

# Batch: simplify + review, then CI once (faster)
iterative-improve -n 5 --batch

# Resume after Ctrl+C
iterative-improve -n 5 --batch --resume

# Skip CI (local-only)
iterative-improve -n 3 --skip-ci
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `-n` | 10 | Number of iterations |
| `--batch` | off | Run simplify+review then CI once per iteration |
| `--resume` | off | Resume from saved state after interruption |
| `--skip-ci` | off | Skip CI checks |
| `--ci-timeout` | 15 | CI timeout in minutes |

## How it works

Each iteration:
1. Sync with main (merge, resolve conflicts if needed)
2. Run `/simplify` (Claude finds and fixes code duplication, complexity)
3. Run `/review` (Claude finds and fixes bugs, security, performance)
4. Commit, push, wait for CI
5. If CI fails, Claude fixes and retries (up to 5 times)
6. Stop when no changes are found in both phases

State is saved to `.improve-loop/state.json` and logs to `.improve-loop/run.log`.
