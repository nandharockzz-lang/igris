"""Locate Jarvis shared modules (repo flat layout or install-time lib/)."""

from __future__ import annotations

import os
import sys


def ensure() -> str:
    """Put the directory that contains safefile.py on sys.path; return it."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "lib"),
        os.path.join(os.path.dirname(here), "lib"),
        here,
    )
    for candidate in candidates:
        abs_c = os.path.abspath(candidate)
        if not os.path.isfile(os.path.join(abs_c, "safefile.py")):
            continue
        if abs_c not in sys.path:
            sys.path.insert(0, abs_c)
        return abs_c
    if here not in sys.path:
        sys.path.insert(0, here)
    return here
