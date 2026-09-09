"""
tools/screenshot.py - RENDER A TERMINAL SCREEN TO HTML (and PNG if Chromium exists)
===================================================================================

    python tools/screenshot.py out.html TICKER
    python tools/screenshot.py out.html --script examples/demo.txt

Runs the given terminal command(s) with colors on, converts the ANSI color
codes to HTML, and writes a black-background page that looks like the real
terminal.  If a Chromium binary is found it also writes out.png next to it.
Handy for sharing a screen or checking the look without a terminal.
"""

import html
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 256-color palette entries we use -> CSS colors
PALETTE = {"214": "#ffa000", "82": "#4ce64c", "196": "#ff3b3b", "245": "#8a8a8a",
           "51": "#3fd8ff", "33": "#1f6fff", "17": "#001a66", "0": "#000000",
           "231": "#ffffff", "226": "#ffe600", "208": "#ff8c00", "236": "#303030",
           "240": "#585858", "27": "#0057ff"}


def ansi_to_html(text: str) -> str:
    """Convert the ANSI codes produced by ui.py into <span style=...> html."""
    out, style = [], {}
    pos = 0
    for m in re.finditer(r"\033\[([0-9;]*)m", text):
        out.append(_span(text[pos:m.start()], style))
        pos = m.end()
        codes = m.group(1).split(";") if m.group(1) else ["0"]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == "0":
                style = {}
            elif c == "1":
                style["font-weight"] = "bold"
            elif c == "97":
                style["color"] = "#ffffff"
            elif c == "30":
                style["color"] = "#000000"
            elif c == "7":
                style["filter"] = "invert(1)"
            elif c in ("38", "48") and i + 2 < len(codes) and codes[i + 1] == "5" and False:
                pass
            elif c in ("38", "48") and i + 2 < len(codes) and codes[i + 1] == "5":
                key = "color" if c == "38" else "background"
                style[key] = PALETTE.get(codes[i + 2], "#ffffff")
                i += 2
            i += 1
    out.append(_span(text[pos:], style))
    return "".join(out)


def _strip_other_escapes(text: str) -> str:
    """Drop cursor / clear-screen sequences (anything not ending in 'm')."""
    return re.sub(r"\033\[[0-9;]*[A-LN-Za-ln-z]", "", text)


def _span(s: str, style: dict) -> str:
    if not s:
        return ""
    s = html.escape(s)
    if not style:
        return s
    css = ";".join(f"{k}:{v}" for k, v in style.items())
    return f'<span style="{css}">{s}</span>'


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    out_html = argv[0]
    cmd = [sys.executable, "-m", "fantasy_terminal"] + argv[1:]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", FORCE_COLOR="1")
    env.pop("NO_COLOR", None)
    text = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env).stdout
    body = ansi_to_html(_strip_other_escapes(text))
    page = f"""<!doctype html><meta charset="utf-8"><title>FFT screen</title>
<style>body{{background:#000;color:#ffa000;margin:0;padding:12px}}
pre{{font:13px/1.25 "DejaVu Sans Mono","Menlo","Consolas",monospace;white-space:pre}}</style>
<pre>{body}</pre>"""
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(page)
    print("wrote", out_html)
    chromium = shutil.which("chromium") or shutil.which("chromium-browser") or "/opt/pw-browsers/chromium"
    if os.path.exists(chromium):
        png = os.path.splitext(out_html)[0] + ".png"
        subprocess.run([chromium, "--headless=new", "--no-sandbox", "--hide-scrollbars",
                        "--window-size=1250,1000", f"--screenshot={png}",
                        "file://" + os.path.abspath(out_html)], capture_output=True)
        if os.path.exists(png):
            print("wrote", png)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
