"""
ui.py - THE LOOK (ANSI colors, panels, columns, bars, the Bloomberg feel)
=========================================================================

Everything here returns strings; terminal.py prints them.  The style rules,
borrowed from the finance terminals:

    * black background painted across the WHOLE line (paint()), so the screen
      looks the same whatever theme the user's terminal has
    * amber for labels, titles and the command bar; WHITE for values
    * green = up, red = down, dim grey for notes
    * section titles are amber-on-navy bars (section())
    * panels sit side by side (columns()) to make a dense dashboard
    * bar charts and sparklines are drawn with block characters (bar(), sparkline())
    * every screen ends with a numbered function menu (menu_bar())

Change colours in THEME below (256-color codes; see
https://en.wikipedia.org/wiki/ANSI_escape_code#8-bit for the chart).
Turn colours off:  DISPLAY["COLOR"]=False, --no-color, or env NO_COLOR=1.
"""

import os
import re
import shutil
from typing import Iterable, List, Sequence

from .config import DISPLAY

_COLOR_ON = DISPLAY["COLOR"] and not os.environ.get("NO_COLOR")

# ---------------------------------------------------------------------------
# THEME - 256-color codes.  fg = text colour, bg = background colour.
# ---------------------------------------------------------------------------
THEME = {
    "amber": "214",    # labels, titles, prompt          (Bloomberg orange)
    "white": "231",    # values
    "green": "82",     # up
    "red": "196",      # down
    "dim": "245",      # notes, rules
    "cyan": "51",      # secondary highlight (incoming edges)
    "navy": "17",      # section title background
    "black": "0",      # screen background
    "bar": "208",      # bar-chart fill
    "menu_bg": "236",  # bottom menu background
    "menu_fg": "214",
}


def set_color(on: bool) -> None:
    global _COLOR_ON
    _COLOR_ON = on


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR_ON else text


def fg(name: str, text: str) -> str:
    return _c(f"38;5;{THEME[name]}", text)


def bg(name: str, text: str, fg_name: str = None, bold: bool = False) -> str:
    code = f"48;5;{THEME[name]}"
    if fg_name:
        code += f";38;5;{THEME[fg_name]}"
    if bold:
        code += ";1"
    return _c(code, text)


class C:
    """Short colour helpers used all over terminal.py."""
    @staticmethod
    def amber(t): return fg("amber", t)
    @staticmethod
    def amber_bg(t): return bg("amber", t, "black", bold=True)
    @staticmethod
    def green(t): return fg("green", t)
    @staticmethod
    def red(t): return fg("red", t)
    @staticmethod
    def dim(t): return fg("dim", t)
    @staticmethod
    def white(t): return _c("1;38;5;" + THEME["white"], t)
    @staticmethod
    def cyan(t): return fg("cyan", t)
    @staticmethod
    def bold(t): return _c("1", t)
    @staticmethod
    def navy(t): return bg("navy", t, "amber", bold=True)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def width() -> int:
    """Screen width.  DISPLAY["WIDTH"] = "auto" follows the terminal window
    (min 100), or set a fixed number."""
    w = DISPLAY["WIDTH"]
    if os.environ.get("FFT_WIDTH"):          # env override, handy for screenshots
        return int(os.environ["FFT_WIDTH"])
    if w == "auto":
        try:
            return max(100, shutil.get_terminal_size((120, 40)).columns)
        except Exception:
            return 120
    return int(w)


_ANSI = re.compile(r"\033\[[0-9;]*[A-Za-z]")


def strip(s: str) -> str:
    return _ANSI.sub("", s)


def visible_len(s: str) -> int:
    """Length of a string ignoring the ANSI colour codes."""
    return len(strip(s))


def pad(s: str, n: int, align: str = "<") -> str:
    """Pad a (possibly coloured) string to n visible characters."""
    gap = n - visible_len(s)
    if gap <= 0:
        return s
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def clip(s: str, n: int) -> str:
    """Cut a coloured string to n visible characters (keeps the reset code)."""
    if visible_len(s) <= n:
        return s
    out, count, i = [], 0, 0
    while i < len(s) and count < n:
        m = _ANSI.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
        else:
            out.append(s[i])
            count += 1
            i += 1
    return "".join(out) + ("\033[0m" if _COLOR_ON else "")


def paint(text: str, w: int = None) -> str:
    """Give every line a black background all the way across the screen."""
    w = w or width()
    if not _COLOR_ON:
        return text
    black = f"\033[48;5;{THEME['black']}m"
    lines = []
    for ln in text.split("\n"):
        # re-assert the background after every reset so nested colours keep it
        ln = ln.replace("\033[0m", "\033[0m" + black)
        lines.append(black + pad(clip(ln, w), w) + "\033[0m")
    return "\n".join(lines)


CLEAR = "\033[2J\033[H"     # clear screen + cursor home (used by the REPL)


# ---------------------------------------------------------------------------
# building blocks
# ---------------------------------------------------------------------------
def header(title: str, subtitle: str = "", right: str = "") -> str:
    """Amber status bar across the screen.  title left, right text right-aligned."""
    w = width()
    left = f" {title} " + (f"  {subtitle} " if subtitle else "")
    right = f" {right} " if right else ""
    body = pad(left, w - visible_len(right)) + right
    return C.amber_bg(pad(body, w))


def section(title: str, right: str = "") -> str:
    """Section title bar: amber bold text on navy, like a terminal panel header."""
    w = width()
    body = f" {title}"
    if right:
        body = pad(body, w - len(right) - 1) + right + " "
    return C.navy(pad(body, w))


