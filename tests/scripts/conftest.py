"""Make scripts/ importable for the tests of its helper modules.

scripts/ is not a package and is not installed with py-common, so its
modules are imported by putting the folder on sys.path.
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
