# Agent Surface Board Skill

Portable skill for long-lived, versioned, agent-driven workspaces. The board is a persistent project UI backed by SQLite; the agent is a replaceable worker. Use when you need versioned artifacts, async generation, non-linear review, approval workflows, or survival across restarts.

**Do NOT use the board for casual chat, one-off answers, or immediate-feedback loops.** Keep trivial interactions in chat. Open the board only when the work materially benefits from persistent state, versions, or review.

---

## When to Open the Board

Open a board when the task involves **3+ related decisions, long review cycles, media, diffs, non-linear edits, or dependencies between stages**. Examples:
- Document review with comments and approval gates
- Image/video/content pipelines with versions
- Feature review and multi-stage sign-off
- Data cleaning or enrichment pipelines where versions matter
- Planning documents with options and constraints

Keep in chat when:
- Immediate feedback is needed in one turn
- The work is a single artifact with no review loop
- No versions or comparison matter
- Only one person decides

---

## Project Lifecycle

### Initialize a Project

```bash
surface init --stage movie [--db <path>]
```

This creates a `.surface-board/` directory with:
- `state.sqlite3` — persistent SQLite store (WAL mode)
- Project metadata (stage config, IDs, etc.)

**Stages available:** `movie`, and others as you define them.

**Stage configs** are JSON files in `surface/stages/`. Each defines stages, artifact types, allowed actions, dependencies, generation defaults, approval rules, and completion criteria. See `movie.json` for the reference model.

### Start the Board

```bash
surface serve [--stage movie] [--db <path>]
```

This launches:
1. **Store** — SQLite artifact/version/event store (already exists from `init`)
2. **Supervisor** — claims events and drives a worker
3. **Poller** — polls generation jobs and emits completions
4. **Board** — Gradio UI (if installed; logs if unavailable)

By default the supervisor+poller loop runs **unbounded**, until interrupted with Ctrl-C (SIGINT triggers a clean shutdown: worker stopped, transport closed, runtime files removed). For scripted/testable runs, bound it with `--max-iterations N` (`--supervisor-iterations` is a legacy alias). The board (if gradio is installed) launches on loopback (`127.0.0.1` by default; override with `--host`/`--port`) with an auto-generated bearer token written to `.surface-board/run/token`; pass `--no-board` to run supervisor+poller only.

### Check Project Status

```bash
surface status
```

Reports: DB exists, artifact count, counts by stage.

---

## Using the Store: No Direct Database Access

**Never write to the SQLite database directly.** Always use store CLI tools; they enforce invariants (versioning, idempotency, DAG cycles, lease expiry).

### Create/Manage Artifacts

```bash
# Get an artifact
surface store get <artifact_id>

# List artifacts (optionally filtered)
surface store list [--stage script] [--status approved]

# Set artifact status
surface store set-status <artifact_id> <status>
```

**Artifact Statuses:** `draft`, `generating`, `review`, `approved`, `stale`, `failed`, `cancelled`.

### Create Versions (Never Overwrites)

Each version is immutable. Selecting a new version marks downstream artifacts `stale` but does not delete old versions.

```bash
# From stdin (JSON)
echo '{"artifact_id":"script_1","content":"Scene 1: INT. OFFICE","content_type":"text/plain","note":"first draft"}' | \
  surface store put-version

# From file
surface store put-version --artifact-id script_1 --file version.json

# Or with JSON schema for structured input (if defined in stage config form_schema)
```

**Idempotency:** Include `"source_event_id":"evt_123"` in the version payload. If the same event is redelivered (after a crash), the store detects it and acks safely.

### Select a Version

```bash
surface store select-version <artifact_id> <version_id>
```

Selecting a new version:
- Updates the artifact's `selected_version_id`
- Traverses the dependency DAG
- Marks all downstream descendants `stale` (recursively)
- **Does not delete old versions**

This allows recovery: if a downstream fix turns out wrong, you can select an earlier version of an upstream dependency and retry.

### Manage Dependencies

```bash
# Add a dependency: if script_1 changes, mark all scripts' descendants stale
surface store add-dependency script_1 shots_1

# View the graph for an artifact
surface store graph shots_1
```

Dependencies are edges in a DAG. Cycles are rejected. Stale propagation is automatic and transactional.

---

## Async Generation: Create Jobs, Don't Poll

Never wait for slow generation inside a turn. Instead:

