# State: Phase 3 — Board Web Redesign

## Status as of 2026-09-21, paused mid-build

Session did three things, in order:

1. **SKILL.md correction + Q&A round-trip testing** — fixed real CLI/auth
   drift (`--db` is a global flag, not per-subcommand; board auth is HTTP
   basic, not bearer), documented that the shipped worker cannot generate
   (`surface/worker.py` re-commits content unchanged on revise), and named
   the failure mode that made live testing look broken when it wasn't: a
   queued inbox event is visually identical to a dead button (no
   queued/working indicator anywhere in the board).
2. **A parallel renderer for turn-taking work** — `examples/04-live-surface/`,
   a from-scratch FastAPI + static HTML/CSS/JS app where the agent's own
   structured response (`mode`/`blocks`/`controls`) declares the UI,
   proving SPEC principle 9 ("renderer is replaceable") for real. Generalized
   from a hardcoded poem app into a `--task`-driven agent (verified live
   with an unrelated recipe task, zero code changes, genuinely different
   `mode`/block shape in the response).
3. **`demo/` → `examples/`** — reorganized into one directory per pattern
   named in SKILL.md (`01-board-basics`, `02-agent-as-worker`,
   `03-qa-roundtrip`, `04-live-surface`), each self-contained and runnable.
   Manual testing of all four surfaced two real bugs, both fixed: stdout
   never flushed before `board_only.py`'s blocking `launch()` call (so its
   startup banner never reached a backgrounded/logged process), and a
   hardcoded fixed password gating `02`'s board (same class of bug already
   fixed once in `04`'s `TOKEN = "satellite"`) — now no auth by default,
   `--pin` opt-in, matching `04`'s pattern.

Also added to SKILL.md: **use a sub-agent whenever holding a board's worker
loop would otherwise block your own turn** — found this the hard way,
almost held `inbox next --wait 300` in my own foreground turn while a human
was meant to be testing the UI live.

Commits, in order: `692e241` (SKILL.md corrections) `51d88a1` (04 build)
`ac46445` (04 fixes: contrast bug, dead button, satellite→surface rename)
`14ae156` (test suite green: questionnaire preset reshape, resolve() bug)
`566353a` (demo→examples reorg) `353350a` (board_only.py stdout flush)
`334346d` (board_only.py no-auth-by-default + sub-agent guidance).

## What triggered this phase

Manually testing `examples/02-agent-as-worker` against the REAL board
(`surface/board.py`, Gradio) showed the exact confusion the SKILL.md
sections above already named: header reads "Ready · 0 pending review" with
no indication of in-flight work, a "Message the worker" box posts with
`artifact_id: null` (unbound to any artifact), and a flat, full-bleed
layout with no visual hierarchy. This is core skill code — every
`surface serve` stage config depends on it — not a demo file, so the fix
that worked for `04` (drop Gradio, hand-build a renderer) needed the same
treatment applied deliberately, not folded in quietly.

User explicitly chose "full redesign, same as 04" over targeted patches or
leaving it, understanding the stated cost: this touches tested core code
every stage config depends on.

## Update: build agent stalled, but left real, substantial work

