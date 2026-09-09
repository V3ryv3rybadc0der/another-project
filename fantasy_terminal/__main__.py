"""
__main__.py - ENTRY POINT
=========================

    python -m fantasy_terminal                    interactive terminal
    python -m fantasy_terminal TICKER             run one command and exit
    python -m fantasy_terminal PLAYER mahomes     (words after the module are the command)
    python -m fantasy_terminal --script demo.txt  run every line of a file, then exit
    python -m fantasy_terminal --no-color ...     plain text output
"""

import sys

from . import ui
from .terminal import Terminal


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--no-color" in argv:
        ui.set_color(False)
        argv.remove("--no-color")
    term = Terminal()
    if argv and argv[0] == "--script":
        with open(argv[1], encoding="utf-8") as f:
            for line in f:
                print(ui.C.amber("FFT> ") + line.rstrip())
                out = term.run_line(line)
                if out == "__QUIT__":
                    break
                if out:
                    print(out)
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
