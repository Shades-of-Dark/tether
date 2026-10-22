import sys

from .cli import main

# `python -m tether` with no arguments opens the app
raise SystemExit(main(sys.argv[1:] or ["app"]))
