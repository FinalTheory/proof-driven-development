from __future__ import annotations

import sys
from pathlib import Path

# Keep test execution independent of working-directory quirks and prevent runtime
# bytecode caches from polluting the repository tree.
sys.dont_write_bytecode = True
CORRECTNESS_ROOT = Path(__file__).resolve().parents[1]
if str(CORRECTNESS_ROOT) not in sys.path:
    sys.path.insert(0, str(CORRECTNESS_ROOT))
