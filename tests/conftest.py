import sys
from pathlib import Path

# Let tests import the modules in scripts/ (dq_rules, checks, utils).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
