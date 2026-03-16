from __future__ import annotations

import logging
import os
import sys

enabled = False

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
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


def init(force_no_color: bool = False) -> None:
    global enabled
    if force_no_color or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        enabled = False
        return
    enabled = sys.stdout.isatty()


def wrap(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if enabled else text


def phase_color(phase: str) -> str:
    return PHASE_COLORS.get(phase, "")


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if not enabled or "]" not in msg:
            return super().format(record)
        tag, rest = msg.split("]", 1)
        color = TAG_COLORS.get(tag.strip(), PHASE_COLORS.get(tag.strip(), ""))
        if color:
            record.msg = f"{color}{tag}]{RESET}{rest}"
            record.args = ()
        return super().format(record)
