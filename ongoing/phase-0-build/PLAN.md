# Plan: Phase 0 Build ("Prove the Loop")

## Overview
Phase 0 implements the minimal end-to-end `agent_surface` board runtime to prove the core interactive loop using a Movie Production stage config (`stages/movie.json`).

---

## Execution Modules

### Module 1: SQLite Store
**Files**: `agent_surface/schema.py`, `agent_surface/store.py`
- **Schema Design**:
  - `artifacts` (id, stage_id, key, title, kind, status, locked, created_at, updated_at)
  - `versions` (id, artifact_id, version_num, content, metadata, created_at)
  - `dependencies` (parent_artifact_id, child_artifact_id)
  - `events` (id, type, artifact_id, version_id, payload, created_at, processed_at)
  - `jobs` (id, artifact_id, provider, status, params, result, claim_owner, claim_expires_at, created_at, updated_at)
  - `inbox` (id, sender, content, read_at, created_at)
- **Store API Operations**:
  - Artifact & immutable version creation/retrieval
  - DAG propagation: Upstream version changes mark downstream artifacts as `stale`
  - Event recording & lease-based job claim handling
  - Store rehydration queries for worker recovery

---

### Module 2: Stage Configs
**Files**: `agent_surface/stages/movie.json`
- **Config Definition**:
  - Define stages, artifact definitions (Script, Shotlist/Prompts, Image Frames, Video Clips), allowed actions (edit, revise, approve, regenerate, select, cancel).
  - DAG mapping (Script -> Shotlist -> Images -> Videos).
  - Define input JSON schemas and action targets.

---

### Module 3: Supervisor & WorkerTransport
**Files**: `agent_surface/supervisor.py`, `agent_surface/transports/native_stream.py`, `agent_surface/transports/resume.py`
- **Worker Supervisor**:
  - Manages worker process lifecycle, rehydrates context from store events, handles turn execution.
- **Worker Transport Abstraction**:
  - `NativeStreamTransport`: Persistent stdin/stdout streaming transport for interactive CLI runners.
  - `ResumeTransport`: One-shot/resume-per-event fallback transport.

---

### Module 4: Board UI
**Files**: `agent_surface/board.py`
- **Gradio Interactive Interface**:
  - Render artifact list, DAG status badges (`current`, `stale`, `generating`, `approved`).
  - Renderers for Text, Image, and Video content.
  - Action handlers: Direct text editing, shot approvals, image/video revision prompts, generation trigger, job cancellation, inbox messaging.
  - Auto-refresh mechanism subscribing to store events.

---

### Module 5: Async Jobs & Poller
**Files**: `agent_surface/poller.py`, `agent_surface/providers/`
- **Job Poller**:
  - Scans `jobs` table for pending/in-progress generation requests.
  - Acquires lease locks, dispatches to provider adapters, polls for completion, writes back results, and updates artifact versions.
- **Provider Adapters**:
  - Mock/real Image Provider (`providers/image.py`).
  - Mock/real Video Provider (`providers/video.py`).

---

### Module 6: Skill Packaging & CLI
**Files**: `SKILL.md`, `agent_surface/cli.py`, `agent_surface/surface/`
- **CLI Commands**:
  - `agent-surface init --stage movie`
  - `agent-surface serve` (launches store, supervisor, poller, and Gradio board)
  - `agent-surface status`
- **OpenClaw Skill Packaging**:
  - Expose surface tools/commands for agent interaction with the board runtime.

---

### Integration & Verification
**Files**: `tests/test_movie_walkthrough.py`
- **Automated Verification Sequence (Section 64)**:
  1. Edit script -> mark shotlist stale.
  2. Approve updated shots -> trigger async image generation.
  3. Revise image prompt -> regenerate single image.
  4. Generate video clips -> test job cancellation on a clip.
  5. Simulate worker process death and verify seamless rehydration/recovery from SQLite store.
