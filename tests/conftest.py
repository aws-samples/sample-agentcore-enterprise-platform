import sys
from pathlib import Path

# Make repo root importable so tests can `import infra_utils...`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os

# The shipped presets carry placeholders (sentinel account ids, REPLACE_ME client
# ids) on purpose, and a real deploy refuses them (tests/test_placeholders.py).
# Every other test that loads a preset describes it as it ships, so the suite
# runs the way scripts/check-contract.sh does. test_placeholders.py deletes this
# for its own strict cases.
os.environ.setdefault("PLATFORM_ALLOW_PLACEHOLDERS", "1")
