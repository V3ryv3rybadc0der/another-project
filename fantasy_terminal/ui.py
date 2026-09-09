"""
ui.py - THE LOOK (ANSI colors, tables, panels)
==============================================

The classic finance-terminal look: amber headers on black, green for up,
red for down, dim grey for labels.  Everything here returns strings; the
terminal prints them.

    C.amber("text")        color helpers (return plain text when color is off)
    header("TITLE", ...)   the amber title bar
    panel("TITLE", lines)  a boxed section
    table(headers, rows)   aligned columns
    fmt_chg(1.23)          "+1.23" in green / "-0.40" in red
    arrow(delta)           "▲" / "▼" / "·"

Turn colors off:  DISPLAY["COLOR"]=False in config.py, or run with --no-color,
or set the NO_COLOR environment variable.
"""

import os
from typing import Iterable, List, Sequence

from .config import DISPLAY

_COLOR_ON = DISPLAY["COLOR"] and not os.environ.get("NO_COLOR")


def set_color(on: bool) -> None:
    global _COLOR_ON
    _COLOR_ON = on


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR_ON else text


class C:
    """Color helpers.  256-color codes: 214 = amber, 82 = green, 196 = red."""
    @staticmethod
    def amber(t): return _c("38;5;214", t)
    @staticmethod
    def amber_bg(t): return _c("48;5;214;30;1", t)
    @staticmethod
    def green(t): return _c("38;5;82", t)
    @staticmethod
    def red(t): return _c("38;5;196", t)
    @staticmethod
    def dim(t): return _c("38;5;245", t)
    @staticmethod
    def white(t): return _c("97;1", t)
    @staticmethod
    def cyan(t): return _c("38;5;51", t)
    @staticmethod
    def bold(t): return _c("1", t)


def width() -> int:
    return int(DISPLAY["WIDTH"])


def visible_len(s: str) -> int:
    """Length of a string ignoring the ANSI color codes."""
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def pad(s: str, n: int, align: str = "<") -> str:
    """Pad a (possibly colored) string to n visible characters."""
    gap = n - visible_len(s)
    if gap <= 0:
        return s
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def header(title: str, subtitle: str = "") -> str:
    """Amber bar across the screen, like the top of a terminal screen."""
    line = f" {title} "
    if subtitle:
        line += f"  {subtitle} "
    return C.amber_bg(pad(line, width()))


def rule(char: str = "─") -> str:
    return C.dim(char * width())


def panel(title: str, lines: Iterable[str]) -> str:
    """A section with an amber title and a thin rule under it."""
    out = [C.amber(f"┌─ {title} ").rstrip() + C.dim("─" * max(0, width() - len(title) - 4))]
    for block in lines:
        # a table is one string with many lines: give every line the border
        for ln in str(block).split("\n"):
            out.append(C.dim("│ ") + ln)
    out.append(C.dim("└" + "─" * (width() - 1)))
    return "\n".join(out)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Sequence[str] = None,
          max_rows: int = None) -> str:
    """Aligned columns.  aligns is a string per column: '<' left, '>' right."""
    rows = [list(map(str, r)) for r in rows]
    if max_rows is not None:
        rows = rows[:max_rows]
    aligns = list(aligns or ["<"] * len(headers))
    widths = [visible_len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], visible_len(cell))
    head = "  ".join(pad(C.amber(h), widths[i], aligns[i]) for i, h in enumerate(headers))
    body = ["  ".join(pad(c, widths[i], aligns[i]) for i, c in enumerate(r)) for r in rows]
    return "\n".join([head, C.dim("  ".join("─" * w for w in widths))] + body)


def fmt_num(v, digits: int = 1) -> str:
    if v is None:
        return C.dim("BYE")
    return f"{v:.{digits}f}"


def fmt_chg(v: float, digits: int = 2) -> str:
    """Signed, colored change.  Green up, red down, dim zero."""
    if v is None or abs(v) < 0.005:
        return C.dim(f"{0:+.{digits}f}")
    s = f"{v:+.{digits}f}"
    return C.green(s) if v > 0 else C.red(s)


def arrow(v: float) -> str:
    if v is None or abs(v) < 0.005:
        return C.dim("·")
    return C.green("▲") if v > 0 else C.red("▼")


def pct(v: float, base: float) -> str:
    if not base:
        return C.dim("  n/a")
    p = v / abs(base) * 100
    s = f"{p:+.1f}%"
    return C.green(s) if p > 0 else (C.red(s) if p < 0 else C.dim(s))