def rule(char: str = "─") -> str:
    return C.dim(char * width())


def panel(title: str, lines: Iterable[str], w: int = None) -> str:
    """A section bar followed by its content lines (no box, terminal style)."""
    w = w or width()
    out = [C.navy(pad(f" {title}", w))]
    for block in lines:
        for ln in str(block).split("\n"):
            out.append(pad(ln, w))
    return "\n".join(out)


def columns(blocks: Sequence[str], widths: Sequence[int] = None, gap: int = 2) -> str:
    """Put blocks of text side by side.  Each block is a string with newlines."""
    n = len(blocks)
    if widths is None:
        total = width() - gap * (n - 1)
        widths = [total // n] * n
        widths[-1] += total - sum(widths)
    cols = [str(b).split("\n") for b in blocks]
    height = max(len(c) for c in cols)
    out = []
    for i in range(height):
        cells = []
        for col, w in zip(cols, widths):
            ln = col[i] if i < len(col) else ""
            cells.append(pad(clip(ln, w), w))
        out.append((" " * gap).join(cells))
    return "\n".join(out)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], aligns: Sequence[str] = None,
          max_rows: int = None) -> str:
    """Aligned columns: amber headers, thin rule, then the rows."""
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


def kv(label: str, value: str, label_w: int = 10) -> str:
    """'LABEL  value' with the label in amber and the value in white."""
    return pad(C.amber(label), label_w) + " " + value


def menu_bar(items: Sequence[str]) -> str:
    """Bottom function menu, e.g. ['1) HOME', '2) TICKER', ...] on a grey bar."""
    w = width()
    body = "  ".join(items)
    return bg("menu_bg", pad(" " + body, w), "menu_fg", bold=True)


def ticker_strip(items: Sequence[str]) -> str:
    """One line of 'NAME value ▲chg' cells separated by dim bars, clipped to width."""
    return clip(C.dim(" │ ").join(items), width())


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------
BLOCKS = "▁▂▃▄▅▆▇█"


def bar(value: float, vmax: float, w: int = 20, color: str = "bar") -> str:
    """Horizontal bar of block characters, proportional to value / vmax."""
    if vmax <= 0 or value is None:
        return " " * w
    frac = max(0.0, min(1.0, value / vmax))
    full = int(frac * w)
    rem = frac * w - full
    partial = "▌" if rem >= 0.5 else ""
    s = "█" * full + partial
    return pad(fg(color, s), w)


def dbar(value: float, vmax: float, w: int = 20) -> str:
    """Diverging bar: negative values grow LEFT from the centre in red,
    positive values grow RIGHT in green.  Used for change columns."""
    if vmax <= 0 or value is None:
        return " " * w
    half = (w - 1) // 2
    n = int(round(min(1.0, abs(value) / vmax) * half))
    if value >= 0:
        return " " * half + C.dim("│") + fg("green", "█" * n) + " " * (half - n)
    return " " * (half - n) + fg("red", "█" * n) + C.dim("│") + " " * half


def sparkline(values: Sequence[float]) -> str:
    """▁▂▃▅▇█ style mini chart of a list of numbers (None = gap)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    out = []
    for v in values:
        if v is None:
            out.append(C.dim("·"))
        else:
            idx = int((v - lo) / span * (len(BLOCKS) - 1))
            out.append(fg("amber", BLOCKS[idx]))
    return "".join(out)


def vchart(values: Sequence[float], labels: Sequence[str], height: int = 6,
           baseline: float = None) -> str:
    """Vertical bar chart drawn with block characters.

    values    one number per column (None = gap / bye)
    labels    one short label per column (e.g. week numbers)
    baseline  if given, bars above it are green, below it red (e.g. base projection)
    """
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    # do not start the axis at zero: leave 30% of the range under the lowest
    # bar so the differences between weeks are visible (finance-chart style)
    rng = (max(vals) - min(vals)) or max(abs(max(vals)) * 0.2, 1.0)
    lo = max(0.0, min(vals) - rng * 0.3)
    hi = max(vals) or 1.0
    span = (hi - lo) or 1.0
    colw = max(len(l) for l in labels) + 1
    rows = []
    for level in range(height, 0, -1):
        cells = []
        threshold_top = lo + span * level / height
        threshold_bot = lo + span * (level - 1) / height
        for v in values:
            if v is None:
                cells.append(pad(C.dim("·") if level == 1 else " ", colw))
                continue
            if v >= threshold_top:
                ch = "█"
            elif v > threshold_bot:
                ch = BLOCKS[min(7, int((v - threshold_bot) / (span / height) * 8))]
            else:
                ch = " "
            if baseline is not None:
                col = "green" if v >= baseline else "red"
            else:
                col = "bar"
            cells.append(pad(fg(col, ch), colw))
        axis = f"{threshold_top:5.1f} " if level in (height, height // 2 + 1, 1) else "      "
        rows.append(C.dim(axis) + "".join(cells))
    rows.append(" " * 6 + "".join(pad(C.dim(l), colw) for l in labels))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# number formatting
# ---------------------------------------------------------------------------
def fmt_num(v, digits: int = 1) -> str:
    if v is None:
        return C.dim("BYE")
    return C.white(f"{v:.{digits}f}")


def fmt_chg(v: float, digits: int = 2) -> str:
    """Signed, coloured change.  Green up, red down, dim zero."""
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
