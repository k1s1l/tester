"""Container entry point: ``python -m app``.

Importing aiogram/anthropic takes several seconds. When the bot runs as PID 1 in a container, a SIGTERM
that arrives during that window would be ignored (PID 1 has no default signal actions) and the platform
would SIGKILL the process later. Installing a handler first makes early stops immediate; aiogram replaces
it with its graceful handler once polling starts.
"""

import signal
import sys


def _early_exit(signum: int, _frame: object) -> None:
    sys.exit(128 + signum)


signal.signal(signal.SIGTERM, _early_exit)

from app.main import main  # noqa: E402  (must come after the signal handler)

main()
