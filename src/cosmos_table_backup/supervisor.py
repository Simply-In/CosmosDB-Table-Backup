"""Bounded private-console collection window for governed job executions."""

from __future__ import annotations

import os
import sys
import time


def collection_window() -> None:
    value = os.environ.get("GOVERNED_CONSOLE_HOLD_SECONDS", "0")
    if value not in {"0", "180"}:
        raise ValueError("invalid governed console collection window")
    sys.stdout.flush()
    sys.stderr.flush()
    if value == "180":
        time.sleep(180)
