from __future__ import annotations

import logging
import os
import sys

enabled = False

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BOLD_WHITE = "\033[1;37m"
DARK_GREEN = "\033[38;5;28m"
DARK_YELLOW = "\033[38;5;178m"
DARK_RED = "\033[38;5;124m"
GRAY = "\033[90m"

PHASE_COLORS = {"simplify": DARK_GREEN, "review": DARK_YELLOW, "security": DARK_RED}
TAG_COLORS = {
    "loop": BOLD_WHITE,
    "ci": CYAN,
    "ci-fix": CYAN,
    "git": BLUE,
    "sync": BLUE,
    "claude": MAGENTA,
    "signal": DARK_YELLOW,
    "update": GRAY,
    "preflight": GRAY,
    "state": GRAY,
}

BOX_WIDTH = 56


def init(force_no_color: bool = False) -> None:
    global enabled
    if force_no_color or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        enabled = False
        return
    enabled = sys.stdout.isatty()


def wrap(text: str, code: str) -> str:
    if not enabled or not code:
        return text
    return f"{code}{text}{RESET}"


def phase_color(phase: str) -> str:
    return PHASE_COLORS.get(phase, "")


def status_mark(passed: bool, changed: bool, reverted: bool = False) -> str:
    if reverted:
        return wrap("\u21ba", YELLOW)
    if not changed:
        return wrap("\u00b7", DIM)
    return wrap("\u2713", GREEN) if passed else wrap("\u2717", RED)


def separator() -> str:
    return wrap("=" * BOX_WIDTH, BOLD + CYAN)


def section_title(title: str) -> str:
    return wrap(title, BOLD + CYAN)


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if not enabled or "]" not in msg:
            return super().format(record)
        tag, rest = msg.split("]", 1)
        tag_name = tag.strip()
        clr = TAG_COLORS.get(tag_name, PHASE_COLORS.get(tag_name, ""))
        if not clr:
            return super().format(record)
        record = logging.makeLogRecord(record.__dict__)
        record.msg = f"{clr}{tag}]{RESET}{rest}"
        record.args = ()
        formatted = super().format(record)
        formatted = formatted.replace(f"[{clr}", f"{clr}[", 1)
        clr_pos = formatted.find(clr)
        if clr_pos > 0:
            prefix = formatted[:clr_pos]
            formatted = f"{GRAY}{prefix}{RESET}{formatted[clr_pos:]}"
        return formatted
