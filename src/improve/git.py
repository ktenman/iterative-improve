from __future__ import annotations

import logging

from improve.process import run
from improve.claude import run_claude
from improve.prompt import extract_summary

logger = logging.getLogger("improve")


def branch() -> str:
    return run(["git", "branch", "--show-current"]).stdout.strip()


def has_changes() -> bool:
    return bool(run(["git", "status", "--porcelain"]).stdout.strip())


def changed_files() -> list[str]:
    lines = run(["git", "status", "--porcelain"]).stdout.strip().split("\n")
    return [line[3:].strip() for line in lines if line.strip()]


def diff_vs_main() -> str:
    return run(["git", "diff", "--name-only", "main...HEAD"]).stdout.strip()


def has_conflicts() -> bool:
    result = run(["git", "diff", "--name-only", "--diff-filter=U"])
    return bool(result.stdout.strip())


def conflict_files() -> list[str]:
    result = run(["git", "diff", "--name-only", "--diff-filter=U"])
    return [f for f in result.stdout.strip().split("\n") if f]


def stage_tracked_changes():
    result = run(["git", "diff", "--name-only"])
    modified = [f for f in result.stdout.strip().split("\n") if f]
    result = run(["git", "diff", "--name-only", "--cached"])
    staged = [f for f in result.stdout.strip().split("\n") if f]
    result = run(["git", "ls-files", "--others", "--exclude-standard"])
    untracked = [f for f in result.stdout.strip().split("\n") if f]
    all_files = list(set(modified + staged + untracked))
    safe = [f for f in all_files if not f.startswith(".env") and f != "credentials.json"]
    if safe:
        run(["git", "add"] + safe, check=True)


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
        run(["git", "push", "-u", "origin", branch_name])
        return True

    if not has_conflicts():
        logger.warning("sync] Merge failed but no conflicts detected")
        run(["git", "merge", "--abort"])
        return False

    return _resolve_conflicts(branch_name)


def _resolve_conflicts(branch_name: str) -> bool:
    conflicts = conflict_files()
    logger.warning("sync] Merge conflicts in %d file(s): %s", len(conflicts), ", ".join(conflicts[:5]))

    file_list = "\n".join(conflicts)
    prompt = (
        "There are git merge conflicts that need resolving.\n\n"
        f"Conflicted files:\n{file_list}\n\n"
        "Instructions:\n"
        "- Read each conflicted file\n"
        "- Resolve conflicts by keeping the correct code (merge both sides logically)\n"
        "- Remove all conflict markers (<<<<<<, ======, >>>>>>)\n"
        "- Make sure the resolved code compiles and is correct\n"
        "- Run lint/format/test commands if appropriate\n"
        '- Output one line starting with "SUMMARY:" describing what you resolved'
    )

    logger.info("sync] Asking Claude to resolve conflicts...")
    output, _ = run_claude(prompt)

    if has_conflicts():
        logger.error("sync] Conflicts remain after Claude attempted resolution")
        run(["git", "merge", "--abort"])
        return False

    run(["git", "add", "-A"], check=True)
    summary = extract_summary(output)
    commit = run(["git", "commit", "--no-edit"])
    if commit.returncode != 0:
        commit = run(["git", "commit", "-m", f"Resolve merge conflicts: {summary[:40]}"])
    if commit.returncode != 0:
        logger.error("sync] Failed to commit merge resolution")
        run(["git", "merge", "--abort"])
        return False

    push = run(["git", "push", "-u", "origin", branch_name])
    if push.returncode != 0:
        logger.warning("sync] Push failed after conflict resolution: %s", push.stderr.strip())
        return False

    logger.info("sync] Conflicts resolved and pushed")
    return True
