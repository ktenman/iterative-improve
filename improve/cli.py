from __future__ import annotations

import argparse
import logging
import sys
import threading
from datetime import datetime

from improve import color, git
from improve.ci_glab import GitLabCI
from improve.config import Config
from improve.mode import Mode
from improve.phases import AVAILABLE_PHASES
from improve.platform import Platform
from improve.process import require_tools, run_preflight
from improve.runner import IterationLoop
from improve.state import LOG_FILE, STATE_DIR, LoopState
from improve.version import check_for_update, get_installed_version

logger = logging.getLogger("improve")


def _setup_logging() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(color.ColorFormatter("  %(asctime)s [%(message)s", datefmt="%H:%M:%S"))

    file_handler = logging.FileHandler(LOG_FILE, mode="a")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )

    logger.addHandler(console)
    logger.addHandler(file_handler)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Iterative code improvement loop using Claude and CI"
    )
    parser.add_argument(
        "-n", "--iterations", type=int, default=None, help="Max iterations (omit for continuous)"
    )
    parser.add_argument("--ci-timeout", type=int, default=15, help="CI timeout in minutes")
    parser.add_argument("--skip-ci", action="store_true")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--batch",
        action="store_true",
        help="Run all phases then check CI once per iteration",
    )
    mode_group.add_argument(
        "--parallel",
        action="store_true",
        help="Run phases in parallel using git worktrees",
    )
    parser.add_argument("--resume", action="store_true", help="Resume from saved state")
    parser.add_argument(
        "--phases",
        default=",".join(AVAILABLE_PHASES),
        help="Comma-separated phases to run (default: %(default)s)",
    )
    parser.add_argument(
        "--squash",
        action="store_true",
        help="Squash all branch commits into one after finishing",
    )
    parser.add_argument(
        "--ci-provider",
        choices=["github", "gitlab"],
        default=None,
        help="CI provider (default: auto-detect from git remote)",
    )
    parser.add_argument(
        "--phase-timeout",
        type=int,
        default=900,
        help="Phase timeout in seconds (default: %(default)s)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable colored output")
    return parser.parse_args()


def _validate_phases(raw: str) -> list[str]:
    phases = [stripped for p in raw.split(",") if (stripped := p.strip())]
    if not phases:
        logger.error("loop] No phases specified (available: %s)", ", ".join(AVAILABLE_PHASES))
        sys.exit(1)
    invalid = [p for p in phases if p not in AVAILABLE_PHASES]
    if invalid:
        logger.error(
            "loop] Unknown phase(s): %s (available: %s)",
            ", ".join(invalid),
            ", ".join(AVAILABLE_PHASES),
        )
        sys.exit(1)
    return phases


def main() -> None:
    args = _parse_args()
    color.init(force_no_color=args.no_color)
    _setup_logging()
    threading.Thread(target=check_for_update, daemon=True).start()
    platform = Platform(args.ci_provider) if args.ci_provider else git.detect_platform()
    ci_tool = "glab" if platform == Platform.GITLAB else "gh"
    require_tools(ci_tool)

    if args.iterations is not None and args.iterations < 1:
        logger.error("loop] Iterations must be at least 1")
        sys.exit(1)
    if args.ci_timeout < 1:
        logger.error("loop] CI timeout must be at least 1 minute")
        sys.exit(1)
    if args.phase_timeout < 30:
        logger.error("loop] Phase timeout must be at least 30 seconds")
        sys.exit(1)
    config = Config(
        claude_timeout=args.phase_timeout,
        ci_timeout=args.ci_timeout * 60,
    )
    if platform == Platform.GITLAB:
        config.ci_provider = GitLabCI()
    phases = _validate_phases(args.phases)

    current_branch = git.branch()
    if not current_branch:
        logger.error("loop] Not on a branch (detached HEAD?), switch to a feature branch")
        sys.exit(1)
    if current_branch in ("main", "master"):
        logger.error("loop] Cannot run on main/master, switch to a feature branch")
        sys.exit(1)

    if not git.resolve_existing_conflicts():
        logger.error("loop] Unresolved merge conflicts — please resolve manually and retry")
        sys.exit(1)

    run_preflight(current_branch, ci_tool, args.skip_ci)

    continuous = args.iterations is None
    max_iterations = 1000 if continuous else args.iterations
    start_iteration = 1
    state = LoopState(branch=current_branch, started_at=datetime.now().isoformat())
    if args.resume:
        prev = LoopState.load()
        if prev and prev.branch == current_branch:
            state = prev
            start_iteration = prev.iteration + 1
            logger.info(
                "loop] Resumed from iteration %d (%d previous results)",
                prev.iteration,
                len(prev.results),
            )
        else:
            logger.info("loop] No matching state to resume, starting fresh")

    if args.parallel:
        mode = Mode.PARALLEL
    elif args.batch:
        mode = Mode.BATCH
    else:
        mode = Mode.SEQUENTIAL

    loop = IterationLoop(
        state=state,
        skip_ci=args.skip_ci,
        mode=mode,
        phases=phases,
        config=config,
        squash=args.squash,
        continuous=continuous,
    )
    loop.install_signal_handlers()
    iter_display = "continuous" if continuous else f"{start_iteration}-{max_iterations}"
    border = color.separator()
    header = (
        f"\n{border}\n"
        f"  Iterative Improvement Loop v{get_installed_version()}\n"
        f"  Branch:     {color.wrap(current_branch, color.BOLD_WHITE)}\n"
        f"  Iterations: {iter_display}\n"
        f"  Phases:     {', '.join(phases)}\n"
        f"  Mode:       {mode.value}\n"
        f"  CI:         {'skip' if args.skip_ci else f'{args.ci_timeout}m timeout'}\n"
        f"  Squash:     {'yes' if args.squash else 'no'}\n"
        f"{border}"
    )
    print(header)
    logger.info(
        "loop] Started: branch=%s iterations=%s phases=%s mode=%s skip_ci=%s",
        current_branch,
        iter_display,
        ",".join(phases),
        mode.value,
        args.skip_ci,
    )

    if not git.sync_with_main(current_branch):
        logger.error("loop] Cannot sync with main, aborting")
        sys.exit(1)

    loop.run(start_iteration, max_iterations)