The build sub-agent hit a 600s stream-watchdog timeout and never delivered
its own completion report — it stalled mid-sentence ("Let me update the
docs to match what actually runs now"), i.e. partway through its own
documentation pass, after the implementation was already functionally
working. Nothing was lost: the worktree persisted with the diff intact.

**I independently verified, myself, before handing this to review** (not
trusting the stalled agent's unfinished claims):

- `python3 -m pytest -q` in the worktree: **503 passed, 3 skipped** (main is
  477 passed — 26 new tests in `tests/test_board_web.py`).
- The actual fix this phase exists for, live, in a real rendered browser:
  ran `surface/board_web.py`'s `create_app()` standalone (no supervisor, so
  an enqueued event could never be claimed), and confirmed the header reads
  **"1 QUEUED"** (not a static "Ready") and the artifact card shows **"1
  queued action — queued 13s ago, waiting for a worker."** First attempt
  gave a false-negative blank page — I'd embedded `user:pass@host` in the
  navigation URL for convenience, which breaks every same-origin `fetch()`
  the page makes per the Fetch spec (relative-URL resolution inherits the
  embedded userinfo, which the spec then forbids on the request). Not a
  product bug — `board.js` correctly relies on native browser credential
  caching, no header logic of its own to blame. Retested with a real
  Playwright context (`httpCredentials`, not URL-embedded) and it rendered
  correctly. Screenshot: `verify-queued-guaranteed.png` (repo root,
  untracked, evidence for the review pass, not meant to be committed).
- `--renderer web` is now the DEFAULT for `surface serve`; `--renderer
  gradio` kept as an explicit fallback — a real decision made and
  documented, not left ambiguous.
- The `SKILL.md` diff I read (the "Queued Actions Are Visible (web
  renderer) / Invisible (gradio renderer)" section and others) is coherent
  and well-cross-referenced, not garbled by the stall.

**Not yet verified by anyone**: multi-stage/multi-artifact behavior against
`movie.json`, image/video artifact rendering, whether `--renderer gradio`
still genuinely works end-to-end post-diff (not just reads correctly),
auth timing-safety, whether the message-box fix is server-enforced or just
client-side, test quality (26 tests existing isn't the same as 26 tests
meaningfully asserting behavior), and whether `SKILL.md`'s doc pass has any
inconsistency in the parts I didn't personally read. **This is exactly what
the adversarial review below is for — do not treat my spot-checks above as
a substitute for it.**

## Merged: `11e66de` on main. 504 tests passing, 3 skipped.

Adversarial review came back with a clear verdict: **"Safe to merge with
fixes."** It ran things, not just read code — 60 tool calls, real
reproductions against a live server and a real browser. Four blocking
issues, three smaller ones, all fixed before merge:

**Blocking:**
- `/static/*` bypassed auth entirely (`app.mount(StaticFiles)` sits outside
  the app's routing) — replaced with an explicit authed route.
- A malformed `POST /api/action` body threw an unhandled 500 with a
  traceback in the response — now a clean 400.
- `README.md` was stale in 5 places (still described gradio as default,
  wrong install instructions, no `--renderer` flag, `movie` as the
  quickstart preset) — fixed, quickstart switched to `poem`.
- Two claims in the SKILL.md text I'd just written were **already false**:
  the shared `DisplayModelBuilder` fix means the legacy gradio board's
  header *also* now reads "N queued" (a side effect neither the build agent
  nor I had caught), directly contradicting what I'd written saying gradio
  doesn't have this. Fixed, plus a stale "TUI is the only verified
  renderer" paragraph the diff never touched.

**Non-blocking, fixed anyway:**
- An event's expired lease showed "worker is on it" forever in
  board-without-supervisor mode (`examples/02-agent-as-worker`) with
  nothing to reclaim it. `build_board_display()` now calls
  `reclaim_expired_leases()` before reading state.
- "started Xs ago" measured from the event's `created_at`, not when a
  worker actually claimed it. Added `oldest_claimed_at`, tracked separately.
- The client treated any non-2xx response as success (`{}.ok === false` is
  `false`), silently discarding the user's typed text on a 401 with no
  feedback. Now checks `res.ok` first.
- A test asserting stage-config gating posted an `edit` action and never
  asserted anything about it. Fixed the assertion; added an honestly-named
  test (`test_edit_is_not_stage_gated_known_gap`) for the real pre-existing
  gap it was papering over — `edit`/`revise`/`regenerate`/`reopen`/`cancel`/
  `message` were never stage-gated in `ActionHandler`, only
  `approve`/`lock`/`unlock`/`select_version` are. Now HTTP-reachable via
  this renderer; tracked, not silently passing.

Both renderers smoke-tested after every fix: `--renderer web` end-to-end in
a real browser (multi-stage `movie.json`, image/video artifacts, live
refresh, every action) and `--renderer gradio` via a direct
`build_board()`/`DisplayModelBuilder` check confirming the shared
`status_message`/`oldest_claimed_at` changes reach it too.

Worktree removed, branch deleted, main pushed. This phase is done.

## Known follow-up, not done in this phase

Raised mid-review by the user, correctly: **`board_web.py`'s revise/edit
labels are hardcoded JS strings** (`"Ask for a revision"`, `"What should
change?"`) — identical for every artifact regardless of what the worker
actually needs from the user. This is a different, separate gap from the
queued-indicator fix above: `04-live-surface`'s `mode`/`blocks`/`controls`
contract already solves exactly this (the agent declares label/placeholder/
input-shape per turn), but that pattern has never been brought into the
board. Doing so is real scope — it means extending `ActionHandler`'s
contract, not just the renderer — and was deliberately left out of this
phase's brief. Worth its own phase.

Also raised, decided, not yet started: **retire `movie.json`** as the
skill's reference workflow (full retirement chosen: rebuild
`examples/01-board-basics` on a simpler stage config, stop citing movie as
the reference example in SPEC.md/SKILL.md, mark `surface/stages/movie.json`
itself as deprecated rather than advertised). Not started as of this
commit.


---

**This phase is closed.** Merged at `11e66de`, 504 tests passing, 3
skipped. The two follow-ups above (agent-declared control labels, retiring
`movie.json`) are separate, tracked, not started.
