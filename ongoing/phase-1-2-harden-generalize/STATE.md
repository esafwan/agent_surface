# State: Phase 1 (Harden) + Phase 2 (Generalize)

## Status as of 2026-09-20

Built via `/claude_swarm`: 9 parallel/sequential agent tasks, 2 broad verification passes,
one senior adversarial review (Opus), and a follow-up fix pass closing everything the
review found reachable to fix in this session. 411 tests passing, 1 skipped (gradio not
installed in this dev/test environment). Full suite stable across repeated runs. Core
loop (board/CLI → event → supervisor → real worker subprocess → store) re-verified
working after all changes layered on top of Phase 0, including with `--pool-size 2`
actually spawning two concurrent worker subprocesses.

Commits, in order: `f380e91` (ACP transport) `82839e1` (Phase 2 presets) `672d5e4`
(authoring guide) `7ea9312` (security tests) `464ff47` (cost accounting + webhook)
`57f3e34` (board conflict UX + visual redesign) `d916dc6` (recovery CLI + convenience
mode) `af9432b` (supervisor hardening) `f0183fe` (worker "form" type fix) `14b0689`
(pool/recycling wiring + render/wait close-out + recover bug fix) `bc90afb` (lock/unlock
gating fix).

## SPEC section 56 (Phase 1) checklist — honest status

| Item | Status |
|---|---|
| ACP transport | **Scaffolded, not verified.** `surface/transports/acp.py` implements the WorkerTransport interface against an assumed duck-typed client protocol — no real ACP Python SDK was available to verify against. Not wired into `cli.py serve()`. Treat as adapter groundwork for a future integration, not a working ACP path. |
| Stronger rehydration | Done. Fixed a mislabeled field (`pending_event_count` was actually counting artifacts, not events) and broadened `recent_event_ids` to reflect genuinely recent activity. |
| Session recycling | Done and reachable: `surface serve --recycle-after-events N` / `--recycle-after-tokens N`. `recycle_after_tokens` is honestly wired-but-inert — no caller in this codebase reports real token usage yet. |
| Robust cancellation | Job cancellation (Phase 0) unchanged/still correct. Worker-turn interrupt is new: `Supervisor.request_worker_interrupt()` plus a reachable `surface interrupt` CLI command (flag-file signal into a running `serve()` process, since they're separate OS processes). |
| Per-artifact worker pool | Done and reachable: `surface serve --pool-size N`. Verified empirically that the store's SQLite connection is not thread-safe and `claim_next_event` has a TOCTOU window in its artifact-lock check; the pool design keeps ALL store I/O on the calling thread and only parallelizes `transport.send_event`, so this is safe for one in-process pool, not for multiple concurrent supervisor *processes* against one db (undocumented/unsupported, not attempted). |
| Cost accounting | Done. Mock providers report `actual_cost`; poller records it; `get_project_cost_summary()` exists but is not surfaced by any CLI command (minor, not required by spec's enforcement language). |
| Provider webhook support | Done, intentionally without an HTTP server/framework — `surface/webhook.py`'s `handle_webhook_payload()` is the ingestion/handling logic a future HTTP layer would call, sharing the poller's exact completion policy (including cancellation-safety). |
| Richer tracing | Not separately addressed this round beyond the rehydration fixes above. |
| Conflict UX | Done. `select_version` conflicts are now surfaced in the board's status line with the actual current version id, not just handled invisibly at the data layer. |
| Recovery CLI | Done: `surface stop`, `surface recover` (+ `--force-reclaim`), `surface interrupt`. Fixed a bug where `--force-reclaim` could strand an unrelated healthy event under a throwaway worker id. |
| Security tests | Done: 34 tests (`tests/test_security.py`) covering path traversal, SQL injection, shell injection, token entropy, media path handling, lease expiry. All controls verified working; no new vulnerabilities found. |

## SPEC section 57 (Phase 2) checklist — honest status

| Item | Status |
|---|---|
| `questionnaire`, `plan_review`, `document_review`, `diff_review`, `generic_media_pipeline` presets | Done — all validate against the real `StageConfig` loader and are covered by `tests/test_stage_presets.py`. |
| `render` + bounded wait convenience mode | Done and closed end-to-end: `surface render` creates a handle, `surface answer <handle> --value <json>` answers it, `surface wait <handle> --max N` polls bounded by `--max` and returns the exact `{"status":...}` shapes from SPEC section 35. Backed by a local JSON file store under `.surface-board/interactions/`, not the SQLite store — a deliberate, documented simplification rather than inventing new artifact/event semantics. Verified via real CLI subprocess calls (not just direct Python calls in tests). |
| Custom stage-config authoring guide | Done: `docs/stage-config-guide.md`, cross-checked against the live `config.py` validation code. |

## Post-session update (2026-09-21): gradio installed, board live-tested and fixed

Everything below this line happened after the table above was written, once gradio 5.50
was actually installed and the board could be run in a real browser (gradio 6.28 was tried
first and found to crash — a Svelte "effect_orphan" error, blank page; pinned to `<6`).

- **Board live-refresh: FIXED.** Was flagged below as deferred; once gradio was installed
  it became fixable and was fixed. `build_board()`'s dynamic region (tabs, cards, version
  history, forms, action bar) now rebuilds via `@gr.render()` on a 2-second timer plus
  immediately after any action. Verified with a live multi-step round trip (approve ->
  instant update; async edit -> auto-picked-up within ~2s; navigate away and back -> state
  holds, no crash).
- **Real thread-safety bug found and fixed.** Live testing crashed the board with "SQLite
  objects created in a thread can only be used in that same thread" on the second tab
  click — Gradio dispatches each callback to its own thread, but the Store's connection
  was created once and reused across threads. Fixed: `check_same_thread=False` +
  `Store`-wide `RLock`. Proven with real multi-threaded regression tests.
- **JSON-Schema form rendering was entirely missing, not just "unverified."** The
  questionnaire preset's `form_schema` was never actually turned into form fields — a
  "form"-typed artifact rendered as a raw, hand-editable JSON blob. Built proper
  `schema_properties`/`parse_form_answers`/`build_form_answers_json` + native
  Textbox/Dropdown/Number/Checkbox rendering. Verified live: filled a 3-field form
  (including a dropdown), submitted, confirmed the real worker wrote back the exact JSON.
- **The visual redesign's biggest problem was real, not cosmetic:** every form field label
  rendered as a colored pill badge — Gradio's own `BlockTitle` Svelte component, not
  anything in BOARD_CSS. No CSS-variable tuning could have fixed this; the fix was
  disabling Gradio's native `label=` entirely and rendering custom typography instead.
  Committed to one restrained visual language (quiet borders, single accent color used
  sparingly) grounded in gradio 5.50's actual shipped CSS/JS, then verified live.
  Also fixed: the width CSS didn't actually widen anything (Gradio's own
  `display:flex` container has no `width`, so `max-width` alone was a no-op) — confirmed
  by measuring computed width in a real browser (514px on a 1920px viewport before the
  fix, 1600px after).
