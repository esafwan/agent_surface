# Agent Surface Board Runtime
## Portable specification for live, versioned, agent-driven workspaces
**Status:** Proposed v1 build specification  
**Revision:** 2026-09-20  
**Reference workflow:** movie pipeline — script → shots → keyframes → clips → assembly  
**Normative language:** MUST, SHOULD, MAY indicate requirement strength.
## 0. Executive Summary
Agent Surface Board is a portable skill/runtime that lets an ordinary coding or cloud agent expose a persistent browser workspace without requiring a separately engineered application backend.
The core model is `Board UI → durable inbox/store → supervisor → live agent worker`, with a separate async job path for slow generation.
The board is long-lived; the store is the source of truth; the agent is a replaceable worker; generation jobs are asynchronous; user actions may occur in any order.
The common path MUST NOT require the LLM to generate frontend code.
Version 1 uses Python, SQLite in WAL mode, Gradio as the first renderer, JSON stage configs, JSON Schema for forms, and a worker transport abstraction.
ACP is the preferred standards-based worker transport where available; native persistent CLI streaming is valid for an initial implementation; resume-per-event is the fallback.
Blocking question/answer remains only as a bounded convenience wrapper: `render → wait(max) → answer|pending`.
One-sentence definition: **A portable skill that gives any agent a live, versioned review board for multi-stage work, with the agent running as a worker behind it and the store as the source of truth.**
```text
Board UI ──user actions──▶ SQLite Store/Inbox ──▶ Supervisor ──▶ Agent Worker
   ▲                              ▲                                  │
   └──────── Artifact Store ◀─────┴──── Job Poller ◀──── Generation Tools
```
## 1. Problem
Coding agents already have reasoning, shell access, files, tools, code execution, model access, and often sub-agents, but user interaction is still mostly terminal/chat.
This fails for workflows with many related decisions, visual review, versions, approvals, slow jobs, non-linear edits, dependencies, and work that must survive an agent restart.
A blocking Q&A runtime cannot be the foundation because harness tool calls may time out, users review non-linearly, generation may take minutes, and a worker may disappear while the project must remain intact.
The runtime therefore MUST model persistent work rather than persistent conversation.
## 2. Goals
The system MUST: expose a browser board from a normal prompt/skill; avoid per-run frontend generation; support text/image/video/diff/form/file artifacts; retain immutable versions; support arbitrary review order; run slow work asynchronously; propagate staleness through dependencies; survive worker crash/restart; support direct sub-agent ownership; work locally and remotely; support more than one agent CLI; minimize UI-related model tokens.
The system SHOULD: allow the board to outlive worker sessions; reuse shipped stage presets; keep project state inspectable without model context; support future native rendering via MCP Apps/A2UI or host-native systems.
## 3. Non-Goals
V1 does NOT require multi-user collaborative editing, a SaaS hosting platform, a new agent-to-agent protocol, a new component protocol, arbitrary browser-side code generation, Redis, distributed queues, Kubernetes, or ACP support from every harness.
Simple interactions SHOULD remain in chat.
The board SHOULD open only when it materially improves the workflow.
## 4. Core Design Principles
1. **Store is truth.** Project state MUST live in the store; agent context is disposable cache.
2. **Agent is worker.** The worker interprets events and writes through store tools; it is not the database.
3. **UI is deterministic.** Common workflows use a shipped board renderer plus stage config, not generated React/HTML.
4. **Reuse before build.** Use JSON Schema, ACP, Gradio, MCP Apps/A2UI compatibility, and existing provider SDKs where appropriate.
5. **Async by default.** Slow generation MUST be jobs; worker turns MUST NOT wait for completion.
6. **Non-linear interaction.** Many artifacts MAY be open for review simultaneously; serialization is per artifact, not global.
7. **Versions are durable.** Revisions create versions; selected versions may change; alternatives remain.
8. **Stale, never destroy.** Upstream changes mark dependents stale; versions are not auto-deleted.
9. **Renderer is replaceable.** Gradio-specific concepts MUST NOT leak into store or stage semantics.
10. **Transport is replaceable.** ACP/native-stream/resume-per-event implement one internal transport contract.
11. **Deterministic rules beat LLM judgment** for budgets, locks, staleness, idempotency, event delivery, and persistence.
## 5. Terminology
**Project/Session:** one persistent board workspace and its store.  
**Board:** browser UI representing artifacts and state.  
**Stage:** configured step/category such as script, shots, keyframes, clips.  
**Artifact:** logical work item independent of its versions.  
**Version:** immutable representation of an artifact at a point in time.  
**Dependency:** directed edge saying one artifact depends on another.  
**Inbox Event:** durable request for worker processing.  
**Supervisor:** process that claims events and drives workers.  
**Worker:** agent session interpreting events.  
**Owner:** logical worker/sub-agent assigned to a board.  
**Job:** slow async external/local operation.  
**Poller:** checks job completion or handles provider callbacks.  
**Renderer:** UI implementation; Gradio is v1.  
**Stage Config:** JSON defining stages, actions, dependencies, generation defaults, budgets, and completion rules.  
**Transport:** supervisor-to-agent communication mechanism.
## 6. Reference Architecture
```text
User/Browser
    │
    ▼
Gradio Board v1
    │ read state / emit actions
    ▼
SQLite Project Store
├─ artifacts
├─ versions
├─ artifact_dependencies
├─ events
├─ jobs
├─ artifact_locks
├─ sessions
└─ kv_state
    │                         │
    ▼                         ▼
Supervisor               Job Poller
    │                         │
WorkerTransport          Generation Providers
├─ ACP                   ├─ image
├─ native stream         ├─ video
└─ resume-per-event      └─ local tools
    │
    ▼
Parent agent or directly owned sub-agent
```
No renderer code MAY call a model directly.
No agent MAY mutate SQLite directly.
All agent writes MUST pass through store tools.
All user actions requiring intelligence MUST become inbox events.
## 7. Recommended v1 Stack
Required: Python 3.11+, SQLite, Gradio, one agent CLI, one WorkerTransport implementation, JSON stage configs.
Optional: MCP exposure of store tools, provider SDKs, ffmpeg, tunnel client, notifications, second agent transport.
Not required: Node.js, React, Tailwind, Bootstrap, FastAPI, Redis, WebSockets, Docker, hosted DB.
Implementations MAY add those, but none may become core protocol assumptions.
## 8. Skill Packaging
```text
surface-board/
  SKILL.md
  surface/
    store.py
    schema.py
    board.py
    supervisor.py
    poller.py
    worker.py
    security.py
    notify.py
    cli.py
  transports/
    acp.py
    native_stream.py
    resume.py
  providers/
    base.py
    image_example.py
    video_example.py
  stages/
    movie.json
    questionnaire.json
    plan_review.json
    document_review.json
    diff_review.json
  tests/
```
`SKILL.md` MUST let an unfamiliar coding agent decide when to open a board, start/reconnect, choose/create stage config, consume events, use store tools, create versions/jobs, ack events, avoid blocking on generation, and safely close/hand off.
## 9. Runtime Directory
```text
.surface-board/
  project.json
  state.sqlite3
  media/
  logs/
  run/
    board.pid
    supervisor.pid
    poller.pid
    token
    port
```
The runtime MAY live inside the project for persistent work or in temp storage for disposable work.
Secrets MUST NOT be embedded in stage configs.
Runtime token files SHOULD use restrictive permissions.
## 10. SQLite Store
SQLite MUST use WAL mode and a schema version.
Minimum logical tables: `runtime`, `sessions`, `artifacts`, `versions`, `artifact_dependencies`, `events`, `jobs`, `artifact_locks`, `kv_state`.
### 10.1 Artifact
`{"id":"shot_002","stage":"shots","title":"Scene 2 / Shot 1","status":"review","selected_version_id":"ver_88","locked":false,"meta":{"scene":2,"order":1}}`
Required fields: `id`, `stage`, `title`, `status`, nullable `selected_version_id`, `locked`, timestamps, `meta_json`.
### 10.2 Version
`{"id":"ver_88","artifact_id":"shot_002","n":3,"content_ref":"media/shot_002_v3.png","content_type":"image/png","prompt":"medium close-up...","params":{"seed":8124},"created_by":"worker","note":"warmer lighting","created_at":"2026-09-20T12:00:00Z"}`
Versions MUST be immutable except non-semantic storage metadata.
Selecting a version MUST NOT delete alternatives.
Worker-generated writes SHOULD record `source_event_id`.
### 10.3 Dependency
`{"upstream_artifact_id":"scene_002","downstream_artifact_id":"shot_002","kind":"content"}`
Dependencies MUST be graph edges, not only `parent_id`.
`parent_id` MAY exist for display grouping.
Stale propagation MUST use dependency edges.
Cycles MUST be rejected.
### 10.4 Event
Minimum fields: `id`, `project_id`, nullable `artifact_id`, `type`, `payload_json`, `status`, nullable `claimed_by`, nullable `claimed_at`, nullable `lease_until`, `attempt_count`, `created_at`, nullable `acked_at`, nullable `error`, nullable `dedupe_key`.
Lifecycle: `pending → processing → acked|failed`; expired processing leases return to `pending`.
### 10.5 Job
Minimum fields: `id`, `artifact_id`, `provider`, `provider_job_id`, `kind`, `status`, `request_json`, `result_json`, `cost_estimate`, `cost_actual`, `cancel_requested`, timestamps.
Lifecycle: `queued → running → succeeded|failed|cancelled`.
Late completion of a cancelled job MUST NOT automatically become selected.
### 10.6 Artifact processing lease
Minimum fields: `artifact_id`, `owner_worker_id`, `lease_until`, `event_id`.
Processing leases MUST expire and be recoverable after supervisor/worker death.
## 11. Artifact Status
Core states: `draft`, `generating`, `review`, `approved`, `stale`, `failed`, `cancelled`.
Typical path: `draft → generating → review → approved`.
`approved` MAY be reopened.
`stale` means at least one selected upstream dependency changed after the current downstream version was produced.
A stale artifact remains visible and retains versions; it MUST NOT silently be treated as current.
## 12. Dependency and Stale Propagation
Whenever artifact A gets a newly selected version, the store MUST: find direct downstream edges; mark affected dependents stale; recursively traverse descendants; emit system stale event(s); retain all versions; avoid auto-regenerating locked artifacts.
Example: `scene_2 → shots_2_* → keyframes_2_* → clips_2_*`.
The operation SHOULD be transactional.
Dependency graph behavior MUST support multiple upstream dependencies.
Locked descendants are flagged stale but not regenerated automatically.
## 13. User Event Types
Required v1 user→worker events: `create`, `revise`, `regenerate`, `select_version`, `edit`, `approve`, `reopen`, `cancel`, `lock`, `unlock`, `bulk_regenerate`, `message`.
Revision example:
`{"id":"evt_143","artifact_id":"img_003","type":"revise","payload":{"note":"warmer light and less contrast"}}`
Edit example:
`{"id":"evt_144","artifact_id":"scene_002","type":"edit","payload":{"content":"Revised scene text..."}}`
Free-form escape hatch:
`{"id":"evt_145","artifact_id":null,"type":"message","payload":{"text":"Keep the next three shots visually consistent with image 3."}}`
## 14. System Event Types
Required system→worker events: `job_done`, `job_failed`, `stale`, `worker_recovered`.
Example:
`{"type":"job_done","payload":{"job_id":"job_91","artifact_id":"clip_002","content_ref":"media/clip_002_v2.mp4"}}`
## 15. Event Delivery
Delivery MUST be at-least-once.
Ordering MUST be correct per artifact.
Global project ordering SHOULD be preserved where practical but MUST NOT be required for correctness.
Every event MUST have a stable ID.
Workers MUST be idempotent.
Claiming MUST be atomic: choose eligible pending event; ensure artifact is not actively leased; set `processing`; set worker/lease; increment attempt count; return event.
If the worker dies, lease expiry returns the event to pending.
Ack MUST happen only after all durable effects succeed.
## 16. Idempotency
Every worker-driven effect SHOULD record `source_event_id`.
Before creating a new semantic effect, tools SHOULD detect whether the same event already produced it.
Example:
`{"source_event_id":"evt_143","artifact_id":"img_003","operation":"start_generation"}`
This prevents duplicate jobs/versions when an event is redelivered after a crash.
## 17. Store/Inbox Tools
The agent MUST use tools rather than direct database writes.
Tools MAY be CLI, MCP, Python API, or multiple forms.
JSON-only results SHOULD go to stdout; diagnostics SHOULD go to stderr.
Required commands:
```text
surface store get <artifact_id>
surface store list [--stage <stage>] [--status <status>]
surface store put-version
surface store select-version
surface store set-status
surface store add-dependency
surface store graph <artifact_id>
surface store job-start
surface store job-cancel
surface store job-finish
surface inbox next --wait <seconds>
surface inbox ack <event_id>
surface inbox fail <event_id>
surface project summary
`Example \`put-version\` input:`
{"artifact_id":"scene_002","content":"INT. KITCHEN — NIGHT...","content_type":"text/plain","created_by":"worker","note":"shorter exchange","source_event_id":"evt_144"}
`Example output:`
{"ok":true,"version_id":"ver_109","version":4,"stale_descendants":["shot_002_01","shot_002_02","img_002_01","clip_002_01"]}
```
## 18. WorkerTransport
Supervisor MUST depend on one internal interface:
```text
start(project_context) -> worker_session
send_event(worker_session, event_context) -> turn_result
interrupt(worker_session) -> result
is_alive(worker_session) -> bool
close(worker_session)
resume(session_ref, project_context) -> worker_session
```
Board/store MUST NOT know which transport is active.
Event context SHOULD include project ID/config, event, current artifact, directly relevant upstream/downstream IDs, and a bounded project summary.
Worker SHOULD query store tools for more state rather than receiving the entire project.
## 19. ACP Transport
ACP SHOULD be the preferred standards-based live transport when a compatible agent/adapter exists.
Supervisor acts as ACP client; coding agent acts as ACP agent.
Needed capabilities are session create/resume, prompt/event delivery, optional streaming progress, turn completion, and cancellation.
ACP-specific details MUST remain inside `transports/acp.py`.
No artifact/store schema may depend on ACP message shapes.
## 20. Native Stream Transport
For initial implementation, a harness MAY expose a persistent subprocess with JSON/JSONL over stdin/stdout.
Conceptual envelope:
`{"type":"event","event_id":"evt_143","payload":{"artifact_id":"img_003","action":"revise"}}`
This MAY be the Phase 0 transport when it is the shortest path to proving the architecture.
## 21. Resume-per-Event Fallback
Fallback concept: `agent --resume <session_id> "<serialized event>"`.
If resume is unavailable, supervisor MAY start a fresh session using a rehydration summary.
This mode is slower but preserves portability.
The rest of the system MUST behave identically regardless of transport.
## 22. Supervisor
Supervisor owns worker lifecycle and inbox dispatch.
Loop:
```text
1 ensure worker exists
2 claim next eligible event
3 acquire artifact processing lease when artifact-scoped
4 construct bounded context
5 send event to worker
6 worker uses store tools
7 validate durable outcome
8 ack event
9 release lease
10 continue
```
Supervisor MUST be restartable and SHOULD heartbeat.
It MUST detect worker exit, transport failure, expired event lease, turn timeout, explicit cancellation, and recycling threshold.
Board does not need to be open for supervisor to operate.
## 23. Rehydration
Worker context is disposable.
A new/recycled worker MUST receive a compact summary derived from store, not replayed chat history.
Example:
`{"project":"movie_01","goal":"Produce approved 30-second product film","stage_counts":{"script":{"approved":1},"shots":{"approved":6,"stale":2},"keyframes":{"review":3,"generating":2},"clips":{"approved":1,"generating":2}},"important_constraints":["16:9","warm practical lighting","same protagonist across shots"],"recent_events":["evt_139","evt_140","evt_142"]}`
Large projects MUST NOT be dumped wholesale into context.
Worker MUST query specific artifacts as needed.
Recycle after N events, token/context threshold, instability, or explicit operator request.
## 24. Direct Sub-Agent Ownership
A board MAY be owned by a sub-agent directly.
```text
Parent Agent
  └─ Storyboard Supervisor sub-agent
       └─ owns board worker session
