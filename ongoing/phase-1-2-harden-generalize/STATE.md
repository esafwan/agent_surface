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

## Known gaps carried forward (not attempted this round)

- **Board live-refresh** (Phase 0 debt, reconfirmed still open): the artifact/version card
  tree is built once at `build_board()` launch; `Refresh` updates only the header/status
  line. Needs a `gr.Timer`- or bounded-slot-driven rebuild — deferred because it requires
  gradio actually installed to verify (not available in this dev/test environment), and an
  unverified rewrite is exactly what produced real bugs in an earlier fix round.
- **Board visual redesign is unverified by any real render.** `surface/board.py`'s Gradio
  layout was substantially redesigned for visual quality (compact metadata line, content
  given visual prominence, action-bar with variant-differentiated buttons, a real CSS
  stylesheet) without gradio installed to actually run it. A senior review found no
  hard-crash-class API misuse by careful reading, but **this must be visually verified
  once gradio is installed** before trusting it in production.
- **`get_project_cost_summary()` unsurfaced** by any CLI command — implemented and
  correct, just not exposed as a convenience.
- **ACP transport unverified** — see table above.
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
