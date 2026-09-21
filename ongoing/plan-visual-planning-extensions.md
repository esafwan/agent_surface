# Plan: Visual Planning Extensions (Mermaid Spec, Agent Task Board, Dependency Graph)

**Branch:** `fix/dreamy-curie-epezxt` (single PR, all sub-tasks land here in dependency order)
**Precedes this doc:** `ongoing/research-visual-planning-extensions.md`

## Reframed scope (per feedback)

The "kanban/todo" idea is **not** a user-editable task manager. It's a
**read-only progress + approval surface for agent-decomposed work**: when a
parent agent breaks a task into sub-tasks (or hands work to sub-agents), the
board should show that decomposition and its live status, and let the user
approve — nothing more. This actually simplifies the design versus the
original research doc:

- No new user action types. `create`/`revise` of task artifacts are
  **agent-only** (the worker writes them, same as any other artifact).
  The only user-facing action is `approve`/`reopen`, which already exists.
- The board renders task artifacts exactly like it renders anything else —
  grouped into columns by `status` instead of tabbed by `stage`. It's a new
  **view mode**, not a new capability.
- This lines up with SKILL.md's existing "Sub-Agent Ownership and Handoff"
  section almost exactly: parent delegates, sub-agent works, parent/user
  sees milestones. The task board is just that pattern made visible instead
  of implicit.

All three proposals from the research doc remain in scope, reframed:

1. Markdown spec artifact with Mermaid rendering + version diff.
2. Read-only agent task/progress board (status columns, approval gate).
3. Dependency graph view (reuses the Mermaid renderer from #1).

---

## Dependency graph

```text
T1 (mermaid renderer)         T2 (version diff)        T4 (task-board preset)
        │                            │                        │
        ├────────────┬───────────────┘                        │
        ▼            ▼                                        │
T3 (markdown/spec artifact + card)                             │
        │                                                      │
        ▼                                                      ▼
T5 (dependency graph view, built on T1) ◀───────────────────────
        │                                                      │
        └──────────────────────┬───────────────────────────────┘
                                ▼
                    T6 (docs + tests + SKILL.md update)
```

- **T1** and **T4** have no dependencies — start both immediately, in parallel.
- **T2** has no dependencies — can start immediately alongside T1/T4.
- **T3** needs T1 (renderer) and T2 (diff) done.
- **T5** needs only T1, but logically follows once T3 exists (proves the
  Mermaid embed works against a real artifact before reusing it for graphs).
- **T6** is last: it documents and tests the merged surface, so it depends
  on T3, T4, and T5 all being in place.

Two independent lanes can run in parallel: **{T1, T2, T4}** first, then
**T3 → T5**, with **T6** closing it out.

---

## T1 — Mermaid-capable markdown renderer (foundation)

**Depends on:** nothing
**Files:** `surface/board.py` (new render helper), possibly a small JS asset
bundled via `gr.HTML`

- Add a renderer component that takes markdown text, extracts fenced
  \`\`\`mermaid blocks, and renders them via mermaid.js inside a `gr.HTML`
  element; renders the rest as normal markdown.
- Smoke-test specifically against the pinned Gradio range (`>=4.0,<6`,
  currently verified at 5.50) — this project already hit one Gradio-6
  incompatibility (`effect_orphan`), so any new HTML/JS embed must be
  checked against that same pin before it's assumed safe.
- No store/schema change. Pure renderer addition.

**Acceptance:** a markdown string containing a ```` ```mermaid ```` block
renders as a diagram in a live `surface serve` board, on gradio 5.50.

---

## T2 — Server-side version diff

**Depends on:** nothing
**Files:** `surface/board.py` (or a small `surface/diff.py` helper), unit tests

- Given two version IDs of the same artifact, compute a unified diff of
  their `content` server-side (e.g. `difflib.unified_diff`), render-time
  only — **never** persisted to browser `localStorage` (would break
  "store is truth"; a second tab or a refresh must show the same diff
  computed fresh from SQLite).
- Surface as a small helper function first, decoupled from any specific
  card UI — T3 wires it into the board.

**Acceptance:** given two version rows, returns a diff structure/string
that a renderer can display; unit-tested independent of Gradio.

---

## T4 — Agent task/progress board preset (read-only)

**Depends on:** nothing
**Files:** `surface/stages/task_board.json` (new preset), `surface/board.py`
(new column-by-status view mode), `docs/stage-config-guide.md` update

- New stage config: single stage (e.g. `tasks`), artifact type `task`,
  `allowed_actions: ["approve", "reopen"]` only — **no `edit`, `revise`, or
  `message`** exposed to the user for this artifact type. Sub-tasks are
  created and status-updated only by the worker (`create`/`revise`/
  `set-status`), driven by its own decomposition, exactly like any other
  worker-driven artifact.
- New board view mode: render artifacts for this preset grouped into
  columns by `status` (`draft`→"Planned", `generating`→"In Progress",
  `review`→"Needs Approval", `approved`→"Done", `failed`/`cancelled` as
  their own columns) instead of the stage-tab layout. Status-based columns
  need zero new stage-config surface — they're free across any preset.
- Approval gate: a task in `review` shows the existing `approve`/`reopen`
  buttons — this is how "show status and get approval" is satisfied without
  adding a new action type.

**Acceptance:** a worker that creates N task artifacts and updates their
`status` as it works produces a live-refreshing read-only column view; the
user can approve a task in `review` and nothing else on that card is
clickable.

---

## T3 — Markdown/spec artifact wired into a real preset

**Depends on:** T1, T2
**Files:** `surface/stages/plan_review.json` (extend existing preset),
`surface/board.py` (card wiring)

- Wire T1's renderer into the artifact card for `text/markdown` content:
  the plan/spec artifact's selected version renders through the Mermaid-
  aware renderer instead of plain text.
- Add a "compare to previous version" control on the card that calls T2's
  diff helper between the selected version and the prior one, rendered
  inline (not stored anywhere client-side).
- "Targeted editing" stays a **prompt convention**, not a new store
  primitive: the per-card revise box can accept "replace section X with…"
  as free text; the worker still commits one full-document `put-version`
  per edit, per Architectural Decision #6 (versions are immutable
  whole-artifact snapshots). No new event type.

