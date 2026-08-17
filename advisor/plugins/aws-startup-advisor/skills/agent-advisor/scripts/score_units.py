#!/usr/bin/env python3
"""Score every agent_session unit in answers.json and print scoring-result JSON.

Thin driver over scoring.py (which stays a pure function): loops units,
merges system + unit answers, and mirrors the primary unit's result at the
top level so single-unit consumers that read `scoring-result.verdict` keep
working. Extracted from an inline `python -c` block in clarify.md Step 5 so
skill instructions never ask the agent to run dynamically assembled code
(and the run-dir path travels as argv, not as text interpolated into source).

Usage: uv run score_units.py /path/to/answers.json > scoring-result.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scoring  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    answers = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    entry_point = answers.get("entry_point", "build_scratch")
    units = {
        unit_id: scoring.score(
            {
                "entry_point": entry_point,
                "answers": {
                    **answers["system"],
                    **{
                        k: v
                        for k, v in info.items()
                        if k not in ("workload_class", "provenance")
                    },
                },
            }
        )
        for unit_id, info in answers["units"].items()
        if info.get("workload_class") == "agent_session"
    }
    out = {"units": units}
    # Collapse mirror: the scope gate guarantees >=1 agent_session unit, so
    # `units` is non-empty and a mirror always exists.
    primary = answers.get("primary_unit")
    mirror = units.get(primary) or next(iter(units.values()))
    out.update(mirror)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
