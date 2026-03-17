from __future__ import annotations

ACTION_VERBS = frozenset(
    {
        "add",
        "fix",
        "update",
        "remove",
        "extract",
        "simplify",
        "refactor",
        "replace",
        "move",
        "rename",
        "clean",
    }
)

PHASE_PROMPTS = {
    "simplify": (
        "You are an expert code simplification specialist focused on enhancing code "
        "clarity, consistency, and maintainability while preserving exact functionality.\n\n"
        "Analyze the changed code and apply refinements that:\n\n"
        "1. **Preserve Functionality**: Never change what the code does - only how it "
        "does it. All original features, outputs, and behaviors must remain intact.\n\n"
        "2. **Apply Project Standards**: Follow the established coding standards from "
        "CLAUDE.md including import patterns, naming conventions, error handling patterns, "
        "and language-specific style.\n\n"
        "3. **Enhance Clarity**: Simplify code structure by:\n"
        "   - Reducing unnecessary complexity and nesting\n"
        "   - Eliminating redundant code and abstractions\n"
        "   - Improving readability through clear variable and function names\n"
        "   - Consolidating related logic\n"
        "   - Removing unnecessary comments that describe obvious code\n"
        "   - Choose clarity over brevity - explicit code is often better than overly "
        "compact code\n\n"
        "4. **Maintain Balance**: Avoid over-simplification that could:\n"
        "   - Reduce code clarity or maintainability\n"
        "   - Create overly clever solutions that are hard to understand\n"
        "   - Combine too many concerns into single functions or components\n"
        "   - Remove helpful abstractions that improve code organization\n"
        "   - Prioritize fewer lines over readability (e.g., nested ternaries, dense "
        "one-liners)\n"
        "   - Make the code harder to debug or extend"
    ),
    "review": (
        "You are an expert code reviewer specializing in modern software development. "
        "Your primary responsibility is to review code against project guidelines with "
        "high precision to minimize false positives.\n\n"
        "## Core Review Responsibilities\n\n"
        "**Project Guidelines Compliance**: Verify adherence to explicit project rules "
        "(typically in CLAUDE.md) including import patterns, framework conventions, "
        "language-specific style, function declarations, error handling, logging, testing "
        "practices, and naming conventions.\n\n"
        "**Bug Detection**: Identify actual bugs that will impact functionality - logic "
        "errors, null/undefined handling, race conditions, memory leaks, security "
        "vulnerabilities, and performance problems.\n\n"
        "**Code Quality**: Evaluate significant issues like code duplication, missing "
        "critical error handling, and inadequate test coverage.\n\n"
        "## Issue Confidence Scoring\n\n"
        "Rate each issue from 0-100:\n"
        "- 0-25: Likely false positive or pre-existing issue\n"
        "- 26-50: Minor nitpick not explicitly in CLAUDE.md\n"
        "- 51-75: Valid but low-impact issue\n"
        "- 76-90: Important issue requiring attention\n"
        "- 91-100: Critical bug or explicit CLAUDE.md violation\n\n"
        "**Only fix issues with confidence >= 80.**\n\n"
        "## False Positives to Ignore\n\n"
        "- Pre-existing issues not introduced by the current changes\n"
        "- Issues that a linter, typechecker, or compiler would catch\n"
        "- Pedantic nitpicks that a senior engineer wouldn't call out\n"
        "- General code quality issues unless explicitly required in CLAUDE.md\n"
        "- Changes in functionality that are likely intentional"
    ),
    "security": (
        "You are an elite error handling and security auditor with zero tolerance for "
        "silent failures, inadequate error handling, and security vulnerabilities.\n\n"
        "## Security Review\n\n"
        "Examine the code for:\n"
        "- Injection attacks (SQL, command, XSS)\n"
        "- Authentication and authorization flaws\n"
        "- Sensitive data exposure (secrets, tokens, credentials in code or logs)\n"
        "- Insecure deserialization\n"
        "- Path traversal and file access issues\n"
        "- Dependency vulnerabilities\n\n"
        "## Error Handling Audit\n\n"
        "For every error handling location, check:\n\n"
        "**Catch Block Specificity:**\n"
        "- Does the catch block catch only the expected error types?\n"
        "- Could this catch block accidentally suppress unrelated errors?\n"
        "- Should this be multiple catch blocks for different error types?\n\n"
        "**Silent Failures:**\n"
        "- Empty catch blocks (absolutely forbidden)\n"
        "- Catch blocks that only log and continue without user feedback\n"
        "- Returning null/default values on error without logging\n"
        "- Fallback chains that try multiple approaches without explaining why\n"
        "- Retry logic that exhausts attempts without informing the user\n\n"
        "**Error Propagation:**\n"
        "- Should this error be propagated to a higher-level handler?\n"
        "- Is the error being swallowed when it should bubble up?\n"
        "- Does catching here prevent proper cleanup or resource management?\n\n"
        "## Confidence Scoring\n\n"
        "Rate each issue from 0-100. Only fix issues with confidence >= 80.\n"
        "Ignore pre-existing issues and likely false positives."
    ),
}

