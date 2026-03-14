from __future__ import annotations

import argparse
import logging
import signal
import subprocess
import sys
import time
from datetime import datetime

from improve import claude
from improve import ci
from improve import git
from improve.process import require_tools
from improve.prompt import build_phase_prompt, build_ci_fix_prompt, build_commit_message, extract_summary
from improve.state import LoopState, PhaseResult, STATE_DIR, LOG_FILE

logger = logging.getLogger("improve")

MAX_CI_RETRIES = 5

_state_ref: LoopState | None = None
_loop_start: float = 0.0


def _shutdown(signum, frame):
    logger.info("signal] Caught %s, shutting down...", signal.Signals(signum).name)
    if claude.active_process and claude.active_process.poll() is None:
        logger.info("signal] Terminating active subprocess...")
        claude.active_process.terminate()
        try:
            claude.active_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            claude.active_process.kill()
    if _state_ref:
        _state_ref.save()
        elapsed = time.monotonic() - _loop_start if _loop_start else 0
        _print_summary(_state_ref, elapsed)
    sys.exit(130)


def _setup_logging():
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


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours = int(minutes // 60)
    return f"{hours}h {minutes % 60}m {secs}s"


def _run_phase(phase: str, iteration: int, state: LoopState, skip_ci: bool) -> PhaseResult:
    phase_start = time.monotonic()
    total_claude = 0.0
    total_ci = 0.0
    prompt = build_phase_prompt(phase, git.diff_vs_main(), state.context())

    logger.info("%s] Running %s...", phase, phase)
    output, claude_time = claude.run_claude(prompt)
    total_claude += claude_time

    if not git.has_changes():
        logger.info("%s] No changes", phase)
        elapsed = time.monotonic() - phase_start
        return PhaseResult(iteration, phase, False, [], "No changes needed", True, 0, elapsed, total_claude, 0.0)

    files = git.changed_files()
    summary = extract_summary(output)
    logger.info("%s] Changed %d file(s): %s", phase, len(files), ", ".join(files[:5]))

    commit_msg = build_commit_message(phase, summary)
    if not git.commit_and_push(commit_msg, state.branch):
        elapsed = time.monotonic() - phase_start
        return PhaseResult(iteration, phase, True, files, summary, False, 0, elapsed, total_claude, 0.0)

    if skip_ci:
        elapsed = time.monotonic() - phase_start
        return PhaseResult(iteration, phase, True, files, summary, True, 0, elapsed, total_claude, 0.0)

    ci_passed, ci_errors, ci_time = ci.wait_for_ci(state.branch)
    total_ci += ci_time
    retries = 0

    while not ci_passed and retries < MAX_CI_RETRIES:
        retries += 1
        logger.info("ci-fix] Attempt %d/%d...", retries, MAX_CI_RETRIES)
        _, fix_claude_time = claude.run_claude(build_ci_fix_prompt(ci_errors))
        total_claude += fix_claude_time

        if not git.has_changes():
            logger.info("ci-fix] No fix produced")
            break

        git.commit_and_push(f"Fix CI after {phase} (attempt {retries})", state.branch)
        ci_passed, ci_errors, ci_time = ci.wait_for_ci(state.branch)
        total_ci += ci_time

    elapsed = time.monotonic() - phase_start
    logger.info(
        "%s] Phase done in %s (claude: %s, ci: %s)",
        phase, _format_duration(elapsed), _format_duration(total_claude), _format_duration(total_ci),
    )
    return PhaseResult(iteration, phase, True, files, summary, ci_passed, retries, elapsed, total_claude, total_ci)


def _print_summary(state: LoopState, total_elapsed: float):
    total_changed = sum(1 for r in state.results if r["changes_made"])
    total_ci_fixes = sum(r["ci_retries"] for r in state.results)
    total_claude_time = sum(r.get("claude_seconds", 0) for r in state.results)
    total_ci_time = sum(r.get("ci_seconds", 0) for r in state.results)

    lines = [
        f"\n{'=' * 60}",
        "RESULTS",
        f"{'=' * 60}",
        f"  Phases run:     {len(state.results)}",
        f"  With changes:   {total_changed}",
        f"  CI fixes:       {total_ci_fixes}",
        f"  Total time:     {_format_duration(total_elapsed)}",
        f"  Claude time:    {_format_duration(total_claude_time)}",
        f"  CI time:        {_format_duration(total_ci_time)}",
        f"  Overhead:       {_format_duration(total_elapsed - total_claude_time - total_ci_time)}",
        "",
    ]
    for r in state.results:
        marker = "+" if r["changes_made"] else " "
        ci_status = "PASS" if r["ci_passed"] else "FAIL"
        duration = _format_duration(r.get("duration_seconds", 0))
        lines.append(f"  [{marker}] {r['phase']:10s} | CI:{ci_status} | {duration:>9s} | {r['summary']}")
    lines.append(f"\n  State: {state.STATE_FILE if hasattr(state, 'STATE_FILE') else '.improve-loop/state.json'}")
    lines.append(f"  Log:   {LOG_FILE}")

    summary = "\n".join(lines)
    print(summary)
    logger.debug(summary)


def main():
    global _state_ref, _loop_start

    parser = argparse.ArgumentParser(
        description="Iterative code improvement loop using Claude and CI"
    )
    parser.add_argument("-n", "--iterations", type=int, default=10)
    parser.add_argument("--ci-timeout", type=int, default=15, help="CI timeout in minutes")
    parser.add_argument("--skip-ci", action="store_true")
    parser.add_argument("--batch", action="store_true", help="Run simplify+review then check CI once per iteration")
    parser.add_argument("--resume", action="store_true", help="Resume from saved state")
    args = parser.parse_args()

    _setup_logging()
    require_tools()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    ci.set_timeout(args.ci_timeout)

    current_branch = git.branch()
    if current_branch in ("main", "master"):
        logger.error("loop] Cannot run on main/master, switch to a feature branch")
        sys.exit(1)

    start_iteration = 1
    if args.resume:
        prev = LoopState.load()
        if prev and prev.branch == current_branch:
            state = prev
            start_iteration = prev.iteration + 1
            logger.info("loop] Resumed from iteration %d (%d previous results)", prev.iteration, len(prev.results))
        else:
            logger.info("loop] No matching state to resume, starting fresh")
            state = LoopState(branch=current_branch, started_at=datetime.now().isoformat())
    else:
        state = LoopState(branch=current_branch, started_at=datetime.now().isoformat())

    _state_ref = state

    mode = "batch" if args.batch else "simplify+review"
    header = (
        f"\n{'=' * 50}\n"
        f"  Iterative Improvement Loop\n"
        f"  Branch:     {current_branch}\n"
        f"  Iterations: {start_iteration}-{args.iterations}\n"
        f"  Mode:       {mode}\n"
        f"  CI:         {'skip' if args.skip_ci else f'{args.ci_timeout}m timeout'}\n"
        f"{'=' * 50}"
    )
    print(header)
    logger.info(
        "loop] Started: branch=%s iterations=%d-%d skip_ci=%s batch=%s",
        current_branch, start_iteration, args.iterations, args.skip_ci, args.batch,
    )

    if not git.sync_with_main(current_branch):
        logger.error("loop] Cannot sync with main, aborting")
        sys.exit(1)

    _loop_start = time.monotonic()

    for i in range(start_iteration, args.iterations + 1):
        print(f"\n--- Iteration {i}/{args.iterations} ---")
        logger.info("loop] === Iteration %d/%d ===", i, args.iterations)
        state.iteration = i
        state.save()

        if not git.sync_with_main(current_branch):
            logger.error("loop] Merge conflict could not be resolved, stopping")
            break

        if args.batch:
            ci_snapshot = ci.get_latest_run_id(current_branch)

            simplify = _run_phase("simplify", i, state, skip_ci=True)
            state.add(simplify)
            state.save()

            review = _run_phase("review", i, state, skip_ci=True)
            state.add(review)
            state.save()

            if not simplify.changes_made and not review.changes_made:
                logger.info("loop] Converged: no changes in either phase")
                break

            if not args.skip_ci:
                ci_passed, ci_errors, ci_time = ci.wait_for_ci(current_branch, known_previous_id=ci_snapshot)
                retries = 0
                while not ci_passed and retries < MAX_CI_RETRIES:
                    retries += 1
                    logger.info("ci-fix] Attempt %d/%d...", retries, MAX_CI_RETRIES)
                    claude.run_claude(build_ci_fix_prompt(ci_errors))
                    if not git.has_changes():
                        logger.info("ci-fix] No fix produced")
                        break
                    git.commit_and_push(f"Fix CI (attempt {retries})", current_branch)
                    ci_passed, ci_errors, ci_time = ci.wait_for_ci(current_branch)
                if not ci_passed:
                    logger.warning("loop] Stopping: CI failed")
                    break
        else:
            simplify = _run_phase("simplify", i, state, args.skip_ci)
            state.add(simplify)
            state.save()

            if not simplify.ci_passed:
                logger.warning("loop] Stopping: CI failed after simplify")
                break

            review = _run_phase("review", i, state, args.skip_ci)
            state.add(review)
            state.save()

            if not review.ci_passed:
                logger.warning("loop] Stopping: CI failed after review")
                break

            if not simplify.changes_made and not review.changes_made:
                logger.info("loop] Converged: no changes in either phase")
                break

    total = time.monotonic() - _loop_start
    logger.info("loop] Finished in %s", _format_duration(total))
    _print_summary(state, total)
