# 04 — Live Surface: the Agent Renders Its Own UI

Contrast with the board: there the UI is fixed by a stage config and the
agent writes into a durable queue, so a click has no visible consequence
until some worker happens to claim it. Here the agent's structured response
*is* the UI — it declares what to draw and which controls are live — and the
call is synchronous, so "working" is always visible on screen.

```jsonc
{
  "mode": "doc" | "chat" | "form",   // which surface; sticky between turns
  "blocks": [ ... ],                 // what to show (verse/prose/code/table/thread)
  "ask": "what to ask the user next",
  "controls": [ ... ],               // what the user can do (buttons/choice/multi/text)
  "done": false
}
```

The agent picks semantics; every visual decision stays in `static/*.css`, so
the agent cannot make the page incoherent. See `SKILL.md`'s "Synchronous
Alternative: Agent-Rendered UI" section for the full contract and its
degradation rules.

## Run — the poem demo

```bash
python examples/04-live-surface/loop.py                # no auth, loopback
python examples/04-live-surface/loop.py --pin 1234      # optional gate
```

Open the printed URL, type what you want, watch it write, Revise or Approve.

## Run — a different agent, same file

The loop is generic. Everything poem-specific lives in one string
(`DEFAULT_TASK`, and the opening `--ask`/`--placeholder`); replace it and
nothing else in the file needs to change:

```bash
python examples/04-live-surface/loop.py \
  --db /tmp/recipes.sqlite3 --port 7874 \
  --ask "What do you want a recipe for?" \
  --placeholder "e.g. weeknight pasta" \
  --task 'Write recipes on request. Use mode "form" with a "prose" block for
a one-line description and a "table" block (columns: ingredient, amount)
for ingredients. Offer buttons ["Approve","Revise"].'
```

Asking for "weeknight pasta aglio e olio" against this produced a `form`-mode
response with a `prose` description, a `table` of ingredients, and a method
— with zero changes to `loop.py`, `static/*.js`, or `static/*.css`. That's
the claim this example exists to make checkable: the contract (`mode`,
`blocks`, `controls`, the degradation rules) is the reusable part; the poem
prompt is just this file's default content.

`--task-file path.txt` works the same way for a longer prompt than fits
comfortably on a command line.

## Flags

| Flag | Default | |
|---|---|---|
| `--task` / `--task-file` | poem-writing | the domain instructions (mutually exclusive) |
| `--ask` | "What do you want me to write?" | opening question |
| `--placeholder` | "e.g. a love poem" | opening input placeholder |
| `--model` | `claude-haiku-4-5-20251001` | passed to `claude -p --model` |
| `--db` | `state.sqlite3` next to this file | the interaction store |
| `--host` / `--port` | `127.0.0.1` / `7872` | bind address; non-loopback requires `--pin` |
| `--pin` | none (no auth) | optional 4- or 6-digit gate |
