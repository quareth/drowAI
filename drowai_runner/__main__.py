"""Module entrypoint for `python -m drowai_runner`."""

from __future__ import annotations

import sys

from drowai_runner.app import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