AVAILABLE_PHASES = tuple(PHASE_PROMPTS)

PHASE_COMMIT_PREFIX = {
    "simplify": "Simplify",
    "review": "Fix",
    "security": "Fix",
}


def build_phase_prompt(phase: str, branch_diff: str, context: str) -> str:
    role = PHASE_PROMPTS[phase]
    return (
        f"{role}\n\n"
        f"Files changed on this branch (vs main):\n{branch_diff}\n\n"
        f"Previous iterations already addressed:\n{context}\n\n"
        "Instructions:\n"
        "- Focus on NEW issues not already fixed in previous iterations\n"
        "- Make changes directly to the files — do not use the Agent tool\n"
        "- After editing, run the project's lint/format/test commands if appropriate\n"
        '- If nothing needs changing, say "NO_CHANGES_NEEDED"\n'
        '- Output exactly one line starting with "SUMMARY:" describing what you changed'
    )


def build_conflict_prompt(conflicts: list[str]) -> str:
    file_list = "\n".join(conflicts)
    return (
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


def build_ci_fix_prompt(errors: str) -> str:
    return (
        "CI/CD pipeline failed. Fix the errors with minimal changes.\n\n"
        f"Error logs:\n{errors}\n\n"
        "Instructions:\n"
        "- Fix only what's needed to pass CI\n"
        "- Run lint/format/test commands after fixing\n"
        '- Output one line starting with "SUMMARY:" describing the fix'
    )


def extract_summary(output: str) -> str:
    fallback = ""
    for line in output.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("SUMMARY:"):
            return stripped[8:].strip()
        if not fallback and len(stripped) > 15:
            fallback = stripped
    return fallback or "Code improvements"


def _truncate(message: str, max_length: int = 50) -> str:
    if len(message) <= max_length:
        return message
    limit = max_length - 3
    last_space = message[:limit].rfind(" ")
    if last_space > 20:
        return message[:last_space] + "..."
    return message[:limit] + "..."


def build_commit_message(phase: str, summary: str) -> str:
    prefix = PHASE_COMMIT_PREFIX.get(phase, "Fix")
    clean = summary.replace("`", "").replace("*", "").strip()
    if not clean:
        return f"{prefix} code issues" if prefix == "Fix" else f"{prefix} code"
    first_word = clean.split()[0].lower()
    if first_word in ACTION_VERBS:
        message = clean[0].upper() + clean[1:]
    else:
        lowered = clean[0].lower() + clean[1:]
        message = f"{prefix} {lowered}"
    return _truncate(message)


def strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def build_squash_prompt(diff: str, kept_results: list[dict]) -> tuple[str, str]:
    phases_used = sorted({r["phase"] for r in kept_results if r.get("phase")})
    summaries = "\n".join(f"- [{r.get('phase', '?')}] {r['summary']}" for r in kept_results)
    if len(kept_results) == 1:
        fallback_subject = build_commit_message(phases_used[0], kept_results[0]["summary"])
    else:
        fallback_subject = f"Improve code ({', '.join(phases_used)})"
    fallback = fallback_subject + "\n\n" + "\n".join(f"- {r['summary']}" for r in kept_results)
    prompt = (
        "Write a git commit message for squashing these changes into one commit.\n\n"
        f"Phase summaries:\n{summaries}\n\n"
        f"Full diff vs main:\n{diff[:8000]}\n\n"
        "Rules:\n"
        "- Subject line: max 50 chars, start with uppercase imperative verb\n"
        "- No prefixes like feat:/fix:/chore:\n"
        "- Add a blank line then a body with bullet points summarizing key changes\n"
        "- Be specific about what changed, not generic\n"
        "- Output ONLY the commit message, nothing else"
    )
    return prompt, fallback
