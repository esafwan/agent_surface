# Examples

One directory per pattern named in `SKILL.md`. Each is self-contained,
runnable on its own, and writes its own SQLite store inside its own
directory (gitignored — delete it to start over).

| # | Pattern | What it proves |
|---|---|---|
| [01-board-basics](01-board-basics/) | Edit → Approve, Async Generation, Stale Propagation | The board's three named patterns, end to end, against the `movie` preset |
| [02-agent-as-worker](02-agent-as-worker/) | Agent as the worker | The board running *without* the supervisor, so an agent — not the no-op reference worker — claims events and writes real content |
| [03-qa-roundtrip](03-qa-roundtrip/) | Blocking convenience mode | `render` → `wait(max)` → `answer`, the bounded Q&A API for a single structured question |
| [04-live-surface](04-live-surface/) | Agent-rendered UI | The synchronous alternative to the board: the agent's own response declares the surface (`mode`/`blocks`/`controls`), for turn-taking work where a queue would be the wrong shape |

None of these require Gradio to be installed except 01 and 02 (they use the
board renderer). 03 and 04 have no board UI at all.

## Which one to read first

- Want to understand the **board** (the versioned, non-linear-review half of
  this skill)? Start with 01, then 02.
- Want to understand the **live surface** (the synchronous, agent-declares-
  the-UI half)? Start with 04, then 03 for the API it's built on.

## A note on 04's task

`04-live-surface/loop.py` ships with a poem-writing task by default, but the
loop itself is generic — the schema, normalizer, and renderer never mention
poems. Everything domain-specific lives in one string (`DEFAULT_TASK`) that
`--task` / `--task-file` replace. The example's own README shows a recipe
agent running from the same file with no code changes, to make that claim
checkable rather than asserted.
