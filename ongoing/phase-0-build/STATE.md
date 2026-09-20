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
1. Create `PLAN.md` with detailed module specifications and execution steps.
2. Implement **Module 1: SQLite Store** (`schema.py`, `store.py`) and verify with unit tests.
3. Implement **Module 2: Stage Configs** (`stages/movie.json`).
4. Implement **Module 3: Supervisor & Worker Transport** (`supervisor.py`, `transports/`).
5. Implement **Module 4: Board UI** (`board.py`).
6. Implement **Module 5: Async Jobs & Poller** (`poller.py`, `providers/`).
7. Implement **Module 6: Skill Packaging & CLI** (`cli.py`, `surface/`, `SKILL.md`).
8. Execute Integration Walkthrough Test (`tests/`).
