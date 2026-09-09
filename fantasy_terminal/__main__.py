"""
__main__.py - ENTRY POINT
=========================

    python -m fantasy_terminal                    interactive terminal
    python -m fantasy_terminal TICKER             run one command and exit
    python -m fantasy_terminal PLAYER mahomes     (words after the module are the command)
    python -m fantasy_terminal --script demo.txt  run every line of a file, then exit
    python -m fantasy_terminal --no-color ...     plain text output
    python -m fantasy_terminal --quiet --script f run a script but only print the LAST screen
    FFT_WIDTH=140 python -m fantasy_terminal ...  force a screen width
"""

import sys

from . import ui
from .terminal import Terminal


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--no-color" in argv:
        ui.set_color(False)
        argv.remove("--no-color")
    quiet = "--quiet" in argv
    if quiet:
        argv.remove("--quiet")
    term = Terminal()
    if argv and argv[0] == "--script":
        last = ""
        with open(argv[1], encoding="utf-8") as f:
            for line in f:
                if not quiet:
                    print(ui.C.amber("FFT> ") + line.rstrip())
                out = term.run_line(line)
                if out == "__QUIT__":
                    break
                if out:
                    last = out
                    if not quiet:
                        print(out)
        if quiet and last:
            print(last)
        return 0
    if argv:
        out = term.run_line(" ".join(argv))
        if out and out != "__QUIT__":
            print(out)
        return 0
    term.repl()
    return 0


if __name__ == "__main__":
    sys.exit(main())
