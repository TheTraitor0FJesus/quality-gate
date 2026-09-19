"""Execute the shared publisher from this exact provider checkout."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quality_gate.publication_cli import main

if __name__ == "__main__":
	raise SystemExit(main())