```
Browser MUST NOT know it is a sub-agent.
Ownership example:
`{"owner_type":"agent","owner_role":"storyboard_supervisor","transport":"acp","session_ref":"session_x"}`
Parent MAY receive only milestones/final completion.
Ownership MAY transfer to another worker without changing board URL/store.
## 25. Worker Pool and Concurrency
V1 MAY use one worker serially.
Phase 1 MAY use a pool.
With multiple workers: each has its own agent session; same-artifact events MUST serialize; unrelated artifacts MAY run concurrently; artifact leases prevent conflicting turns; graph-wide changes use transactions; version creation must be conflict-safe.
A worker MAY launch multiple generation jobs from one event if allowed by stage config.
## 26. Jobs
Slow generation MUST become jobs.
Worker flow: receive event → construct request → store job → set artifact `generating` → end turn.
Worker MUST NOT poll until completion inside the same turn.
Example:
`{"id":"job_91","artifact_id":"clip_002","provider":"video_provider","provider_job_id":"vp_82918","kind":"video","status":"running","request":{"prompt":"...","duration":5}}`
## 27. Poller / Provider Adapter
Poller is independent of agent worker.
It MAY poll APIs, receive webhooks, watch subprocesses, or watch files.
On success: confirm not cancelled; persist provider result; emit `job_done`; apply only deterministic completion policy; otherwise let worker interpret.
On failure: emit `job_failed`.
Provider contract:
```text
submit(request) -> provider_job_id
status(provider_job_id) -> state
cancel(provider_job_id) -> result
collect(provider_job_id) -> content_ref + metadata
```
## 28. Cancellation
Cancellation MAY target worker turn, pending event, generation job, or regeneration batch.
Worker-turn cancel SHOULD call transport interrupt; durable writes already committed remain.
Generation cancel MUST mark `cancel_requested`, call provider cancel if supported, set cancelled state, and prevent late success from becoming selected.
Late results MAY be retained as quarantined metadata but MUST NOT override user state.
## 29. Stage Config
One board template MUST be driven by stage config.
Config defines ordered stages, artifact kinds, dependency defaults, display grouping, actions, approval rules, generation defaults, concurrency, budgets, optional form schema, and completion rules.
Config MUST NOT contain Gradio-specific widget definitions.
## 30. `movie.json` Example
`{"schema_version":"1","id":"movie","title":"Movie Pipeline","stages":[{"id":"script","artifact_type":"text","allowed_actions":["edit","revise","approve","reopen"],"approval_required":true},{"id":"shots","artifact_type":"text","depends_on":["script"],"allowed_actions":["edit","revise","approve","reopen"],"approval_required":true},{"id":"keyframes","artifact_type":"image","depends_on":["shots"],"allowed_actions":["regenerate","revise","select_version","approve","cancel"],"generation":{"provider":"image_default","max_parallel":4}},{"id":"clips","artifact_type":"video","depends_on":["keyframes"],"allowed_actions":["regenerate","revise","select_version","approve","cancel"],"generation":{"provider":"video_default","max_parallel":2}},{"id":"assembly","artifact_type":"video","depends_on":["clips"],"allowed_actions":["regenerate","approve","reopen"]}],"completion":{"require_approved_stages":["script","shots","keyframes","clips","assembly"]}}`
Stage configs MUST be validated before board startup.
## 31. JSON Schema Forms
Structured user input MUST use JSON Schema rather than a custom form DSL.
Example:
`{"type":"object","required":["aspect_ratio","duration"],"properties":{"aspect_ratio":{"type":"string","enum":["16:9","9:16","1:1"]},"duration":{"type":"integer","minimum":5,"maximum":120},"notes":{"type":"string"}}}`
Renderer MAY map schema to native controls.
Supported subset SHOULD remain compatible with common JSON Schema tooling and MCP-style elicitation.
## 32. Gradio Board v1
Gradio is an implementation choice, not the protocol.
Board MUST show stage navigation, artifact cards/rows, selected version preview, version history, status, allowed actions, pending-review count, generation status, stale status, lock status, message input, and project status.
Board SHOULD be usable from mobile.
Renderer SHOULD use dynamic rendering for changing artifact lists and MAY poll store on a timer.
Board reads only durable state; hidden model context MUST NOT be display state.
## 33. Board Action Rules
Actions requiring intelligence create inbox events.
Safe deterministic actions MAY use store service directly.
Examples: `approve` may be deterministic; `select_version` may be deterministic and then propagate stale; `revise` normally requires worker; `message` always requires worker; `regenerate` may require worker prompt construction.
Implementation MUST explicitly classify each action.
Repeated browser submission MUST be idempotent.
## 34. Attention
Board SHOULD show: `Waiting on you`, `Agent working`, `Generating`, `Stale`, `Failed`, `Approved`.
Browser title SHOULD include pending review count when practical.
Optional adapters MAY send OS notification, webhook, Slack, email, or host-app push.
Notifications are not required for v1.
## 35. Blocking Convenience Mode
Small workflows MAY use:
```text
surface render questionnaire.json --data questions.json
surface wait <handle> --max 45
`Result:`
{"status":"answered","value":{"choice":"A"}}
`or:`
{"status":"pending","handle":"interaction_7"}
```
Every wait MUST be bounded.
Agent MAY wait again later.
No design may depend on a shell command blocking for human-scale durations.
## 36. Remote Access
Remote browser use is core.
Phase 0 MUST prove one authenticated remote path.
Board SHOULD bind loopback by default.
Remote access SHOULD use a self-controlled tunnel for sensitive/client work or a renderer-provided share mechanism when acceptable.
Reusable bearer tokens MUST NOT remain exposed in URL history.
A bootstrap token MAY exchange for short-lived session/cookie auth.
## 37. Security
Runtime MUST bind locally by default, validate Host where applicable, protect against DNS rebinding, use unpredictable runtime tokens, protect state-changing requests, validate configs/payloads, constrain file paths, reject shell execution from board events, keep provider credentials out of board state, force agent writes through store tools, audit privileged actions, expire leases, and prevent media path traversal.
Remote mode MUST require authentication and encrypted transport through tunnel/proxy.
Public anonymous share links SHOULD NOT be used for confidential client work.
## 38. Privacy
Storage locations MUST be explicit.
Third-party share/tunnel services may route traffic externally; implementations MUST disclose this.
For sensitive work prefer local-only, VPN, SSH forwarding, self-controlled tunnel, or authenticated reverse proxy.
Artifacts SHOULD NOT be uploaded elsewhere merely to render them.
## 39. Cost Guardrails
Config MAY define:
`{"budget":{"stage_usd":20,"project_usd":100,"confirm_above_usd":5}}`
Runtime SHOULD track estimated/actual provider cost where available.
Board SHOULD show estimate for expensive actions.
`bulk_regenerate`, large descendant regeneration, locked-item regeneration, provider/model upgrades, or high-resolution/long-duration work SHOULD require confirmation above configured thresholds.
Budget limits MUST be enforced deterministically outside LLM reasoning.
## 40. Failure Recovery
Board crash: restart renderer; state remains in SQLite.
Supervisor crash: expired claims return pending; restart and rehydrate.
Worker crash before ack: lease expires; event retries.
Worker crash after durable writes before ack: retry detects source event/effect and acks safely.
Poller crash: durable jobs remain; restart and continue.
Provider failure: job failed; emit `job_failed`; artifact enters configured failed/prior state.
Renderer offline: worker/poller MAY continue; renderer rebuilds from store.
DB busy: use WAL, short transactions, bounded retry; never hold transaction during model/provider work.
## 41. Selection Conflicts
Selecting a version SHOULD use optimistic concurrency:
`{"expected_selected_version_id":"ver_88","select_version_id":"ver_91"}`
If current selection changed, return `conflict` plus current state.
No stale browser action may silently overwrite a newer selection.
## 42. Locks
Two concepts MUST remain separate:
**processing lease** = short internal lock for worker serialization.
**user lock** = persistent instruction that automation MUST NOT regenerate/replace without confirmation.
A stale user-locked artifact stays stale+locked and is flagged for user attention.
## 43. Observability
Trace chain SHOULD be: `UI action → event → worker turn → store mutation → job → provider result → version → selection → stale cascade`.
Recommended IDs: `project_id`, `event_id`, `artifact_id`, `version_id`, `job_id`, `worker_session_id`, `transport`.
Logs SHOULD prefer IDs over full sensitive payloads.
Debug mode MAY expose event timeline.
## 44. Completion
Project completes when stage-config rules are satisfied or authorized user closes it.
Movie default: all required stage artifacts approved and final assembly approved.
Completion MUST NOT depend on survival of the original agent session.
## 45. Resource Lifecycle
Starting a project MAY launch board, supervisor, and poller as separate processes.
Each SHOULD restart independently.
`surface stop` SHOULD stop new work, attempt clean worker shutdown, persist state, stop processes, and retain DB/artifacts unless deletion is explicit.
Idle policies MAY stop worker sessions while board/store remain alive.
## 46. SKILL.md Contract
The skill MUST instruct agents to: stay in chat for trivial interactions; open board for 3+ related decisions, long reviews, media, diffs, or non-linear work; prefer presets; create stage config only when needed; never generate UI code in common path; trust store over memory; process events atomically; query relevant artifacts only; write via store tools; create versions not overwrites; never wait on slow generation; create jobs; ack after durable effects; treat `message` as ordinary conversation; respect locks/budgets; rehydrate after restart; return milestones/final result to parent when delegated.
## 47. Movie Walkthrough
1. Parent receives “Create a 30-second product film” and delegates to a movie/storyboard sub-agent.
2. Sub-agent starts `movie.json`; board opens.
3. Worker creates script artifact/version and sets `review`.
4. User edits Scene 2; board emits `edit`; new selected scene version is created.
5. Store marks dependent shots/keyframes/clips stale; no versions are deleted.
6. User approves shots 1–4 in any order.
7. Worker launches keyframe jobs up to configured parallelism and ends turn.
8. Poller emits `job_done` as each image completes; versions appear on board.
9. User sends `revise{"note":"warmer light"}` on image 3 while unrelated clip jobs run.
10. Approved keyframes launch clip jobs; some clips can be review while others generate.
11. User cancels clip 2; provider cancel is attempted; late completion cannot become selected.
12. User edits Scene 2 again; only graph descendants go stale; unrelated approved work remains current.
13. Locked stale descendants are flagged but not regenerated.
14. Worker crashes during revision; event lease expires; supervisor resumes/replaces worker.
15. Rehydration summary comes from store; event retry remains idempotent.
16. Approved clips create assembly job (for example ffmpeg/local/provider).
17. Final assembly reaches review and is approved.
18. Completion rules pass; delegated sub-agent returns result to parent.
## 48. Other Presets
`questionnaire.json`: related structured questions, usually JSON Schema; short cases may use bounded blocking convenience.
`plan_review.json`: draft/sections/risks/final with revise/approve/reopen/message.
`document_review.json`: sections/findings as artifacts; long analysis may become jobs.
`diff_review.json`: files/logical changes as artifacts; renderer may show diffs.
These are configs, not separate workflow engines.
## 49. Example Use Cases
Requirement interviews; document review; research evidence boards; software plan review; code/diff approval; UI option review; image pipelines; video/storyboard pipelines; catalog enrichment; data-cleaning review queues; content production; proposals; approval-heavy automations; QA triage; ERP configuration review; migration plans.
The board is appropriate when the work has persistent objects and decisions, not just conversation.
## 50. Existing Standards We Build On
**JSON Schema:** structured form/input definition; no new form protocol unless necessary.
**MCP/Elicitation:** useful for small structured interaction; board covers a broader persistent lifecycle.
**MCP Apps:** potential future host-native renderer; MUST NOT require store redesign.
**A2UI:** potential declarative native renderer; stage/artifact semantics remain independent.
**ACP:** strong fit for persistent supervisor↔agent sessions and interruption; kept behind WorkerTransport.
**Gradio:** fastest v1 renderer for dynamic Python UI/media; explicitly replaceable.
## 51. Token/Reasoning Efficiency
LLM tokens should be spent on domain decisions, not UI mechanics.
Stage presets replace generated apps; store summaries replace chat replay; targeted store queries replace full project dumps; deterministic stale propagation replaces reasoning; budgets replace model self-restraint; poller replaces repeated “check status” turns; renderer components replace generated markup; event IDs replace natural-language bookkeeping.
Repeated project-specific stage configs SHOULD become shipped presets.
## 52. Performance Targets
Suggested local targets: warm board open <2s; deterministic user action persist <200ms; board reflects local state <1s; idle supervisor pickup <1s; stale cascade over hundreds of nodes <500ms; runtime restart/store open <2s.
No model/provider call may run inside a long SQLite transaction.
Targets are engineering goals, not wire-protocol guarantees.
## 53. Testing
Unit tests MUST cover artifact CRUD, immutable versions, version selection, DAG cycle rejection, stale cascade, locked stale behavior, atomic event claim, lease expiry, idempotent retry, selection conflict, job cancellation, late result discard, and budget enforcement.
Integration tests MUST cover board→event, event→worker→version, job→poller→event→version, worker crash before/after durable write, supervisor restart, poller restart, renderer restart, remote tunnel, session recycling, and direct sub-agent ownership.
End-to-end MUST execute the complete movie walkthrough.
## 54. Portability Acceptance
Before v1 is called portable, identical board/store/stage/skill code MUST run with at least two agent CLIs.
Only transport configuration or adapter code MAY differ.
No stage config, event type, store schema, board contract, or skill behavior may fork by harness.
## 55. Phase 0 — Prove the Loop
Build exactly: `store.py` with artifacts/versions/DAG/events/jobs/leases; `board.py` for movie config; text/image/video renderers; edit/revise/regenerate/select/approve/cancel/message actions; `supervisor.py`; one persistent-stream transport if available; resume fallback; `poller.py`; one image provider; one video provider; authenticated remote tunnel smoke test; full movie walkthrough.
Do not over-generalize before this passes.
## 56. Phase 1 — Harden
Add ACP transport, stronger rehydration, session recycling, robust cancellation, per-artifact worker pool, cost accounting, provider webhook support, richer tracing, conflict UX, recovery CLI, and security tests.
Phase 1 MUST preserve Phase 0 data semantics.
## 57. Phase 2 — Generalize
Ship `questionnaire`, `plan_review`, `document_review`, `diff_review`, and `generic_media_pipeline`.
Add `render + bounded wait` convenience mode.
Add custom stage-config authoring guide.
Add artifact kinds only when real workflows require them.
## 58. Phase 3 — Optional
Possible: stdlib/non-Gradio renderer, MCP Apps renderer, A2UI renderer, TUI, hosted/shared board service, multi-user collaboration, cross-machine worker scheduling, richer notifications, preset marketplace.
None may become v1 core assumptions.
## 59. Acceptance Criteria
V1 passes when: user can act in any order while generation runs; no agent turn waits for slow generation; upstream selection change marks descendants stale within one board refresh; no stale propagation deletes versions; locked descendants are not auto-regenerated; worker death loses no durable state; expired claims retry safely; duplicate delivery does not duplicate semantic output; cancelled job late results cannot become selected; board reconstructs from SQLite; worker rehydrates from store; direct sub-agent ownership works without parent mediation per UI action; remote browser works in Phase 0; common path generates no frontend code; budgets block over-limit generation until confirmed; same skill works with two CLIs via transport differences only.
## 60. Architectural Decisions
1. Store is source of truth.
2. Agent context is disposable.
3. Core = board + inbox + worker + artifact store.
4. Blocking Q&A is convenience only.
5. SQLite is v1 durable coordination.
6. Versions are immutable.
7. Dependencies are a DAG.
8. Upstream selected-version changes propagate stale.
9. Stale artifacts are retained.
10. Generation is always async when slow.
11. Jobs are outside worker turns.
12. Events are at-least-once.
13. Claims use leases.
14. Handlers are idempotent.
15. Serialization is per artifact.
16. One board template uses stage configs.
17. Forms use JSON Schema.
18. Gradio is renderer, not protocol.
19. WorkerTransport isolates harnesses.
20. ACP is preferred, not mandatory.
21. Native stream is valid bootstrap.
22. Resume-per-event is fallback.
23. Remote access is core.
24. Agent writes only via store tools.
25. Budgets/staleness/locks/persistence are deterministic.
26. MCP Apps/A2UI are future renderer integrations.
## 61. Open Questions
Which CLI is best for first persistent-stream transport? Which two CLIs should prove portability? Which ACP adapters are mature enough? Is Gradio polling responsive with hundreds of artifacts? Which `job_done` cases can be deterministic without a worker turn? Where should media live by default? How should very large DAGs be summarized? What lease duration/renewal policy is best? When can safe events be coalesced? Which actions should be deterministic vs worker-driven? How should provider costs be normalized? What is default policy for locked+stale items? How should parent receive milestones from sub-agent? When should board remain after owner finishes?
These MUST NOT block Phase 0 unless required by the movie walkthrough.
## 62. Deferred
Custom component protocol — deferred; use renderer + JSON Schema.    Generated React/shadcn UI — escape hatch only.  
Stdlib renderer — optional later replacement.    FastAPI/WebSockets — only if measured need appears.  
Redis/distributed queue — out of v1.    Multi-user collaboration — deferred.  
TUI — future renderer.    Multiple hosted workspaces — deferred.  
A2UI renderer — future native/declarative renderer.    MCP Apps renderer — future host-native renderer.  
Workflow marketplace — future distribution concern.    Arbitrary custom pages — exceptional escape hatch.  
Cross-machine worker pool — deferred.    Complex visual layout DSL — intentionally not invented.  
Permanent SaaS deployment — not required.
## 63. Standards References
Implementations SHOULD track current versions and isolate compatibility code in adapters/renderers.
- ACP: https://agentclientprotocol.com/ and https://github.com/agentclientprotocol/agent-client-protocol
- MCP: https://modelcontextprotocol.io/
- MCP Apps: https://apps.extensions.modelcontextprotocol.io/
- A2UI: https://a2ui.org/
- Gradio: https://www.gradio.app/docs/
Standards upgrades MUST NOT require changes to artifact/version/event/job semantics.
## 64. Recommended First Build
Build the movie board only.
Use one SQLite DB, one worker, one stream-capable CLI transport, one image provider, one video provider, one poller, and one authenticated remote tunnel.
Prove this sequence:
```text
edit script
→ stale shots
→ approve shots
→ async images
→ revise one image
→ generate clips
→ cancel one clip
→ kill worker
→ recover
→ approve remaining clips
→ assemble
→ finish
```
If this works reliably, the architecture has proven the hard properties it exists to provide.
Only then generalize.
## 65. Final Reference Architecture
```text
                           REMOTE OR LOCAL USER
                                   │
                                   ▼
                          Board Renderer v1
                               Gradio
                                   │
                     read state / emit actions
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────┐
│                    SQLITE PROJECT STORE                      │
│ artifacts ─ versions ─ artifact_dependencies                 │
│ events ─ claim/lease/ack    jobs ─ provider state            │
│ sessions / runtime / locks / kv_state                        │
└───────────────┬──────────────────────────────┬───────────────┘
                │                              │
                ▼                              ▼
          Supervisor                       Job Poller
                │                              │
                ▼                              ▼
         WorkerTransport              Generation Providers
      ACP / native / resume          image / video / local
                │
                ▼
      Parent agent or owned sub-agent
      disposable context, durable tools
```
The board is not a wrapper around a conversation.
The board is a persistent project workspace.
The agent is a replaceable intelligent worker behind that workspace.
