from __future__ import annotations

import logging
import shutil
from pathlib import Path

from improve.claude import run_claude
from improve.phases import build_conflict_prompt, extract_summary
from improve.process import run

logger = logging.getLogger("improve")


def head_sha() -> str:
    result = run(["git", "rev-parse", "HEAD"])
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        logger.warning("git] Failed to determine HEAD sha")
    return sha


def revert_to(sha: str, branch_name: str) -> bool:
    reset = run(["git", "reset", "--hard", sha])
    if reset.returncode != 0:
        logger.warning("git] Reset failed: %s", reset.stderr.strip())
        return False
    push = run(["git", "push", "--force-with-lease", "origin", branch_name])
    if push.returncode != 0:
        logger.warning("git] Force push failed: %s", push.stderr.strip())
        return False
    logger.info("git] Reverted to %s", sha[:8])
    return True


def discard_changes() -> None:
    result = run(["git", "checkout", "--", "."])
    if result.returncode != 0:
        logger.warning("git] Failed to discard changes: %s", result.stderr.strip())


def branch() -> str:
    return run(["git", "branch", "--show-current"]).stdout.strip()


def detect_platform() -> str:
    result = run(["git", "remote", "get-url", "origin"])
    if result.returncode != 0:
        return "github"
    url = result.stdout.strip().lower()
    if "gitlab" in url:
        return "gitlab"
    return "github"


def has_changes() -> bool:
    return bool(run(["git", "status", "--porcelain", "--no-renames"]).stdout.strip())


def changed_files(cwd: str | None = None) -> list[str]:
    cmd = ["git"]
    if cwd:
        cmd.extend(["-C", cwd])
    cmd.extend(["status", "--porcelain", "--no-renames"])
    lines = run(cmd).stdout.split("\n")
    return [line[3:].strip() for line in lines if line.strip()]


def diff_vs_main() -> str:
    return run(["git", "diff", "--name-only", "main...HEAD"]).stdout.strip()


def has_conflicts() -> bool:
    return bool(conflict_files())


def conflict_files() -> list[str]:
    result = run(["git", "diff", "--name-only", "--diff-filter=U"])
    return [f for f in result.stdout.strip().split("\n") if f]


def stage_tracked_changes() -> None:
    files = changed_files()
    if not files:
        return
    result = run(["git", "add", "--", *files])
    if result.returncode != 0:
        logger.warning("git] Failed to stage files: %s", result.stderr.strip())


def commit_and_push(message: str, branch_name: str) -> bool:
    stage_tracked_changes()
    commit = run(["git", "commit", "-m", message])
    if commit.returncode != 0:
        logger.warning("git] Commit failed: %s", commit.stderr.strip())
        return False
    push = run(["git", "push", "-u", "origin", branch_name])
    if push.returncode != 0:
        logger.warning("git] Push failed: %s", push.stderr.strip())
        return False
    logger.info("git] Pushed: %s", message)
    return True


def sync_with_main(branch_name: str) -> bool:
    logger.info("sync] Fetching origin/main...")
    fetch = run(["git", "fetch", "origin", "main"])
    if fetch.returncode != 0:
        logger.warning("sync] Fetch failed: %s", fetch.stderr.strip())
        return True

    behind = run(["git", "rev-list", "--count", "HEAD..origin/main"])
    count = behind.stdout.strip()
    if count == "0":
        logger.info("sync] Branch is up to date with main")
        return True

    logger.info("sync] Branch is %s commit(s) behind main, merging...", count)
    merge = run(["git", "merge", "origin/main", "--no-edit"])

    if merge.returncode == 0:
        logger.info("sync] Merged cleanly")
        push = run(["git", "push", "-u", "origin", branch_name])
        if push.returncode != 0:
            logger.warning("sync] Push after merge failed: %s", push.stderr.strip())
        return True

    if not has_conflicts():
        logger.warning("sync] Merge failed but no conflicts detected")
        run(["git", "merge", "--abort"])
        return False

    return _resolve_conflicts(branch_name)


def _commit_resolution(output: str) -> bool:
    stage_tracked_changes()
    if run(["git", "commit", "--no-edit"]).returncode == 0:
        return True
    summary = extract_summary(output)
    return run(["git", "commit", "-m", f"Resolve merge conflicts: {summary[:40]}"]).returncode == 0


