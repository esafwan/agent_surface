# Phase 3 — Optional (SPEC section 58)

Not implemented in this session, deliberately. SPEC section 58 explicitly lists Phase 3
as optional and states "None may become v1 core assumptions":

- stdlib/non-Gradio renderer
- MCP Apps renderer
- A2UI renderer
- TUI
- hosted/shared board service
- multi-user collaboration
- cross-machine worker scheduling
- richer notifications
- preset marketplace

None of these are required for the architecture to be considered complete per SPEC
section 55's acceptance bar, which Phase 0 already satisfies, hardened further by
Phase 1/2 (see `STATE.md` in this directory). Revisit only if a concrete need for one of
these emerges — SPEC's own guidance is not to build them speculatively.
