# Phase 3 — Optional (SPEC section 58)

SPEC section 58 lists 9 possible items and explicitly states "None may become v1 core
assumptions." Two are concretely buildable features with a fixed scope; the rest are
infrastructure/ecosystem decisions without a single "done" state. Built the former,
documented why the latter are out of scope for this codebase rather than silently
skipped.

## Built

- **stdlib/non-Gradio renderer** — `surface/tui.py` + `surface tui` CLI command. A
  plain-terminal interactive board using only the Python standard library, reusing
  `board.py`'s existing gradio-independent display/action logic rather than duplicating
  it. Notably: this is the **only** renderer in the whole system whose actual behavior
  has been fully verified in this environment — the Gradio board has never been run here
  (gradio isn't installed), but the TUI has, via both its test suite (19 tests) and a
  real subprocess smoke test.
- **Richer notifications** — `surface/notify.py`. A `NotificationAdapter` interface with
  a dependency-free `LogNotifier` and a stdlib-only `WebhookNotifier`, plus a pure
  `detect_notifications()` function that diffs two project-summary snapshots to surface
  only genuinely new attention-worthy changes (new failures, new stale artifacts,
  pending-review count crossing zero). Not yet wired into `serve()`'s loop to fire
  automatically — that wiring is a small, well-scoped follow-up (periodically diff
  `project summary`-shaped output and call `dispatch_notifications`), described in the
  module's own docstring but not implemented, since it wasn't asked for as a running
  behavior, only as an available capability.

## Deliberately not built — infrastructure/ecosystem decisions, not features

- **Hosted/shared board service** — this is a decision to stand up and operate a service
  (hosting, auth model, multi-tenancy, billing/ops), not a code module. There's no
  "finished" state to implement against; it's a product decision this repo doesn't make
  on its own.
- **Multi-user collaboration** — requires deciding on a concurrency/identity model
  (who can see/edit what, conflict resolution beyond optimistic version-select) that
  SPEC itself defers ("V1 does NOT require multi-user collaborative editing"). Building
  this without a real multi-user requirement to design against would be speculative.
- **Cross-machine worker scheduling** — requires a scheduling/coordination protocol
  across machines (leader election, distributed leases) that the current SQLite-backed
  single-node store deliberately does not provide (SPEC section 10: "SQLite is v1
  durable coordination"). This is a storage-layer redesign, not an additive feature.
- **Preset marketplace** — a distribution/discovery mechanism (hosting, versioning,
  trust model for third-party presets) — again a product/infrastructure decision, not a
  bounded coding task.
- **MCP Apps renderer, A2UI renderer** — both are "potential future" integrations against
  external, evolving standards (SPEC sections 50, 63) that this repo cannot responsibly
  implement against a guessed/unverified API surface — the same reasoning that led Phase
  1's ACP transport adapter to be explicitly labeled "scaffolded, unverified" rather than
  claimed as working. Building against an assumed API for a protocol with no verified
  Python integration available in this environment would repeat that mistake rather than
  avoid it.

None of the above are required for the architecture to be considered complete per SPEC
section 55's acceptance bar, which Phase 0 already satisfied and Phase 1/2 hardened
further (see `STATE.md` in this directory).
