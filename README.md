# Agent Surface Board

A portable skill/runtime that gives a coding agent a persistent, versioned, browser-based
review board — without hand-building a frontend for every workflow. The full design
rationale and normative spec lives in [`docs/SPEC.md`](docs/SPEC.md); the behavioral spec
an agent actually loads to use this is [`SKILL.md`](SKILL.md).

**Model:** `Board UI → SQLite store/inbox → Supervisor → Worker`, with a separate async
job path for slow generation (images, video, etc). The store is the source of truth; the
worker is a replaceable, disposable process; the board is a thin renderer over the store.

## Current status

Phases 0–5 are built and merged into `main`: a working runtime with a real (optional)
coding-agent in the loop:

- **SQLite-backed store** with immutable versions, provenance tracking, and DAG-aware
  staleness propagation (selecting a new upstream version marks descendants stale without
  destroying them; locked descendants are flagged, not auto-regenerated).
- **Async job + event system**: slow generation runs as a job the poller tracks; events are
  at-least-once and idempotent, so crash/redelivery never duplicates effects.
- **Crash recovery**: `surface recover` reclaims leases and resumes in-flight work after a
  worker or supervisor crash, with `--force-reclaim` for stuck leases.
- **Web board UI** (`surface/board_web.py`, FastAPI + hand-written HTML/CSS/JS) with several
  view types — flat list, status columns, and a new **grid** view for image/video tiles with
  a per-tile version switcher — plus Mermaid diagram rendering, server-side version diffs,
  and a live dependency-graph view.
- **Real coding-agent backends**: `opencode`, `codex`, and `agy` can sit behind the board's
  `revise` action over the real production dispatch path (not a demo shim), selected with
  `surface serve --agent <name>`.

