# State: Phase 0 Build ("Prove the Loop")

## Current Status
- **Phase**: Phase 0 Initialized
- **Track**: `ongoing/phase-0-build/`
- **Specification**: `docs/SPEC.md`
- **Goal**: Implement the core `agent_surface` board runtime and prove the full interactive movie generation loop end-to-end.

---

## Objectives
1. Build core SQLite store (`store.py`, `schema.py`) to manage artifacts, immutable versions, DAG dependencies, event log, jobs, and lease claims.
2. Implement Stage Config loader for human-in-the-loop workflows (`stages/movie.json`).
3. Build Worker & Transport system (`supervisor.py`, `transports/native_stream.py`, `transports/resume.py`) to isolate agent turns from long-running async generation.
4. Implement Gradio Board UI (`board.py`) supporting text/image/video artifact renderers and user actions (edit, revise, regenerate, select, approve, cancel, message).
5. Build Async Job Poller (`poller.py`, `providers/`) for off-turn media generation.
6. Package runtime as OpenClaw Skill and CLI (`SKILL.md`, `cli.py`, `surface/`).
7. Implement integration walkthrough test suite (`tests/`) verifying script editing → stale shot marking → approval → async image generation → revision → clip generation → job cancellation → worker failure recovery.

---

## Key Architectural Decisions (Section 60 of SPEC.md)
1. **Store is source of truth**: SQLite is the v1 durable coordination mechanism; all state, versions, events, and jobs live in the store.
2. **Agent context is disposable**: Worker/agent rehydrates from the store and can be killed/restarted at any time without losing state.
3. **Core components**: Board + Inbox + Worker + Artifact Store.
4. **Blocking Q&A is convenience only**: Non-blocking async user interactions and worker turns.
5. **SQLite for v1 durable coordination**: Provides ACID guarantees, event logging, and lease locks.
6. **Immutable Versions & DAG Dependencies**: Every mutation creates a new version; dependencies form a DAG; upstream version updates mark descendants as `stale` while retaining previous versions.
7. **Async Generation outside worker turns**: Slow media operations (images, video) are handled by background jobs and pollers, keeping worker turns short and non-blocking.
8. **Events & Leases**: Event log guarantees at-least-once processing; worker job claims use time-bounded lease locks (`claim_owner`, `claim_expires_at`).
9. **WorkerTransport isolates harnesses**: Abstract transport layer isolates CLI/harness nuances (`native_stream` bootstrap, `resume` fallback, future ACP).
10. **Gradio is renderer, not protocol**: Board UI uses Gradio as an interactive interface renderer without binding core runtime protocol to frontend components.

---

## Next Steps
1. Create `PLAN.md` with detailed module specifications and execution steps. ✅
2. Implement **Module 1: SQLite Store** (`schema.py`, `store.py`) and verify with unit tests. ✅
3. Implement **Module 2: Stage Configs** (`stages/movie.json`). ✅
4. Implement **Module 3: Supervisor & Worker Transport** (`supervisor.py`, `transports/`). ✅
5. Implement **Module 4: Board UI** (`board.py`). ✅ (see Known Gap below)
6. Implement **Module 5: Async Jobs & Poller** (`poller.py`, `providers/`). ✅
7. Implement **Module 6: Skill Packaging & CLI** (`cli.py`, `surface/`, `SKILL.md`). ✅
8. Execute Integration Walkthrough Test (`tests/`). ✅

## Status as of 2026-09-20: Phase 0 loop is real, closed after two adversarial review rounds

All six modules plus a genuine end-to-end test (`tests/test_movie_walkthrough.py`) and a
reference worker (`surface/worker.py`) are implemented and committed (252 tests passing,
1 skipped for gradio-not-installed-in-this-environment).

The initial build (commits `321225d`..`102cd47`) passed its own tests but a senior
adversarial review found the core `board -> event -> supervisor -> worker` loop had
**never actually run** — `serve()` built a Supervisor and never used it, no worker process
existed, and the walkthrough test only exercised Store+Poller directly. This was closed in
commit `4120c4e` (added `surface/worker.py`, fixed an envelope bug in `native_stream.py`
that dropped the domain event type, wired `serve()` to really dispatch), then a second
review round found four fixes had themselves introduced real bugs (timeout/stream desync,
budget cost always read as 0, non-idempotent job creation under retry, cancel-before-submit
race) — all closed in commits `4fb0ee3` and `6b8bbd1`.

The end-to-end loop has been manually verified working (not just unit-tested): an `edit`
event seeded directly into the store was claimed by a live `Supervisor`, dispatched to a
real `python -m surface.worker` subprocess, and produced a correctly-attributed version.

### Known deliberate gap: board live-refresh (not fixed)

`surface/board.py`'s Gradio wiring still builds its artifact/version card tree once at
launch; `refresh_board()` updates only summary text, not the live card tree. A second
review round flagged this as significant. Fixing it properly requires a `gr.Timer`- or
bounded-slot-driven rebuild of the whole card tree — a nontrivial rewrite of Gradio wiring
that this environment cannot runtime-verify (gradio is not installed here), which is
exactly the blind spot that produced the S3/S4/S6/S7/S8 regressions fixed above. Left open
rather than risk another unverified rewrite. **Do this first in Phase 1**, with gradio
actually installed so the fix can be run, not just read.

Also open/never attempted this round (smaller, lower severity): `SupervisorConfig.turn_timeout`
is set but not plumbed into the transport (the real bound is `NativeStreamTransport`'s own
`timeout` default); board action handlers gate on `allowed_actions` only at the button-render
layer, not inside the handler functions themselves, so a programmatic/replayed call bypasses
stage permission checks.
