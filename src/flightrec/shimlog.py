"""Tiny helper invoked by the PATH shims (see ``shim.py``).

    python -m flightrec.shimlog start <real> <argv...>   -> prints a record id
    python -m flightrec.shimlog end   <id> <exit_code>

Appends raw JSON lines to $FLIGHTREC_EXEC_LOG. Kept dependency-free and
minimal because it runs for every command the agent executes.
"""

import json
import os
import sys
import time
import uuid


def main(argv: list[str]) -> int:
    log = os.environ.get("FLIGHTREC_EXEC_LOG")
    if not log or len(argv) < 2:
        return 0
    mode = argv[0]
    rec: dict
    if mode == "start":
        rec = {
            "id": uuid.uuid4().hex[:12],
            "phase": "start",
            "ts": time.time(),
            "real": argv[1],
            "argv": argv[2:],
            "cwd": os.getcwd(),
        }
        sys.stdout.write(rec["id"])
    elif mode == "end":
        rec = {"id": argv[1], "phase": "end", "ts": time.time(),
               "exit_code": int(argv[2]) if len(argv) > 2 else None}
    else:
        return 0
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
