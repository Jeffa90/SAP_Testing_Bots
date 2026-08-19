"""Entry point for the packaged executable.

Double-clicking ``saptest.exe`` should do the useful thing, so bare invocation
starts the web interface. Passing arguments falls through to the normal CLI, which
is what a scheduled task or a power user wants.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> None:
    # Required before anything else in a frozen build: without it, a child process
    # re-runs the bootstrap and the executable relaunches itself in a loop.
    multiprocessing.freeze_support()

    from saptest.cli import app

    if len(sys.argv) == 1:
        sys.argv.append("ui")
    app()


if __name__ == "__main__":
    main()
