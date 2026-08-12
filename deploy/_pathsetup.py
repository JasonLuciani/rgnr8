"""Put the Python package sources on sys.path (no install step needed).

The monorepo isn't pip-installed in the container image; the deploy entrypoints
import the `rgnr8_*` packages straight from `packages/*/src`. Importing this
module wires that up. (In a published build these would be real wheels.)
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _pkg in ("forecast", "briefing", "web", "runtime", "ops"):
    _src = _ROOT / "packages" / _pkg / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))
