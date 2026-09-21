# Research: Visual Planning Extensions (Markdown/Mermaid Spec, Kanban, Dependency Tree)

**Status:** Research / pro-con analysis, not yet scheduled into a phase.
**Question asked:** Are these natural extensions of the board, or scope creep?

Three proposals evaluated:

1. Spec shown in a markdown editor, with a diff view and targeted (section-level)
   editing, plus Mermaid rendering — locally or over a tunnel.
2. Decomposed todos shown as a **read-only** kanban board (or a plain todo list).
3. A tree/graph view of dependency-mapped todos.

## Short answer

Yes — all three are natural extensions, not a new product. Each one is a new
**renderer view over data the store already has** (artifacts, versions,
`artifact_dependencies`). None require a new event type, a store schema
change, or a change to the worker contract. That's exactly the shape the
spec already anticipates: Principle 3 ("UI is deterministic... shipped
renderer, not generated code"), Principle 9 ("renderer is replaceable;
Gradio-specific concepts MUST NOT leak into store"), and the `diff_review` /
`plan_review` presets already gesture at #1 and #2 without a real diff or
board layout for them yet.

The one genuinely new piece of infrastructure, shared by #1 and #3, is a
Mermaid/graph rendering widget in the Gradio board. Everything else is
read-only projection of existing rows.

---

## 1. Markdown spec editor: diff, targeted editing, Mermaid, tunnel

**What it needs, mapped to what exists:**

| Ask | Existing hook |
|---|---|
| Markdown spec as an artifact | Already representable: `text/plain` (or new `text/markdown`) artifact under `plan_review`/`document_review`/`generic_media_pipeline` presets |
| Versioned editing | Already exists: `edit`/`revise` events → `put-version`, immutable versions |
| Diff between versions | **New**: no diff renderer exists yet; `diff_review.json` names the concept but nothing renders a unified diff today |
| Targeted (section) editing | **New UX convention**: patch a heading/section instead of resubmitting the whole document. Doesn't need a new store primitive — still lands as one `put-version` per edit — but the board needs to let the user select a section and the worker needs a convention for "replace section X" |
| Mermaid diagrams | **New renderer component**: Gradio's `gr.Markdown` has no built-in Mermaid support; needs a `gr.HTML` embed with mermaid.js, or a custom component |
| Local host or tunnel | **Already solved**: `surface serve` binds loopback with a per-run token (§36 of SPEC); "self-controlled tunnel" is already the documented remote path. No new work. |

**Pros**
- Fits squarely inside the existing document/plan review presets — it's a
  rendering upgrade to a case the spec already names, not a new artifact
  kind or event type.
- Mermaid rendering is *doubly* useful: the same widget can later render the
  project's own dependency DAG (see #3), so building it once pays for two
  of the three asks.
- A diff view directly strengthens an existing weak point: right now
  "version history" is a list of blobs: the user has to eyeball two full
  documents to see what changed.

**Cons / risks**
- "Local storage diff" as worded conflates two different things: a diff
  *computed* from two immutable SQLite versions (fine, keeps Principle 1 —
  store is truth) vs. state *persisted* in the browser's `localStorage`
  (would violate it — a refresh or a second tab would show a different
  world than the store). Worth building the diff as a pure render-time
  computation over two version rows, never as client-persisted state.
- "Targeted editing" implies either (a) a UX convention layered on top of
  full-document `put-version` (cheap, recommended), or (b) real sub-document
  patch semantics in the store (expensive, not needed — versions are
  deliberately whole-artifact snapshots per Architectural Decision #6).
  Stick to (a).
- Mermaid via `gr.HTML` needs to be verified against the same Gradio
  version pin issue already burned once (`>=4.0,<6`, since 6.x's Svelte 5
  rewrite broke this board). Any new HTML/JS embed should be smoke-tested
  against 5.50 specifically, not assumed compatible.

**Verdict:** natural extension. Mostly a renderer-side diff + Mermaid embed
on top of the `plan_review`/`document_review` presets that already exist.

---

## 2. Read-only kanban (or plain todo list)

**What it needs, mapped to what exists:**

Artifact `status` (`draft/generating/review/approved/stale/failed/cancelled`)
and `stage` are already columns waiting to happen. A kanban view is:
`surface store list` grouped by status (or stage) → render as columns. No
new event, no new action — explicitly **read-only**, so it doesn't even
reach the "safe deterministic action" tier (select-version, approve,
lock/unlock) that already exists; it's strictly below that, a pure
projection.

**Pros**
- Cheapest of the three by a wide margin: no backend change, no new store
  read query beyond what `store list`/`project summary` already return.
- Directly answers a documented gap: "Queued Actions Are Invisible" (SKILL.md)
  — today there's no at-a-glance progress view; a header just says "Ready."
  A kanban/todo glance view is a genuine UX fix, not just a nice-to-have.
- Composable with the existing tab-based board: can ship as a second view
  mode (`--view kanban`) or an additional tab, without touching the
  stage-tab renderer used for the movie/pipeline case.

**Cons / risks**
- The spec is explicit that tabs suit a pipeline and poorly suit
  conversational/todo-shaped work ("Match Stage Shape to the Work"). A
  kanban view is really the fix for that mismatch for non-pipeline stage
  configs (`questionnaire`, ad hoc todo lists) — worth framing it that way
  rather than as a second view of the *same* movie pipeline, where tabs
  already work fine.
- Column definition needs one small decision: columns by `status` (uniform
  across any stage config, zero new config) vs. columns by `stage` (matches
  a pipeline's natural order but needs per-stage-config column ordering).
  Recommend status-based columns as the default — it's free and stage-config-agnostic.

**Verdict:** natural extension, and the cheapest of the three. Ship first.

---

## 3. Dependency tree/graph view

**What it needs, mapped to what exists:**

The DAG already exists as `artifact_dependencies` and is already exposed via
`surface store graph <artifact_id>`. A tree/graph view is a visual
projection of that same query — turn the graph into `graph TD` Mermaid
syntax (or feed a JS graph layout lib) and render it, color-coding nodes by
`status` (stale = amber, approved = green, failed = red, locked = padlock
badge). This reuses the Mermaid renderer built for #1.

**Pros**
- Zero new store primitives: the DAG and stale-propagation logic already
  exist and are already tested (§53 requires DAG-cycle-rejection and stale-
  cascade unit tests). This is purely a visualization of data the store
  already computes correctly.
- Makes staleness *legible*. Right now stale propagation is a badge on a
  card the user has to notice per-artifact; a graph view showing the whole
  cascade at once (e.g., "editing the script turned these 6 downstream
  nodes amber") is a much stronger mental model, and ties directly to the
  spec's own worked example in §47 (script edit → stale shots/keyframes/clips).
- Shares infrastructure with #1 once the Mermaid embed exists.

**Cons / risks**
- Bigger UI-complexity jump than kanban. Mermaid's own auto-layout is fine
  for tens of nodes but the perf target in §52 explicitly plans for
  "stale cascade over hundreds of nodes <500ms" — a naive full-graph render
  at that scale will be visually unreadable even if computation is fast.
  Needs a scoping mechanism from day one (e.g., render the local neighborhood
  of a selected artifact via `store graph <id>`, not the whole project DAG,
  matching how the CLI command already scopes it).
- Slightly more renderer logic than kanban: mapping DAG edges + per-node
  status into Mermaid syntax, vs. kanban's flat "group by status" query.

**Verdict:** natural extension, real payoff (makes staleness visible), but
scope it per-artifact-neighborhood rather than whole-project from the start.

---

## Cross-cutting take

All three stay inside the architecture's own guardrails:
- No new event types, no schema change, no change to the worker/transport
  contract — everything here is renderer-side, per Principle 3/9.
- None of them push the agent toward generating bespoke UI per project,
  which is the thing the spec explicitly guards against (§7, §46) — these
  ship as reusable renderer components + (for kanban) possibly one new
  stage-config-agnostic view mode, not one-off pages.
- The only genuinely new technical risk is the Mermaid/JS embed inside
  Gradio, and it's shared by two of the three proposals, so it only needs
  to be solved once.

**Recommended order, cheapest/most orthogonal first:**

1. **Kanban/todo view** — no new rendering tech, fixes a named gap
   ("Queued Actions Are Invisible"), ships as a second board view mode.
2. **Markdown + Mermaid renderer with a version diff** — introduces the
   Mermaid embed, tested against the existing Gradio 5.x pin; diff computed
   server-side from two version rows (never `localStorage`).
3. **Dependency tree/graph view** — reuses the Mermaid embed from (2),
   scoped to an artifact's local neighborhood (reusing `store graph`)
   rather than the whole project, with status-based node coloring.

This mirrors the project's own phasing philosophy (prove the cheap, load-
bearing thing first; generalize only once it's proven) rather than building
all three as one redesign.
