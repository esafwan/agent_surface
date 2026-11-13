#!/usr/bin/env python3
"""
Standalone fake agent CLI for tests. No repo imports.

Usage: python3 fake_agent.py "<prompt text>"

Behavior is controlled by env vars:
  FAKE_AGENT_MODE: echo (default) | crash | hang | garbage | slow_ok
  FAKE_AGENT_SLEEP: seconds to sleep for hang/slow_ok modes (defaults: 60 / 2)
  FAKE_AGENT_COUNTER_FILE: if set, append one line per invocation (for counting)
"""

import os
import sys
import time

SENTINEL_START = "<<<SURFACE_CONTENT"
SENTINEL_END = "SURFACE_CONTENT>>>"


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    mode = os.environ.get("FAKE_AGENT_MODE", "echo")

    counter_file = os.environ.get("FAKE_AGENT_COUNTER_FILE")
    if counter_file:
        try:
            with open(counter_file, "a") as f:
                f.write("run\n")
        except Exception:
            pass

    if mode == "crash":
        print("fake agent crashed", file=sys.stderr, flush=True)
        return 1

    if mode == "hang":
        sleep_s = float(os.environ.get("FAKE_AGENT_SLEEP", "60"))
        time.sleep(sleep_s)
        print(f"{SENTINEL_START}\nnever gets here\n{SENTINEL_END}", flush=True)
        return 0

    if mode == "garbage":
        print("this is unrelated output with no sentinels", flush=True)
        sys.stdout.flush()
        return 0

    if mode == "slow_ok":
        sleep_s = float(os.environ.get("FAKE_AGENT_SLEEP", "2"))
        time.sleep(sleep_s)
        print(f"{SENTINEL_START}\nOK\n{SENTINEL_END}", flush=True)
        return 0

    # default: echo
    print(f"{SENTINEL_START}\n{prompt}\n{SENTINEL_END}", flush=True)
    return 0


if __name__ == "__main__":
    sys.stdout.flush()
    rc = main()
    sys.stdout.flush()
    sys.exit(rc)
