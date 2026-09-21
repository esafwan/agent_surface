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

## Adversarial review — in progress

A second Opus sub-agent is reviewing the worktree diff against the checklist
above (auth, query correctness w/ lease expiry, multi-stage, message-box
server-side enforcement, action correctness via real clicks, test quality,
`--renderer gradio` regression check, and the rest of `SKILL.md`'s diff),
told explicitly to run things and click through flows, not just read code.
Review only — it will not modify the worktree or touch `main`.

## In progress before that — DO NOT TRUST THIS SECTION AS A COMPLETION REPORT

A background sub-agent (Opus, isolated git worktree, NOT yet reviewed or
merged) is building `surface/board_web.py` — a FastAPI renderer reusing
`DisplayModelBuilder`/`ActionHandler`/`BoardDisplayModel` from `board.py`
**unchanged** (same "renderer is swappable" proof as `04`, this time for
real). Brief given (full detail in the Agent tool call this session):

- **P0**: text/form artifact types, edit/revise/approve/reopen, multi-stage
  tabs, version history/select-version, a **genuine** queued/working
  indicator (new `Store` query: is there a pending/processing inbox event
  against this artifact right now?), fix/remove the unbound "message the
  worker" box, `04`'s design tokens (centred column, light+dark,
  `pre-wrap`, disabled buttons that stay legible — the exact contrast bug
  from `04` was named explicitly so it isn't repeated).
- **P1** (land if time allows, defer honestly otherwise): image/video/diff
  rendering, job/generation progress, locks, budget confirmation, live
  refresh.
- Told explicitly to run the full test suite before/after, verify in a real
  browser via Playwright against both `poem.json` (simple) and `movie.json`
  (complex, multi-stage), and report honestly what's verified vs. deferred
  — not to claim parity it hasn't checked.

**Last confirmed state before pausing** (checked via worktree file mtimes,
not by reading the subagent's transcript — that would overflow context):
worktree at `.claude/worktrees/agent-a4d86998e3c393ee5`, branch
`worktree-agent-a4d86998e3c393ee5`, based on `334346d`. Files touched:
`surface/board.py`, `surface/cli.py`, `surface/store.py`,
`tests/test_cli.py`, `pyproject.toml` (modified); `surface/board_web.py`
(new, 435 lines as of last check), `surface/static/{board.html,board.js,
board.css}` (new), `tests/test_board_web.py` (new). File timestamps showed
continuous activity up to the moment of the last check (~19:22–19:25 local,
no gap) — genuinely working, not hung. **No commit made yet in the
worktree** — still on `334346d`, changes are working-tree diffs.

## To resume

1. Check the sub-agent's status: `ListAgents` (name/id: the one spawned
   this session for "Build a redesigned board renderer" — agentId is in
   this session's own tool-call history, not repeated here since ids are
   internal/not for user-facing text). If a `<task-notification>` already
   arrived, its `status` field says `completed`/`failed`; read the result
   it carries — do not re-derive from the worktree by hand once a real
   report exists.
2. If still running: worktree file mtimes are a legitimate way to check
   liveness without reading its transcript (`find <worktree> -newer
   <reference-file>` or `stat -f "%Sm %N" <files>` compared to `date`).
3. Once it reports: **do not merge on trust.** Per the user's request this
   session ("review with opus 5 low"), run a second Opus pass reviewing the
   worktree diff adversarially before anything touches `main` — check the
   P0 list above was actually met, the queued/working indicator is real
   (not just CSS), the unbound-message-box bug is actually fixed, tests
   genuinely pass (not just claimed), and the browser screenshots it
   produced actually show what it says they show.
4. Decide, with the user: replace `surface serve`'s default renderer
   outright, or add it behind a flag (`--renderer web` vs. keeping Gradio
   as `--renderer gradio`) — the build brief left this as the agent's call
   to make and report on, not a pre-decided outcome.
5. Merge from the worktree into `main` only after review; then `git
   worktree remove` it (or leave it — worktrees auto-clean if unchanged,
   per the Agent tool's own description, but this one has real changes so
   won't auto-clean silently).

## Known-good baseline if this needs to be abandoned

`main` at `334346d` is fully working and pushed: 477 tests passing, 3
skipped, all four `examples/` runnable and manually verified this session.
Nothing about this phase is required for the skill to function — it is a
UX improvement to the existing, working (if confusing) Gradio board.
