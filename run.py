"""
run.py - double-click / `python run.py` launcher for the terminal.
Same as:  python -m fantasy_terminal  (all the same arguments work)
"""
import sys
from fantasy_terminal.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