Per-phase build notes (what was delivered, what was found by review, what's still rough) are
tracked under `ongoing/phase-1-2-harden-generalize/STATE.md`, `ongoing/phase-3-board-web-redesign/STATE.md`,
and `ongoing/phase-4-visual-planning-extensions/STATE.md`.

---

## For everyday users

Think of this as a **persistent project board that an AI agent works on in the background**,
instead of a chat window. You open a browser tab, see the current state of the work (a draft,
an image, a task list — whatever the project is), and you approve, revise, or comment. The
agent picks up your feedback asynchronously and updates the board. Nothing is lost if you close
the tab, restart your machine, or the agent process crashes — the board survives all of it,
because every piece of state lives in a SQLite file, not in a chat transcript you'd have to
re-explain next time.

The simplest possible example — a one-artifact "poem" project with no real agent attached
(the board still works without one; it just won't auto-revise your feedback):

```bash
pip install -e .
pip install -e ".[board]"     # FastAPI + uvicorn, needed for the board UI

surface --db .surface-board/state.sqlite3 init --stage poem
surface --db .surface-board/state.sqlite3 serve
```

`serve` prints a `http://127.0.0.1:<port>` URL with a one-time auth token baked into it.
Open that URL and you'll see a single card with the poem's current text, its status, and
buttons for the actions that stage allows (e.g. approve, revise). Edit it, approve it, or
leave it — the state is saved immediately in the SQLite file, and reopening the same `--db`
path later shows you exactly where you left off.

To actually have an agent write and revise content for you (rather than just hosting a blank
board), add `--agent <name>` when you `serve` — see "Real agent backends" below.

### Example surfaces

The architecture below is one skeleton — board + store + supervisor + worker — with the
*worker* and *stage config* swapped out per app. Two concrete examples built on it:

- **Haiku board** (a minimal example) — a one-box app: type a topic, click Generate, and the
  worker calls an LLM to write a fresh haiku, shown on a yellow sticky note. Each generate is
  an immutable version, so you can look back at every haiku ever written in that session — the
  "undo history" is free, it's just the version table.

  ![Haiku board](docs/images/surface_poem.webp)

- **Migration surface** (a real-world example) — a data-migration console for moving rows from
  one dataset to another. The left column lists source rows (matched/unmatched), the middle
  panel compares a matched pair field-by-field with price deltas and an editable "final" column,
  and the right column shows the live target dataset plus a running report (rows, matched,
  migrated, unmatched counts). Clicking "Migrate" writes a new version of the target dataset, so
  the version history doubles as a full audit trail of every migration decision.

  ![Migration surface](docs/images/surface_migration.webp)

Both are the same pattern: the board is a thin, deterministic renderer over the SQLite store
(it never holds state itself — clicks become inbox events, polling shows fresh state); a
Supervisor claims events and dispatches them to a worker; the worker is the only part that's
actually different between the two apps (haiku-writing vs. row-matching), and plugs in via the
same stage-config + worker contract described in "Writing a new stage config" below.

---

## For programmers

### Architecture at a glance

```
Board UI (web) ──read/write──▶ SQLite Store ◀──jobs/results── Job Poller ──▶ Providers
                                     │                                      (image/video)
                                     ▼
                                Supervisor
                                     │
                            WorkerTransport (native subprocess / resume fallback)
                                     │
                                     ▼
                            Worker (python -m surface.worker, or a real agent backend)
```

- **Store is truth.** All project state lives in SQLite; worker context is disposable.
- **Versions are immutable.** Revisions create new versions; nothing is deleted.
- **Dependencies are a DAG.** Selecting a new upstream version marks descendants stale
  (never destroys them); locked descendants are flagged but not auto-regenerated.
- **Generation is always async.** Slow work becomes a job; the worker never blocks a turn
  waiting on it.
- **Events are at-least-once, idempotent.** Redelivery after a crash does not duplicate
  effects (versions/jobs are deduplicated by the originating event id).
- **Renderer is replaceable.** `board.py` builds a renderer-independent display model;
  `board_web.py` is the shipped renderer, but it's not the only possible one (see the
  `04-live-surface` pattern referenced from `SKILL.md`).

See [`docs/SPEC.md`](docs/SPEC.md) for the full normative specification.

### Store, events, and jobs

The store (`surface/store.py`, `surface/schema.py`) owns artifacts, immutable versions, DAG
dependencies with stale propagation, an at-least-once event inbox with lease-based claiming,
async jobs, budgets, and locks. The `Supervisor` claims events from the inbox and dispatches
them to a worker over a `WorkerTransport` (native subprocess, with a resume fallback); the
`Poller` independently drives async jobs (image/video generation, etc.) against pluggable
providers. You can drive all of this directly from the CLI without ever opening the board —
`surface store ...` and `surface inbox ...` below operate straight on the store.

### Writing a new stage config

Don't guess at the schema — the two canonical references are:

1. [`docs/stage-config-guide.md`](docs/stage-config-guide.md) — the schema reference, including
   the `board_view` field, which is now an **object** with a `type` key
   (`{"type": "list"}`, `{"type": "columns"}`, or `{"type": "grid", "group_by": ..., "media_field": ...,
   "title_field": ..., "badge_fields": [...]}`). A bare string like `"board_view": "columns"` is
   still accepted and normalized automatically for backward compatibility.
2. The **"Planning a New Surface"** section near the top of [`SKILL.md`](SKILL.md) — a
   methodology to work through *before* writing JSON: what's the unit of work, what depends on
   what, is this a pipeline or a conversation, what does the user need to see to decide, what
   collection view fits, and which actions actually need a worker. Read this before picking a
   `board_view` — the view type is a consequence of those answers, not an independent choice.

Custom stage configs are plain JSON validated against `surface/stages/config.py`.

### Real agent backends

`surface serve --agent <name> [--agent-model <id>] [--agent-timeout <seconds>]` puts a real
coding-agent CLI behind the board's `revise` action, through the real production dispatch
path (`Supervisor` → `NativeStreamTransport` → `surface worker` subprocess), not a bypass
script. Registered backends, from `surface/agents/registry.py`:

- `opencode` (`surface/agents/opencode_backend.py`)
- `codex` (`surface/agents/codex_backend.py`)
- `agy` (`surface/agents/agy_backend.py`)
- `fake` (`surface/agents/fake_backend.py`) — deterministic, no network call; used by the test
  suite and for exercising the dispatch path without a live CLI.

See [`docs/agent-backends.md`](docs/agent-backends.md) for exact non-interactive invocation
templates and model ID formats per backend. To wire up a new backend, implement the
`AgentBackend` interface in `surface/agents/base.py` and register it in
`surface/agents/registry.py`.

### Tests

```bash
PYTHONPATH=. pytest -q
```

Tests live under `tests/`. The suite runs the full stack (board/CLI → event → supervisor →
worker subprocess → store) through the production dispatch path using the `fake` backend, so
crash/timeout/recovery behavior is verified without needing a live agent CLI or network
access.

### Project layout

```
surface/
  store.py, schema.py       # SQLite store — source of truth
  supervisor.py              # event claim/dispatch loop
  transports/                # WorkerTransport implementations
  worker.py                  # reference deterministic worker
  poller.py, providers/      # async job polling + mock generation providers
  board.py                   # display model + action handler (renderer-independent)
  board_web.py, static/      # default board renderer (FastAPI + hand-written HTML/CSS/JS)
  agents/                    # real agent backends (opencode, codex, agy, fake) + registry
  stages/                    # stage config loader + bundled presets
  cli.py                     # surface CLI entrypoint
tests/
docs/SPEC.md                 # normative specification
docs/stage-config-guide.md   # stage config schema reference
docs/agent-backends.md       # per-backend CLI invocation details
SKILL.md                     # the behavioral spec / "how to use this" doc an agent loads
ongoing/                     # phase build logs (STATE.md per phase)
```

---

## Quickstart (CLI-only, verified against `surface/cli.py`)

Requires Python 3.10+.

```bash
pip install -e .              # installs the `surface` command
pip install -e ".[board]"     # FastAPI + uvicorn, for the web board

# --db is a global flag; it comes BEFORE the subcommand.
# Initialize a new project using the built-in "poem" stage preset
surface --db .surface-board/state.sqlite3 init --stage poem

# Inspect the store directly
surface --db .surface-board/state.sqlite3 store list

# Run the full runtime: store + supervisor + poller + board
surface --db .surface-board/state.sqlite3 serve

# Run with a real agent backend behind the board's "revise" action
surface --db .surface-board/state.sqlite3 serve --agent codex --agent-model claude-sonnet-4

# Or run headless (no board), bounded for scripting/testing:
surface --db .surface-board/state.sqlite3 serve --no-board --max-iterations 5
```

`serve` runs until Ctrl-C by default (`--run-once` or `--max-iterations N` to bound it). It
binds the board to `127.0.0.1` (`--host`/`--port` to change) with a random per-run auth token
written under the runtime dir (`--runtime-dir` to change it), never a fixed/guessable
credential. The board dependency (fastapi/uvicorn) is optional at import time — `serve` logs
and runs headless if it's missing.

## CLI reference

`--db PATH` is a **global** flag — it must come before the subcommand (default:
`.surface-board/state.sqlite3`).

```
surface [--db PATH] init --stage <preset>
surface [--db PATH] status                                # summarize project state
surface [--db PATH] tui                                    # stdlib terminal board
surface [--db PATH] serve [--run-once] [--max-iterations N] [--no-board]
                          [--renderer {web,gradio}] [--host HOST] [--port PORT]
                          [--runtime-dir DIR] [--poll-interval SECONDS]
                          [--keep-runtime-files] [--pool-size N]
                          [--recycle-after-events N] [--recycle-after-tokens N]
                          [--agent NAME] [--agent-model ID] [--agent-timeout SECONDS]
surface [--db PATH] stop
surface [--db PATH] interrupt
surface [--db PATH] recover [--force-reclaim]

surface [--db PATH] render <preset_or_path> --data <file.json>
surface [--db PATH] wait <handle> --max <seconds>
surface [--db PATH] answer <handle> --value <json>

surface [--db PATH] store get <artifact_id>
surface [--db PATH] store list [--stage S] [--status S]
surface [--db PATH] store put-version [--artifact-id ID] [--file FILE]   # or JSON via stdin
surface [--db PATH] store select-version <artifact_id> <version_id>
surface [--db PATH] store set-status <artifact_id> <status>
surface [--db PATH] store add-dependency <upstream_id> <downstream_id>
surface [--db PATH] store graph <artifact_id>
surface [--db PATH] store job-start <artifact_id> <provider> <kind>
surface [--db PATH] store job-finish <job_id>
surface [--db PATH] store job-cancel <job_id>

surface [--db PATH] inbox next [--wait SECONDS]
surface [--db PATH] inbox ack <event_id>
surface [--db PATH] inbox fail <event_id>

surface [--db PATH] project summary
```

JSON results go to stdout; diagnostics go to stderr.

## Stage presets

Bundled presets, all in `surface/stages/`:

- **`poem`** — the minimal single-artifact example used in the everyday-user quickstart above.
- **`movie`** — a linear pipeline (script → shots → keyframes → clips → assembly) for
  video/media production, with per-stage allowed actions, dependencies, and generation
  defaults.
- **`task_board`** — a status-column board for agent-decomposed multi-step work, typically
  used with `board_view: {"type": "columns"}`.
- **`image_grid_example`** — demonstrates the `grid` board view (`{"type": "grid", ...}`) for
  image/video tiles with a per-tile version switcher.
- **`project_kickoff`**, **`questionnaire`**, **`plan_review`**, **`document_review`**,
  **`diff_review`**, **`generic_media_pipeline`** — templates for common review/intake
  workflows.

Custom stage configs are plain JSON validated against `surface/stages/config.py`; see
[`docs/stage-config-guide.md`](docs/stage-config-guide.md) for the full schema.

## Web board rendering features

The web renderer (`surface/board_web.py`) includes:

- **Multiple collection views**, via the `board_view` config field: `list` (flat, default),
  `columns` (grouped by status), and `grid` (image/video tiles with a per-tile version
  switcher).
- **Mermaid diagram support**: any text/markdown artifact with fenced `` ```mermaid ``` ``
  blocks renders them as diagrams automatically.
- **Version diff**: text artifacts with 2+ versions show a "Compare to previous version"
  control, with server-side line-by-line diffs computed fresh on each load. Not shown for
  form/image/video/audio content.
- **Dependency graph view**: a "Dependencies" button on any card shows its upstream/downstream
  neighbors as a status-colored Mermaid graph, updating live without restart.

## Real agent backends

`surface serve --agent <name> [--agent-model <id>] [--agent-timeout <seconds>]` puts a real
coding-agent CLI (`opencode`, `codex`, or `agy`) behind the board's `revise` action, through
the real production dispatch path: `Supervisor` → `NativeStreamTransport` → `surface worker`
subprocess, no bypass scripts. See [`docs/agent-backends.md`](docs/agent-backends.md) for
exact non-interactive invocation templates and model ID formats per backend.

A `fake` backend (deterministic, no network call) exists for all unit/integration tests —
run `PYTHONPATH=. pytest -q` to exercise the full stack through the production dispatch path
with it, proving timeouts, crashes, and recovery all work without a real agent CLI or network.

## Current limitations / known gaps

- **CLI-only.** There's no desktop launcher or packaged installer for Windows/Linux (or
  macOS) — you run `pip install -e .` and use the `surface` command directly.
- **Grid view options aren't read yet.** `board_view: {"type": "grid", ...}`'s `group_by`,
  `media_field`, `title_field`, and `badge_fields` options pass config validation but are not
  yet consumed by the renderer (tiles render with defaults regardless).
- **Some view types are deliberately not built**: gantt, calendar, saved filters, and a
  report builder are explicitly out of scope for now — `list`, `columns`, and `grid` are the
  full set.
- **One live-CLI test for the `agy` backend has a known, environment-specific failure** (a
  stale hardcoded model name), unrelated to product code.
- `--renderer gradio` is a legacy path (needs the separate `pip install -e ".[gradio]"` extra
  and gradio `>=4.0,<6` specifically). `web` (the default) is the actively developed renderer
  and the one described above; prefer it unless you have a specific reason not to.
