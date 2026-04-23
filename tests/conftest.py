"""Ensure ``src/`` is on ``sys.path`` regardless of editable-install state.

Why: the project lives under ``~/Documents/`` (iCloud-synced), and iCloud
sets the macOS ``UF_HIDDEN`` flag on files it has processed. uv's editable
install writes ``_editable_impl_ccforensics.pth`` into site-packages;
when iCloud hides it, ``site.py`` silently skips it, breaking
``import ccforensics`` at test collection.

Prepending ``src/`` here is defense-in-depth — the editable install
still works when not hidden, but tests don't depend on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
