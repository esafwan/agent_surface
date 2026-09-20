# Agent Surface Board

A portable skill/runtime that gives a coding agent a persistent, versioned, browser-based
review board — without hand-building a frontend for every workflow. The full design
rationale and normative spec lives in [`docs/SPEC.md`](docs/SPEC.md).

**Model:** `Board UI → SQLite store/inbox → Supervisor → Worker`, with a separate async
job path for slow generation (images, video, etc). The store is the source of truth; the
worker is a replaceable, disposable process; the board is a thin renderer over the store.

## Status: Phase 0 ("Prove the Loop")

Phase 0 is implemented and the core loop has been verified working end-to-end — not just
unit-tested. Details, what was found by adversarial review, and what's fixed vs. still
open are tracked in [`ongoing/phase-0-build/STATE.md`](ongoing/phase-0-build/STATE.md).

**What works right now:**
- Full CLI (store/inbox/project operations, `init`, `status`, `serve`) — usable without any UI.
- SQLite store: artifacts, immutable versions, DAG dependencies with stale propagation,
  at-least-once events with lease-based claiming, jobs, budgets, locks.
- A real reference worker (`python -m surface.worker`) that interprets events
  deterministically and writes through the store — proving the architecture without
  requiring a real LLM in the loop.
- A real async job poller with mock image/video providers (deterministic, no network calls).
- `surface serve` genuinely wires store + supervisor + poller + worker + board together,
  with an authenticated loopback bind (unpredictable per-run token) for remote-adjacent access.

**Status of the Gradio board:** live-tested in a real browser (gradio 5.50 — see below for
why the version matters). What's confirmed working: thread-safe under real Gradio request
dispatch, JSON-Schema-driven form rendering for `form`-typed artifacts (native text/
dropdown/number/checkbox controls instead of a raw JSON blob), and a desktop-width layout.
**Known gap:** the board builds its artifact/version display once at launch — it does not
yet live-refresh when versions/status change without restarting the process. This is the
top remaining item for Phase 1. The stdlib `surface tui` command (see below) has no such
gap and needs no gradio install at all.

**Gradio version pin:** use gradio `>=4.0,<6` (`pip install -e ".[board]"` already pins
this). Gradio 6's frontend (a Svelte 5 rewrite) throws a `effect_orphan` error and renders
a blank page with this board as of gradio 6.28 — confirmed by live testing, not a
theoretical incompatibility. Gradio 5.50 renders correctly.

## Quickstart

Requires Python 3.10+.

```bash
pip install -e .              # installs the `surface` command
pip install -e ".[board]"     # also installs gradio, for the board UI

# --db is a global flag; it comes BEFORE the subcommand.
# Initialize a new project using the built-in "movie" stage preset
surface --db .surface-board/state.sqlite3 init --stage movie

# Inspect the store directly
surface --db .surface-board/state.sqlite3 store list

# Run the full runtime: store + supervisor + poller + board
surface --db .surface-board/state.sqlite3 serve

# Or run headless (no board), bounded for scripting/testing:
surface --db .surface-board/state.sqlite3 serve --no-board --max-iterations 5
```

`serve` runs until Ctrl-C by default. If gradio is installed, it launches the board on
`127.0.0.1` with a random per-run auth token written to `.surface-board/run/token`
(0600 permissions) — never a fixed/guessable credential.

## CLI reference

`--db PATH` is a **global** flag — it must come before the subcommand (default:
`.surface-board/state.sqlite3`).

```
surface [--db PATH] init --stage <preset>
surface [--db PATH] status                                # summarize project state
surface [--db PATH] tui                                   # stdlib terminal board (no gradio needed)
surface [--db PATH] serve [--no-board] [--max-iterations N]
                          [--host HOST] [--port PORT] [--runtime-dir DIR]
                          [--pool-size N] [--recycle-after-events N]
surface [--db PATH] stop
surface [--db PATH] interrupt
surface [--db PATH] recover [--force-reclaim]

surface [--db PATH] render <preset_or_path> --data <file.json>
surface [--db PATH] answer <handle> --value <json>
surface [--db PATH] wait <handle> --max <seconds>

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

Ships with a `movie` pipeline preset (`surface/stages/movie.json`): script → shots →
keyframes → clips → assembly, with per-stage allowed actions, dependencies, and
generation defaults. Custom stage configs are plain JSON validated against the same
schema (see `surface/stages/config.py`).

## Architecture at a glance

```
Board UI (Gradio) ──read/write──▶ SQLite Store ◀──jobs/results── Job Poller ──▶ Providers
                                       │                                       (image/video)
                                       ▼
                                  Supervisor
                                       │
                              WorkerTransport (native subprocess / resume fallback)
                                       │
                                       ▼
                              Worker (python -m surface.worker)
```

- **Store is truth.** All project state lives in SQLite; worker context is disposable.
- **Versions are immutable.** Revisions create new versions; nothing is deleted.
- **Dependencies are a DAG.** Selecting a new upstream version marks descendants stale
  (never destroys them); locked descendants are flagged but not auto-regenerated.
- **Generation is always async.** Slow work becomes a job; the worker never blocks a turn
  waiting on it.
- **Events are at-least-once, idempotent.** Redelivery after a crash does not duplicate
  effects (versions/jobs are deduplicated by the originating event id).

See [`docs/SPEC.md`](docs/SPEC.md) for the full normative specification.

## Development

```bash
pip install pytest
python -m pytest -q     # 250+ tests
```

Project layout:

```
surface/
  store.py, schema.py       # SQLite store — source of truth
  supervisor.py              # event claim/dispatch loop
  transports/                # WorkerTransport implementations
  worker.py                  # reference deterministic worker
  poller.py, providers/      # async job polling + mock generation providers
  board.py                   # Gradio board UI
  stages/                    # stage config loader + presets
  cli.py                     # surface CLI entrypoint
tests/
docs/SPEC.md                 # normative specification
ongoing/phase-0-build/       # phase plan + current build state
```
