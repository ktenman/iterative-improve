from __future__ import annotations

import argparse
import logging
import sys
import threading
from datetime import datetime

from improve import ci, git
from improve.ci_gitlab import GitLabCI
from improve.loop import IterationLoop
from improve.process import require_tools
from improve.prompt import AVAILABLE_PHASES
from improve.state import LOG_FILE, STATE_DIR, LoopState
from improve.version import check_for_update, get_installed_version

logger = logging.getLogger("improve")


def _setup_logging() -> None:
    STATE_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("  %(asctime)s [%(message)s", datefmt="%H:%M:%S"))

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
    parser.add_argument("-n", "--iterations", type=int, default=10)
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
    _setup_logging()
    threading.Thread(target=check_for_update, daemon=True).start()
    platform = args.ci_provider or git.detect_platform()
    if platform == "gitlab":
        ci.set_provider(GitLabCI())
    ci_tool = "glab" if platform == "gitlab" else "gh"
    require_tools(ci_tool)

    if args.iterations < 1:
        logger.error("loop] Iterations must be at least 1")
        sys.exit(1)
    if args.ci_timeout < 1:
        logger.error("loop] CI timeout must be at least 1 minute")
        sys.exit(1)
    ci.set_timeout(args.ci_timeout)
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

    loop = IterationLoop(
        state=state,
        skip_ci=args.skip_ci,
        batch=args.batch,
        phases=phases,
        squash=args.squash,
        parallel=args.parallel,
    )
    loop.install_signal_handlers()

    mode = "parallel" if args.parallel else ("batch" if args.batch else "sequential")
    header = (
        f"\n{'=' * 50}\n"
        f"  Iterative Improvement Loop v{get_installed_version()}\n"
        f"  Branch:     {current_branch}\n"
        f"  Iterations: {start_iteration}-{args.iterations}\n"
        f"  Phases:     {', '.join(phases)}\n"
        f"  Mode:       {mode}\n"
        f"  CI:         {'skip' if args.skip_ci else f'{args.ci_timeout}m timeout'}\n"
        f"  Squash:     {'yes' if args.squash else 'no'}\n"
        f"{'=' * 50}"
    )
    print(header)
    logger.info(
        "loop] Started: branch=%s iterations=%d-%d phases=%s mode=%s skip_ci=%s squash=%s",
        current_branch,
        start_iteration,
        args.iterations,
        ",".join(phases),
        mode,
        args.skip_ci,
        args.squash,
    )

    if not git.sync_with_main(current_branch):
        logger.error("loop] Cannot sync with main, aborting")
        sys.exit(1)

    loop.run(start_iteration, args.iterations)
