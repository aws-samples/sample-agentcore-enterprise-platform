"""deploy.sh counts stacks with `list-stacks --query length(...)`.

The CLI paginates list-stacks. With `--output text` it prints one count per page
("0\t0\t1"), so `[ "$count" = "0" ]` is wrong as soon as an account has more than
100 stack summaries (deleted stacks stay listed for 90 days) and the first deploy
refuses. `--output json` makes the CLI apply the query to the merged result.
"""

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"


def _list_stacks_counts() -> list[str]:
    text = DEPLOY.read_text()
    calls = re.findall(r"aws cloudformation list-stacks\b(?:[^\n]*\\\n)*[^\n]*", text)
    return [c for c in calls if "length(" in c]


def test_every_stack_count_uses_json_output():
    calls = _list_stacks_counts()
    assert calls, "expected list-stacks length() counters in deploy.sh"
    for call in calls:
        assert "--output json" in call, call
        assert "--output text" not in call, call