- **`get_project_cost_summary()` unsurfaced** by any CLI command — implemented and
  correct, just not exposed as a convenience. Still open.
- **ACP transport unverified** — see table above. Still open.

**Lesson for future sessions:** every one of the items above was previously logged as
"can't verify, no browser/gradio available" and left as a documented limitation. Every
single one turned out to hide a real bug once actually run. Treat "unverified because the
tool wasn't installed" as a loud signal to install the tool and check, not a reason to
trust the code as-is.
- Multi-process supervisor pools, richer tracing beyond rehydration, hosted/shared board
  service, multi-user collaboration — Phase 3 territory or explicitly out of v1 scope per
  SPEC (see `PHASE3.md` in this directory for the full accounting).

## Phase 3 update (same session, following user follow-up)

Two of Phase 3's nine items are concretely buildable features (not infrastructure/
ecosystem decisions) and have been built:

- **`surface tui`** (`surface/tui.py`) — a stdlib-only, non-Gradio terminal renderer.
  This is the only renderer in the system that has actually been run and verified in
  this environment (the Gradio board never has been — see above).
- **`surface/notify.py`** — notification adapters (`LogNotifier`, `WebhookNotifier`) and
  a pure `detect_notifications()` diff function, per SPEC section 34/58. Not yet wired
  to fire automatically inside `serve()`'s loop — that's a small follow-up, described in
  the module docstring.

See `PHASE3.md` for why the remaining 7 items (hosted service, multi-user collaboration,
cross-machine scheduling, preset marketplace, MCP Apps renderer, A2UI renderer) are
infrastructure/ecosystem decisions without a fixed "done" state, not deferred out of
laziness.

## Real-agent backend integration update (2026-09-26): supersedes the earlier CLI smoke test

