#!/usr/bin/env python3
"""Convert data/<user>/raw into data/<user>/tcx. The user defaults to default."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from keep_convert import main_tcx

if __name__ == "__main__":
    main_tcx()
