# State: Phase 4 — Visual Planning Extensions

## Status: closed. Merged through `3bf4d5e` on main. 554 tests passing, 3 skipped.

## What this closes

`fix/dreamy-curie-epezxt` (a PR from a parallel session) added two research/
plan docs — `ongoing/research-visual-planning-extensions.md` and
`ongoing/plan-visual-planning-extensions.md` — proposing three board
extensions (Mermaid-capable markdown rendering, a read-only agent task/
progress board, a dependency-graph view) but never implemented them. Per a
goal check-in ("did we finish all that was planned... if no, do it fully
with claude_swarm"), the docs were copied to main (`dc6efff`, re-attributed,
no Claude co-author trailer) and the source PR/branch closed and deleted,
then the plan's own T1–T6 dependency graph was executed via `claude_swarm`.

## Execution summary

Ladder used: Haiku (default) → Sonnet Low (one escalation) → Opus (two
review passes, not "Low" specifically — this session doesn't expose a
reasoning-effort dial via the Agent tool, only model choice).

| Task | What | Model | Outcome |
|---|---|---|---|
| T1 | Mermaid-capable markdown renderer | Haiku | Built, then a real bug (mermaid.js leaking its own error-SVG onto the page, independent of the calling code's catch block) found by independent verification and fixed in the same pass |
| T2 | Server-side version diff (`surface/diff.py`) | Haiku | Clean first try — fully disjoint new files, 29 real tests |
| T3 | Diff view wired into cards + confirmed T1's mermaid support is generic | Haiku | Clean; correctly identified that plan_review.json needed no changes since T1 was built generically |
| T4 | Read-only task-board preset + column view | Haiku | Clean implementation; its own report overclaimed one thing (said Approve/Reopen only shows on "Needs Approval" — false, confirmed pre-existing app-wide behavior, not a T4 regression) |
| T5 | Dependency graph view | Haiku → **escalated to Sonnet** | First attempt stuck 30+ min, reset its own work to zero net diff, last message suggested a path-resolution loop. Stopped (auto-cleaned, nothing lost) and escalated with the stuck-cause folded into the brief. Escalated attempt succeeded clean, with specific and accurate visual verification |
| T6 | Integration tests, SKILL.md/README docs | Haiku | One test (`test_task_board_approve_button_appears_on_all_cards`) had assertions that could never fail regardless of real behavior — fixed in the same pass with real assertions |

Each task ran in its own isolated git worktree (mandatory per this session's
swarm invariants once file-scope overlap was classified: T1 and T4 both
touch `board.js`/`board.css`, isolated and merged by hand with no actual
git conflicts). Every task was independently verified — scope diff, full
test suite, and a live browser check — by the orchestrating agent before
merging, not accepted on the sub-agent's own report.

## Architectural decision, made once, honored throughout

All UI work (T1, T3, T4, T5) targets **only** the web renderer
(`surface/board_web.py` + `surface/static/`) — the default `surface serve`
renderer since the prior phase's board_web.py rewrite. The legacy Gradio
renderer (`surface/board.py`) was explicitly out of scope. Confirmed by the
final review: zero line changes to `surface/board.py` across all 14 commits
of this run.

## Final adversarial review: 3 real bugs found, all fixed

A second Opus pass reviewing all six tasks together (not task-by-task) found:

- **P1**: `stale` was missing from `board.js`'s status→column mapping — a
  real, auto-assigned status (`Store._propagate_stale`) silently vanished
  from the task board with no column, no count, no trace. Root cause named
  explicitly by the reviewer: no status-column-grouping test existed at all.
- **P2**: the "Compare to previous version" button rendered on every
  multi-version card regardless of content type, but the diff-container
  element only exists for text content — dead button on forms, misleading
  error on media.
- **P3**: `_mermaid_label()` escaped only quotes before a worker-written
  title reached the DOM as SVG via `innerHTML`. A title containing HTML
  produced a real `<img>` element in the rendered dependency graph and
  fired a real outbound request on its `src` (handler attributes were
  stripped by mermaid's bundled DOMPurify, so not script execution, but
  real markup injection and an attacker-controlled request from an
  auth-gated page).

All three fixed and independently re-verified live (real stale propagation
via `put_version()`, a real form artifact confirming zero Compare buttons,
a real malicious title confirming zero `<img>` elements and no fired
handler) before the closing commit `3bf4d5e`. A status-coverage test was
added (`test_task_board_server_state_covers_every_status_including_stale`)
as the closest available tripwire for P1's class of regression — the repo
has no JS test infrastructure, so it can't test `board.js`'s mapping
directly, only lock down the server-side status set it must stay in sync
with.

## Accepted caveats, not fixed, documented honestly

- **CRLF/LF diff noise**: `difflib` diffs by exact line content; mixed line
  endings between two versions can show every line as changed. Correct
  behavior, no normalization was asked for, real caveat for a Windows-saved
  or pasted-content scenario. Noted in SKILL.md.
- Unpinned `mermaid@10` CDN script (floating minor/patch, no SRI) — offline
  degradation to text fallback is handled.
- Diff endpoint: a nonexistent version returns 404 before the
  artifact-ownership check runs (minor probe-ability, behind auth); a
  nonexistent `artifact_id` with valid version ids returns 400 not 404.
  Both cosmetic.
- Dead markup in minimal task cards (an "updated Xm ago" line CSS already
  hides) — zero user impact.

## Where the two originally-tracked follow-ups from Phase 3 stand

Unrelated to this phase, still open, still not started:
- Agent-declared control labels for the board (the `04-live-surface`
  `mode`/`blocks`/`controls` pattern has never been brought into the board)
- Retiring `movie.json` as the reference workflow (scope decided: full
  retirement; not started)