def _resolve_conflicts(branch_name: str) -> bool:
    conflicts = conflict_files()
    logger.warning(
        "sync] Merge conflicts in %d file(s): %s",
        len(conflicts),
        ", ".join(conflicts[:5]),
    )

    logger.info("sync] Asking Claude to resolve conflicts...")
    output, _ = run_claude(build_conflict_prompt(conflicts))

    if has_conflicts():
        logger.error("sync] Conflicts remain after Claude attempted resolution")
        run(["git", "merge", "--abort"])
        return False

    if not _commit_resolution(output):
        logger.error("sync] Failed to commit merge resolution")
        run(["git", "merge", "--abort"])
        return False

    push = run(["git", "push", "-u", "origin", branch_name])
    if push.returncode != 0:
        logger.warning("sync] Push failed after conflict resolution: %s", push.stderr.strip())
        return False

    logger.info("sync] Conflicts resolved and pushed")
    return True


def resolve_existing_conflicts() -> bool:
    conflicts = conflict_files()
    if not conflicts:
        return True
    logger.warning(
        "git] Found %d file(s) with unresolved merge conflicts: %s",
        len(conflicts),
        ", ".join(conflicts[:5]),
    )

    logger.info("git] Asking Claude to resolve pre-existing conflicts...")
    output, _ = run_claude(build_conflict_prompt(conflicts))

    if has_conflicts():
        logger.warning("git] Auto-resolution failed, aborting merge to restore clean state...")
        run(["git", "merge", "--abort"])
        if has_conflicts():
            logger.error("git] Could not abort merge — manual resolution required")
            return False
        logger.info("git] Merge aborted, working tree restored")
        return True

    if not _commit_resolution(output):
        logger.error("git] Failed to commit conflict resolution")
        run(["git", "merge", "--abort"])
        return False

    logger.info("git] Pre-existing conflicts resolved and committed")
    return True


def create_worktree(worktree_path: str) -> bool:
    result = run(["git", "worktree", "add", "--detach", worktree_path])
    if result.returncode != 0:
        logger.warning("git] Failed to create worktree: %s", result.stderr.strip())
        return False
    return True


def remove_worktree(worktree_path: str) -> None:
    result = run(["git", "worktree", "remove", "--force", worktree_path])
    if result.returncode != 0:
        logger.warning(
            "git] Failed to remove worktree %s: %s", worktree_path, result.stderr.strip()
        )


def apply_worktree_changes(worktree_path: str) -> list[str]:
    files = changed_files(worktree_path)
    if not files:
        return []
    main_root = run(["git", "rev-parse", "--show-toplevel"]).stdout.strip()
    if not main_root:
        logger.warning("git] Cannot determine repo root, skipping worktree apply")
        return []
    worktree = Path(worktree_path).resolve()
    main = Path(main_root).resolve()
    applied: list[str] = []
    for f in files:
        src = (worktree / f).resolve()
        dst = (main / f).resolve()
        if not src.is_relative_to(worktree) or not dst.is_relative_to(main):
            logger.warning("git] Skipping path traversal: %s", f)
            continue
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()
        applied.append(f)
    return applied


def squash_branch(branch_name: str, message: str) -> bool:
    base = run(["git", "merge-base", "HEAD", "main"]).stdout.strip()
    if not base:
        logger.warning("git] Cannot find merge base with main")
        return False

    commit_count = run(["git", "rev-list", "--count", f"{base}..HEAD"]).stdout.strip()
    if commit_count in ("0", "1"):
        logger.info("git] Nothing to squash (%s commit(s))", commit_count)
        return True

    logger.info("git] Squashing %s commit(s)...", commit_count)
    reset = run(["git", "reset", "--soft", base])
    if reset.returncode != 0:
        logger.warning("git] Reset failed: %s", reset.stderr.strip())
        return False

    commit = run(["git", "commit", "-m", message])
    if commit.returncode != 0:
        logger.warning("git] Commit failed: %s", commit.stderr.strip())
        return False

    push = run(["git", "push", "--force-with-lease", "origin", branch_name])
    if push.returncode != 0:
        logger.warning("git] Force push failed: %s", push.stderr.strip())
        return False

    logger.info("git] Squashed and force-pushed")
    return True