1. **Create a job** (marks artifact `generating`)
2. **End your turn immediately**
3. **Let the poller check completion asynchronously**
4. **Handle `job_done` events on next turn**

### Submit a Job

```bash
# Image generation request from stdin
echo '{"prompt":"a sunset","model":"flux"}' | \
  surface store job-start <artifact_id> image_default image

# Video generation from file
surface store job-start <artifact_id> video_default video --file video_request.json
```

This enqueues the job (`status: queued`). The poller will:
1. Submit to the provider
2. Poll for completion
3. On success: create version, emit `job_done`, update artifact
4. On failure: emit `job_failed`

### Handle Job Completion

Jobs complete asynchronously. The supervisor delivers `job_done` and `job_failed` events like any other event.

```bash
# Claim next event (may be job_done)
surface inbox next --wait 60

# Process it (e.g., check artifact, update status, launch next stage)
...

# Acknowledge only after durable effects are committed
surface inbox ack <event_id>
```

### Cancel a Job

```bash
surface store job-cancel <job_id>
```

Requests cancellation from the provider. If the job has already completed, it remains marked `cancel_requested` (late results cannot become selected).

---

## Event Inbox: At-Least-Once Delivery

The inbox is a durable queue of user actions and system events. Always process events atomically: read → apply durable effects → ack.

### Claim an Event

```bash
# Non-blocking (try once)
surface inbox next --wait 0

# Poll for up to 60 seconds
surface inbox next --wait 60

# Custom worker ID
surface inbox next --wait 60 --worker-id my_worker_1
```

Returns the next pending event with `status: processing` and a lease (`lease_until`). If you crash, the lease expires and the event retries.

### Event Types

**User Events (from board actions):**
- `create`, `revise`, `edit` — user input; worker interprets and commits versions
- `approve`, `reopen` — status changes
- `select_version` — user picked a version
- `regenerate` — user triggered generation
- `cancel` — user cancelled a job
- `message` — free-form text (treat like chat)

**System Events:**
- `job_done` — async generation completed; new version available
- `job_failed` — async generation failed; artifact goes `failed` or `stale`
- `stale` — an upstream dependency changed; this artifact marked `stale`
- `worker_recovered` — (future) worker was recycled

### Acknowledge After Durable Effects

```bash
# After all writes (versions, status, jobs) are persisted:
surface inbox ack <event_id>
```

Only ack once the side effects that answer the event are durable (in SQLite). On ack, the lease is released.

### Fail an Event

```bash
surface inbox fail <event_id> --error "Could not generate image: out of credits"
```

Marks the event `failed` and logs the error. The supervisor may retry or hand to a human; deterministic retry is not automatic in Phase 0.

---

## Event Loop: Rehydration and Recovery

The supervisor can restart and rehydrate from the store. It does not replay chat history.

### Rehydration Summary

