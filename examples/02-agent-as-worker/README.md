# 02 — Agent as the Worker

`surface serve` always starts a supervisor that dispatches every inbox event
to the reference worker (`python -m surface.worker`), which has no model —
on `revise` it re-commits the previous content unchanged. To put a real
agent behind the board instead, the board has to run *without* that
supervisor, so an agent can claim events itself. There is no CLI flag for
that (`--no-board` is the opposite); `board_only.py` calls `build_board()`
directly to get there.

No auth by default — it binds loopback, so a login wall buys nothing for a
single-user local app. Pass `--pin 1234` if you want one anyway; a non-
loopback `--host` requires it.

## Two ways to run this, depending on what you're testing

**Testing the mechanism** (you play the worker, by hand, in a second
terminal — below). Good for seeing exactly what an event/claim/ack cycle
looks like.

**Testing the user experience** (an agent plays the worker, invisibly, and
you only ever touch the browser). This is the shape the skill actually
targets: an agent exposes the UI, a human uses it, with no CLI in between.
If you're doing this, don't run terminal 2 yourself — ask an agent to spawn
a **sub-agent** that holds the `inbox next --wait N` loop, so the agent
you're talking to stays free while you click around. A blocking poll loop
held by the agent you're actively conversing with means it can't respond to
you at all for the length of the loop — that's the wrong agent to hold it.

## Run (mechanism test)

Terminal 1 — start the board (seeds one empty `poem` artifact on first run):

```bash
python examples/02-agent-as-worker/board_only.py
```

It prints the URL and the exact command to claim events with. Open the
board, type a request in the Revise box, and click Revise.

Terminal 2 — be the worker:

```bash
surface --db examples/02-agent-as-worker/state.sqlite3 inbox next --wait 60
```

This claims the event and prints it, including the note you typed. Write a
real version for it — by hand, or from an agent:

```bash
python3 -c "
from surface.store import Store
s = Store('examples/02-agent-as-worker/state.sqlite3')
s.put_version('poem_1', content='whatever you want here',
               content_type='text/markdown', created_by='worker',
               source_event_id='<the event id inbox next printed>')
"
surface --db examples/02-agent-as-worker/state.sqlite3 inbox ack <event_id>
```

Refresh the board (or wait ~2s — it polls) and the new version is there.

## What this proves

The claim/write/ack loop in terminal 2 *is* being the worker. Nothing about
it is specific to a human typing those commands — an agent doing exactly
this (claim, generate, write, ack) is the whole mechanism `surface serve`'s
supervisor automates once it has a worker with a model behind it. This
example exists because the shipped one doesn't.
