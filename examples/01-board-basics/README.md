# 01 — Board Basics

The three patterns named in `SKILL.md`'s "Preferred Patterns" section, run
back to back against the `movie` preset (script → shots → keyframes).

| Step | Pattern |
|---|---|
| 2–3 | **Pattern 1: Edit → Approve Loop** — write a script, approve it |
| 5 | **Pattern 3: Stale Propagation** — revise the approved script and watch `shots` and `keyframes` flip to `stale` without being touched |
| 7–8 | **Pattern 2: Async Image Generation** — a job is created, a poller (simulated inline here) completes it, a version appears |

## Run

```bash
python examples/01-board-basics/run.py
```

Prints each step as it happens: events enqueued, the reference worker
claiming and acking them, and artifact status after each one. No board UI —
this exercises the store and worker directly, which is the fastest way to
see the state machine without a browser in the way.

Writes `board.sqlite3` in this directory. Delete it to run again from
scratch; the script is not idempotent against a pre-existing store (it
`create_artifact`s unconditionally).

## What to look at

Step 5 is the interesting one. The script gets a new version, and *nothing
downstream is touched* — `shots_001` and `kf_001` keep their old approved
content, just flagged `stale`. That is SPEC principle 8, "stale, never
destroy": the old versions are still there, inspectable, until a human
decides whether to regenerate, revert, or accept the mismatch.