This session replaced the earlier smoke-test approach (manual bypass scripts shelling out
to opencode/codex directly and hand-writing results to the store) with real integration
through the production `Supervisor → NativeStreamTransport → surface.worker` dispatch path.
Every event now claims, deserializes, times out, recovers, and re-attempts exactly as it
would in the live board, with the agent CLI integrated as a normal part of the worker's
handler, not a side channel.

**What was built:**

- `surface/agents/` package:
  - `base.py`: `AgentBackend` Protocol defining the interface every agent CLI adapter must
    implement (`build_argv`, `parse_output`, `env_overrides`). `AgentError` and `AgentReply`
    types for normalized error/success handling.
  - `runner.py`: `run_agent()` function that spawns an agent CLI subprocess, enforces a
    timeout by killing the whole process group (not just the child), and parses output via
    the backend's parser. Includes the critical fix: `start_new_session=False` so the agent
    CLI child inherits the worker's process group, allowing a single top-level `killpg` to
    reap the entire tree (worker + agent CLI + any grandchildren the CLI spawned).
  - `prompt.py`: `build_prompt()` constructs the prompt sent to the agent CLI, using context
    about the event type, note/message, selected content, and artifact stage.
  - `registry.py`: `get_backend()` registry lookup by name (e.g. "opencode", "codex", "agy",
    "fake").
  - Adapter implementations: `opencode_backend.py`, `codex_backend.py`, `agy_backend.py`,
    `fake_backend.py` — each implements the `AgentBackend` protocol for its CLI, handling
    model ID format, argv construction, stdout/stderr parsing, and env overrides.

- `surface/transports/native_stream.py` process-group safety fix:
  - Added `_killpg_kill()` and `_killpg_interrupt()` helpers that send signals to the entire
    worker process group (via `os.killpg()` on POSIX), not just the worker process itself.
  - **Why this was needed**: A worker that shells out to an agent CLI (e.g. `opencode run ...`)
    creates a grandchild process. If the worker didn't use `start_new_session=True` (which
    it doesn't, per the runner.py design), then a naive `process.kill()` would only kill the
    worker, leaving the agent CLI running as an orphan. The process-group fix ensures one
    top-level `killpg(worker_pgid, SIGKILL)` reaps the entire tree.

- `surface/worker.py` `AgentConfig` class and integration:
  - `AgentConfig` holds optional `name`, `model`, `timeout`, and `log_dir` for the agent
    backend. When `name` is `None` (the default), the worker is the deterministic reference
    worker, unchanged from Phase 0 — all 611+ pre-existing tests still pass without modification.
  - `_handle_revise()` uses `_generate_agent_content()` when `agent_config.enabled` is true,
    dispatching to `run_agent()` with the backend, prompt, and timeout.
  - Errors from `run_agent()` (timeout, crash, bad output, auth, rate limit) map to the
    normal retry path, with lease expiry leading to automatic re-attempt, no special handling.

- `surface/cli.py` wiring:
  - Added `--agent <name>`, `--agent-model <id>`, `--agent-timeout <seconds>` flags to the
    `serve` command.
  - When `--agent` is set, the worker subprocess is invoked with `--agent` and related flags
    passed through. The transport timeout is set to `max(turn_timeout, agent_timeout + 30)`
    to give the agent CLI headroom.

- `tests/test_agent_faults.py`: Real fault-injection test suite exercising the full production
  path (not a bypass). Supervisor claims events, dispatches through NativeStreamTransport
  into a real `python -m surface.worker --agent fake` subprocess, which then shells out to
  the fake agent CLI backend. Tests verify:
  - Crash → lease expiry → automatic re-attempt.
  - Timeout → process-group kill → no orphan processes.
  - Successful convergence through the real path (not just deterministic worker logic).
  - Worker-count is as expected; no stray orphans left behind.

- Individual real-CLI tests (skipped in normal CI unless `@pytest.mark.live_agent` is
  enabled): `tests/test_agents_opencode.py`, `tests/test_agents_codex.py`,
  `tests/test_agents_agy.py`. Each calls the real CLI (if installed) and verifies output
  parsing without network timeouts.

**Test results (regression gate, 3 consecutive runs after the T7 stdin fix + T8 E2E test):**

- Full suite: **640 passed, 3 skipped**, identical across all 3 runs (skipped: Gradio-related
  tests requiring gradio 5.50 which is not installed in this test environment).
- All 411+ pre-existing tests continue to pass (deterministic reference worker path).
- All real-agent-path tests pass, including the fault-injection suite through the production
  Supervisor/transport/worker dispatch path, and the board HTTP E2E test (T8).
