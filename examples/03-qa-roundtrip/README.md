# 03 — Blocking Convenience Mode (`render` / `wait` / `answer`)

SPEC section 35 scopes this to small workflows: one structured question, no
board, no versions, no dependency graph. It's the escape hatch for when
standing up a whole board would be overkill, without falling back to a tool
call that blocks indefinitely.

```
render   persists a stage config + data as a durable handle
wait     polls that handle, bounded by --max seconds — never blocks forever
answer   the missing write side: something writes the answer into the handle
```

## Run

```bash
python examples/03-qa-roundtrip/run.py
```

This plays both halves in one process so it runs unattended: it renders a
question, then a background thread answers it 2 seconds later — standing in
for a human clicking on a board, or literally typing
`surface answer <handle> --value '...'` in another terminal.

## The manual two-terminal version

Terminal 1:

```bash
surface --db examples/03-qa-roundtrip/state.sqlite3 render questionnaire --data question.json
# -> {"status": "pending", "handle": "interaction_...", ...}
surface --db examples/03-qa-roundtrip/state.sqlite3 wait interaction_... --max 60
# blocks here, bounded at 60s
```

Terminal 2, before the 60s is up:

```bash
surface --db examples/03-qa-roundtrip/state.sqlite3 answer interaction_... --value '{"text":"Aurora"}'
```

Terminal 1 returns `{"status": "answered", "value": {"text": "Aurora"}}` as
soon as terminal 2 runs. If nothing answers in time, terminal 1 returns
`{"status": "pending", ...}` instead of hanging — every wait is bounded, and
an agent may call `wait` again later on the same handle.
