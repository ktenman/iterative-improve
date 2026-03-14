from __future__ import annotations

ACTION_VERBS = {
    "add", "fix", "update", "remove", "extract", "simplify",
    "refactor", "replace", "move", "rename", "clean",
}


def build_phase_prompt(phase: str, branch_diff: str, context: str) -> str:
    focus = {
        "simplify": (
            "simplification opportunities:\n"
            "- Duplicated code that can be extracted\n"
            "- Overly complex logic that can be simplified\n"
            "- Inefficient patterns\n"
            "- Dead or unreachable code"
        ),
        "review": (
            "quality issues:\n"
            "- Bugs and correctness problems\n"
            "- Security vulnerabilities\n"
            "- Performance issues\n"
            "- Missing edge case handling"
        ),
    }[phase]

    return (
        f"Review the code changed on this branch (vs main) for {focus}\n\n"
        f"Files changed on this branch:\n{branch_diff}\n\n"
        f"Previous iterations already addressed:\n{context}\n\n"
        "Instructions:\n"
        "- Focus on NEW issues not already fixed\n"
        "- Make changes directly to the files\n"
        "- After editing, run the project's lint/format/test commands if appropriate\n"
        '- If nothing needs changing, say "NO_CHANGES_NEEDED"\n'
        '- Output exactly one line starting with "SUMMARY:" describing what you changed'
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
    for line in output.split("\n"):
        stripped = line.strip()
        if stripped.upper().startswith("SUMMARY:"):
            return stripped[8:].strip()
    for line in output.split("\n"):
        stripped = line.strip()
        if len(stripped) > 15:
            return stripped
    return "Code improvements"


def build_commit_message(phase: str, summary: str) -> str:
    clean = summary.replace("`", "").replace("*", "").strip()
    first_word = clean.split()[0].lower() if clean else ""
    if first_word in ACTION_VERBS:
        message = clean[0].upper() + clean[1:]
    elif phase == "simplify":
        message = f"Simplify {clean[0].lower() + clean[1:]}" if clean else "Simplify code"
    else:
        message = f"Fix {clean[0].lower() + clean[1:]}" if clean else "Fix code issues"
    if len(message) <= 50:
        return message
    truncated = message[:47]
    last_space = truncated.rfind(" ")
    if last_space > 20:
        return truncated[:last_space] + "..."
    return truncated + "..."
