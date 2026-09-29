"""Suite-wide harness setup."""
from __future__ import annotations

# Import ib_insync ONCE, before any test runs (KAN-90). Its dependency eventkit
# calls asyncio.get_event_loop() at import time, and every production caller
# imports ib_insync lazily — so the first import in a process lands inside
# whichever test touches it first. If an earlier test in that process called
# asyncio.run(), which leaves no current event loop on exit, that import raises
# "There is no current event loop" and the test fails for a reason unrelated to
# it. Serial order happened to dodge this; pytest-xdist's distribution does not.
try:
    import ib_insync  # noqa: F401
except ImportError:
    # Not installed: the tests that need it importorskip on their own.
    pass
