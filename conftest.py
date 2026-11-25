"""Root conftest: the `src` layout on the path, so a copied tree (mutmut's `mutants/`) imports `rb_workflows` too."""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "src"
if SOURCE.is_dir() and str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