- Protected-path check: `git diff --stat 82de560^..HEAD -- surface/board_web.py surface/board.py
  surface/static surface/store.py surface/schema.py surface/stages` is empty across this
  entire session's work (`82de560` is this session's first commit, T0). Board/store/schema are
  untouched by the agent-backend integration.

**Soak-test evidence (real HTTP against live `surface serve` process):**

Located at `ongoing/phase-1-2-harden-generalize/live_runs/`:

- **`fake-1790375994.jsonl`**: 2 runs, both converged (`converged: true`), both `ok: true`.
  No orphan processes on timeout (`orphan_workers_remaining: 0`). Latency ~1 second per run.
  ✓ **Proven**: The fake backend converges, recovers cleanly, and leaves no orphans.

- **`opencode-1790376009.jsonl`** (superseded, kept as evidence of the bug below): 5 runs,
  none converged, all timing out at exactly the configured `agent_timeout` (~180s = 3x the
  poll deadline). Root cause found: `run_agent()`'s subprocess left `stdin` unredirected
  (inherited). In isolation that's harmless, but `run_agent()` runs *inside* the worker
  subprocess, whose own stdin is the long-lived pipe `NativeStreamTransport` uses to send
  future turns — deliberately never closed between turns. The opencode CLI child inherited
  that pipe, treated its non-EOF stdin as something to read/monitor, threw an unhandled
  EPIPE, and hung until timeout — reproducibly, independent of prompt complexity. This was a
  real dispatch-path bug, not a model-capability issue as first suspected. Fixed with
  `stdin=subprocess.DEVNULL` on the agent CLI child (`surface/agents/runner.py`).
- **`opencode-1790417636.jsonl`** (post-fix): 5/5 runs converged, including the run-3
  mid-turn SIGKILL (recovers automatically via lease expiry, no human polling), 0 orphan
  processes on every run. Latency 9–22s per run. ✓ **Proven.**
- **`agy-1790417636.jsonl`** (post-fix): 5/5 runs converged, including the run-3 mid-turn
  SIGKILL, 0 orphan processes on every run. Latency 9–31s per run. ✓ **Proven.**
- **`codex-1790419045.jsonl`**: 5/5 runs converged, including the run-3 mid-turn SIGKILL,
  0 orphan processes on every run. Latency 6–10s per run. ✓ **Proven.** (Closed the one gap
  flagged by the T11 senior review below: all three registered real backends now have soak
  evidence, not just unit-level live-CLI tests.)

**Unchanged from Phase 0 (confirmed via git diff):**

```
surface/board_web.py     — unchanged (board still no-API, live, browser-controlled)
surface/board.py         — unchanged
surface/static/          — unchanged
surface/store.py         — unchanged by this session's agent work
surface/schema.py        — unchanged
surface/stages/          — unchanged
```

The board's entire architecture (no-API, live polling + action-triggered refresh, store-backed)
remains untouched. All integration of the real agent CLI is at the worker level, below the
board, exactly as designed.

**Senior adversarial review (T11, Opus low effort):** Verdict was accept, with two gaps
flagged: codex lacked soak evidence (closed above, same session), and the board E2E was
HTTP-level rather than browser-driven for the automated suite. Both the automated
`tests/test_board_agent_e2e.py` (HTTP-level, in-suite) and a manual real-browser run against
a real `surface serve --agent opencode` process (not committed as an automated test, since it
needs a real network-calling CLI) confirmed the board's live, no-API UX is driven end-to-end
by a real agent with no manual DB access. The review explicitly confirmed: no bypass path
exists, fault injection runs through the real Supervisor/transport/worker path, both fixes
(process-group kill, stdin=DEVNULL) are real and minimally targeted, harness independence
holds (49–84 lines of pure per-backend glue), and the board/store/schema diff is empty.

**What remains open:**

1. **`kimi` backend**: Documented in `docs/agent-backends.md` but dropped from the active
   backend set due to subscription auth requirement (not installed/auth'd in the current
   environment). When kimi is authenticated, adding `kimi_backend.py` would be identical
   in structure to the others.

2. **Soak statistics are an existence proof, not a reliability claim**: N=5 per backend with
   one mid-turn kill each proves the mechanism works, not a failure rate. A larger N would be
   needed before citing a specific reliability number.

3. **A fully browser-driven (not just HTTP-level) automated E2E test** exercising a real CLI
   through Playwright or similar isn't in the automated suite — the manual browser run above
   covers this once, but isn't repeatable via `pytest`.