When the supervisor starts (or recycles the worker), it receives a compact summary from the store:
- Total artifact count and stage distribution
- Stage config (what stages exist, what's allowed)
- A few recent unacked events (to resume work)
- Key constraints (budgets, locks, approval rules)

The worker should query the store for full artifact details as needed, not expect the summary to be comprehensive.

### Resuming After a Crash

1. **Worker dies before acking:** The event lease expires. Supervisor starts, claims the event again, and retries. If the previous worker already committed side effects (e.g., a version), the `source_event_id` prevents duplicates.

2. **Worker dies after acking:** The event is already `acked`. Supervisor continues to the next pending event.

3. **Supervisor dies:** On restart, it claims pending events and resumes. Board/store remain intact and reflect the last committed state.

To manually inspect or reset:
```bash
surface store list --status processing  # see abandoned claims
surface status  # check artifact counts
```

---

## Project Summary and Inspection

```bash
surface project summary
```

Returns counts by stage and status. Use to understand project progress at a glance.

---

## Locked Artifacts and Budget Constraints

### Lock an Artifact (Phase 1+)

A **user lock** tells automation: "Don't regenerate this without asking."

```bash
# Lock an artifact (CLI support Phase 1)
surface store set-lock <artifact_id> true
```

If an upstream dependency changes, a locked downstream artifact goes `stale+locked` and is flagged for user review.

### Budget Enforcement

Stage configs may define budgets (e.g., `"stage_usd": 20`). If generation cost would exceed the threshold, the board prompts for confirmation before proceeding. The **supervisor enforces this deterministically**, not the model.

---

## Board UI Best Practices

The board is deterministic, not generated. It renders from stage config + artifact store.

### Structure

The board shows:
- **Stage tabs/groups:** one per configured stage
- **Artifact cards:** one per artifact, with versions, status, allowed actions
- **Action buttons:** constrained by stage config and artifact status
- **Version history:** all immutable versions, not auto-deleted
- **Stale badges:** warning that upstream changed

### Safe Actions (Deterministic, No Worker Needed)

- Select a version (auto-propagates stale)
- Approve an artifact (status change)
- Lock/unlock an artifact

### Actions Requiring Worker (Create Events)

- Edit text (new version)
- Revise (new version with note)
- Regenerate (create job)
- Message (free-form)
- Cancel (job cancellation request)

---

## Preferred Patterns

### Pattern 1: Edit → Approve Loop

1. User edits script on board
2. Board emits `edit` event
3. Worker reads event, writes new version, sets `review`, acks
4. User sees new version, approves on board
5. Board emits `approve` event
6. Worker sees approval, sets `approved`, acks
7. Downstream artifacts marked `stale` (implicit)

### Pattern 2: Async Image Generation

1. User clicks "Generate" on keyframes stage
2. Board emits `regenerate` event
3. Worker reads event, creates job, sets artifact `generating`, acks (does NOT wait)
4. Poller polls generation provider in background
5. On completion, poller creates version, emits `job_done` event
6. Board refreshes; user sees new image
7. User approves or requests revisions

### Pattern 3: Stale Propagation

1. Script gets new version
2. Store marks all downstream artifacts (shots, keyframes, clips) `stale`
3. Locked descendants are flagged but not auto-regenerated
4. User decides: regenerate downstream, or revert script, or accept stale state

---

## Sub-Agent Ownership and Handoff

A board may be owned by a sub-agent directly (no parent mediation per UI action).

```python
# Parent agent initializes and starts supervisor:
sub_agent = spawn_sub_agent("storyboard_supervisor")
sub_agent.start_board(project_id="movie_01", stage="movie")

# Sub-agent owns the board loop, processes all events, polls jobs
# Parent receives only milestones (e.g., "all keyframes approved")
```

When delegating to a sub-agent:
1. Hand over the `.surface-board/state.sqlite3` path
2. Include stage config or reference
3. Sub-agent resumes from store state, not chat history
4. On completion, sub-agent returns milestones/final artifact IDs to parent

---

## Troubleshooting

### No events claimed

- Check if any events are `pending`: `surface store list --status pending` (not a real command yet, but introspect the DB)
- If events are `processing`, they may be leased; wait for lease to expire or manually recover
- Ensure at least one artifact or user action has created an event

### Artifact marked stale but shouldn't be

- Check dependencies: `surface store graph <artifact_id>`
- Upstream selection changes always trigger stale propagation (by design)
- To continue work: select a new version downstream, or revert the upstream change

### Version not appearing on board

- Ensure `selected_version_id` is set: `surface store get <artifact_id>` and check the field
- If you created a version with `select=false`, it won't be the selected version
- After acking the event, board should refresh

### Job stuck in `queued` or `running`

- Check poller: `surface serve --supervisor-iterations 10` to run more loops
- Ensure provider is registered: e.g., `image_default` or `video_default` in stage config
- Mock providers auto-complete; real providers need API keys/network

---

## Limitations and Current Scope

**Implemented (Phase 0 + Phase 1 + Phase 2 + partial Phase 3):**
- SQLite store, artifact versioning, DAG + stale propagation
- Movie pipeline plus questionnaire/plan_review/document_review/diff_review/
  generic_media_pipeline presets, and a custom stage-config authoring guide
  (`docs/stage-config-guide.md`)
- Gradio board UI (if available; graceful fallback if not), with select-
  version conflict surfacing and live-refresh (a 2-second poll plus
  immediate post-action updates -- new versions/status changes appear
  without restarting `surface serve`, verified via live browser testing)
- Event inbox with leases, bounded dispatch retry, session recycling and a
  per-artifact worker pool (`surface serve --pool-size N`,
  `--recycle-after-events N`) -- default remains one serial worker session
- Mock image and video providers with cost tracking (estimate + actual);
  provider webhook payload ingestion (`surface.webhook`) exists but is not
  wired to an HTTP endpoint in this codebase
- Recovery CLI (`surface stop`, `surface recover`, `surface interrupt`)
- `surface render`/`answer`/`wait` bounded convenience mode, backed by a
  local JSON interaction store under `.surface-board/interactions/`
  (not the SQLite store)
- A native-subprocess reference worker (`python -m surface.worker`) and an
  ACP transport adapter (`surface/transports/acp.py`) -- the ACP adapter is
  written against an assumed duck-typed client interface, has NOT been
  verified against a real ACP SDK, and is not wired into `serve()`; treat
  it as scaffolding for a future integration, not a working ACP path
- `surface tui` (`surface/tui.py`) -- a stdlib-only, non-Gradio terminal
  renderer. Reuses `board.py`'s gradio-independent display/action logic. This
  is the one renderer actually run and verified end-to-end in this dev
  environment (the Gradio board has not been, since gradio isn't installed
  here)
- `surface/notify.py` -- `LogNotifier`/`WebhookNotifier` notification
  adapters and a pure `detect_notifications()` diff function; not yet wired
  to fire automatically inside `serve()`'s loop

**Still NOT included:**
- Multi-user collaboration
- Hosted/shared board service
- MCP Apps / A2UI renderers
- A verified ACP integration (see above)
- Budget confirmation UI (deterministic backend only)
- Robust error recovery (basic retry on lease expiry)

---

## CLI Reference

All commands output JSON to stdout; errors to stderr.

### Store
- `surface store get <artifact_id>` — fetch artifact
- `surface store list [--stage S] [--status S]` — list with filters
- `surface store put-version [--artifact-id ID] [--file F]` — create version (stdin or file)
- `surface store select-version <artifact_id> <version_id>` — select & propagate stale
- `surface store set-status <artifact_id> <status>` — update status
- `surface store add-dependency <up> <down>` — add edge
- `surface store graph <artifact_id>` — view dependencies

### Jobs
- `surface store job-start <artifact_id> <provider> <kind> [--file F]` — enqueue job
- `surface store job-finish <job_id> [--file F]` — mark succeeded with result
- `surface store job-cancel <job_id>` — cancel job

### Inbox
- `surface inbox next [--wait N]` — claim next event (poll up to N seconds)
- `surface inbox ack <event_id>` — acknowledge event
- `surface inbox fail <event_id> [--error MSG]` — fail event

### Project
- `surface project summary` — counts by stage & status
- `surface init [--stage S] [--db PATH]` — initialize project
- `surface status` — DB existence & artifact count
- `surface serve [--stage S] [--db PATH] [--supervisor-iterations N]` — start runtime

---

## Example Workflows

### Workflow 1: Simple Document Review

```bash
# Init
surface init --stage document_review --db ./review.sqlite3

# Start board (in background or tmux)
surface serve --db ./review.sqlite3 &

# Create a document artifact (via worker or CLI)
# In a separate terminal, worker claims and processes events:
while true; do
  event=$(surface inbox next --wait 30)
  if [ "$event" != "null" ]; then
    # Process (e.g., revise text, create version)
    surface store put-version --artifact-id doc_1 --file updated.json
    surface inbox ack $event_id
  fi
done
```

### Workflow 2: Image Pipeline with Job Polling

```bash
# Parent agent delegates to sub-agent
surface init --stage movie --db ./film.sqlite3
surface serve --db ./film.sqlite3 --supervisor-iterations 5

# Worker:
# 1. Claims revise event on keyframes
# 2. Creates job, acks immediately
# 3. Next iteration: poller completes job, emits job_done
# 4. Next claim: worker reads job_done, creates version, acks
# 5. Board refreshes, user reviews

# Repeat per stage
```

### Workflow 3: Stale Propagation and Recovery

```bash
# Script is approved, shots are approved
surface store set-status script_1 approved
surface store set-status shots_1 approved

# User edits script (worker creates new version)
# Store marks shots, keyframes, clips stale (recursively)

# User can now:
# 1. Regenerate downstream (clean update)
# 2. Revert script to prior version (undo change)
# 3. Accept stale and lock downstream (manual review later)
```

---

## See Also

- `docs/SPEC.md` — full specification and architectural details
- `ongoing/phase-0-build/PLAN.md` — build modules and testing strategy
- `surface/stages/movie.json` — reference stage config

---

**Version:** 1.0 (Phase 0)  
**Status:** Stable for Phase 0; ready for agent use.