**Acceptance:** a `plan_review` artifact with Mermaid fences in its content
renders diagrams on the card; selecting two versions shows a real diff;
editing still produces one ordinary immutable version.

---

## T5 — Dependency graph view

**Depends on:** T1 (renderer); sequenced after T3 to prove the embed on
real content first, though it does not technically require T3's diff work

**Files:** `surface/board.py` (graph-to-mermaid helper + view), reuses
`store.graph()` / `surface store graph <artifact_id>`

- New helper: given an artifact ID, walk its DAG neighborhood (upstream +
  downstream, not the whole project) via the existing `store graph` query,
  and emit `graph TD` Mermaid syntax with node status encoded as styling
  (approved=green, stale=amber, failed=red, locked=padlock label).
- Render through T1's Mermaid renderer as a new "Dependencies" tab/button
  on an artifact's card.
- Explicitly scope to local neighborhood by default (matching how the CLI
  command already scopes it) rather than rendering hundreds of nodes at
  once — full-project graphs are a possible future flag, not the default.

**Acceptance:** opening an artifact's "Dependencies" view renders a Mermaid
graph of its immediate upstream/downstream neighbors, colored by status;
editing an upstream artifact and re-opening the view shows the new stale
coloring without restarting `surface serve`.

---

## T6 — Docs, tests, SKILL.md update

**Depends on:** T3, T4, T5

**Files:** `tests/` (new test modules), `SKILL.md`, `README.md`,
`docs/stage-config-guide.md`

- Unit tests: Mermaid extraction/render helper (T1), diff helper (T2),
  status-column grouping (T4), DAG-to-mermaid neighborhood helper (T5).
- Integration test: task-board preset end-to-end (worker creates tasks,
  updates status, user approves one, column view reflects it live).
- SKILL.md: document the task-board preset as the recommended pattern for
  "long multi-step decomposed work where the user should see progress and
  approve," alongside the existing "When to Open the Board" guidance —
  explicitly distinguish it from the movie/pipeline tab layout.
- README: add the new preset and view modes to the stage-preset list and
  architecture diagram if warranted.

**Acceptance:** `python -m pytest -q` green with new tests included; docs
describe the three new surfaces and when to use each.

---

## Suggested execution order (single PR, ordered commits)

1. T1 (mermaid renderer) and T2 (version diff) and T4 (task-board preset) —
   independent, can be done in any order or in parallel by separate agents.
2. T3 (spec artifact wired to T1+T2).
3. T5 (dependency graph, reuses T1).
4. T6 (tests + docs, closes out all of the above).

This keeps every intermediate commit shippable on its own (each sub-task
has its own acceptance criteria) while landing as one PR against
`fix/dreamy-curie-epezxt`.
