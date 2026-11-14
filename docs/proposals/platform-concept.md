# Agent Surface as an app runtime, skill repository and fluid-app platform

Status: draft v2 (author's analysis + round 1 review folded in)

## 1. The idea

Today Agent Surface is a per-project board: a JSON stage config, a SQLite store of immutable
versions, a supervisor, and a worker backed by the user's own coding agent (codex / opencode /
agy). The proposal is to promote it from "a board an agent can open" to three things at once:

1. **App runtime.** A surface (stage config + worker prompt/skill + assets) is an *app*. The
   runtime installs, launches, lists and updates apps. A built-in "home" surface lists every
   installed app and can open one.
2. **Skill / app repository with discovery.** Apps are shareable bundles. A discovery tab lets
   people browse surfaces others made. The only thing a consumer needs is their own existing
   agent (BYO agent), so the platform hosts no inference.
3. **Fluid apps.** Installing is the start, not the end. Because every change is already an
   immutable version in the store, a user's personalization of an app is a tracked *delta* on
   top of the upstream bundle. They can fork it by talking to their agent, keep upstream
   updates flowing in, and see/undo exactly what they changed.

Plus a **creator loop**: a "surface-builder" skill that is itself a surface app, which creates
new apps (vibe-coding, but the output is an agentic app, not a static site). Capabilities are
bounded by what the host system actually offers (ffmpeg, yt-dlp, image/video providers, the
agent's tools), exposed as a declared capability registry.

Example catalogue: video converter, trimmer, editor, YouTube downloader, video creator,
design creator, illustrator.

## 2. Why the foundation fits unusually well

| Platform need | What the repo already has |
| --- | --- |
| App definition | Stage config JSON (`surface/stages/config.py`), validated; `form_schema`, `board_view` |
| App state / history | Immutable versions, artifacts, dependency DAG, stale propagation (`store.py`) |
| Delta tracking | Versions + `diff.py` + provenance/freshness work (phase 5) |
| BYO agent | `AgentBackend` registry: codex, opencode, agy, fake |
| Long jobs (video!) | Async jobs + Poller + provider adapters (`poller.py`, `providers/`) |
| Crash safety | Lease-based inbox, at-least-once + idempotent events, `recover` |
| Agent-readable method | `SKILL.md` "Planning a New Surface" is already a creator-loop prompt |
| Launch UX | `launcher.py`, `--app-window`, `.command` shortcuts: app-like feel exists |

The key property: **the UI is data**. An app is declarative config plus a prompt, so it is
cheap to share, diff, fork, review and sandbox, unlike shipping arbitrary frontend code.

## 3. Parallels

- **Obsidian / VS Code extensions / Raycast store:** discovery + local-first + user owns the
  data; but their customization is plugin code and settings, not agent-edited.
- **Shortcuts / Zapier / n8n templates:** shareable declarative workflows. Lesson: the long
  tail of templates is mostly unused; quality and curation beat volume.
- **Claude Skills / Custom GPTs / GPT Store:** closest parallel for "skill repository". Lesson:
  the GPT Store's discovery never took off because GPTs were thin prompt wrappers with no
  durable state or real capabilities. Surfaces differ by having state, a real UI, and tools.
- **Glitch / Replit remix, Hypercard, Smalltalk, Notion templates:** "remix culture" and
  malleable software. Lesson: malleability only wins when the *default* is already good and
  edits are cheap and reversible.
- **Ink & Switch "malleable software", Dynamicland:** research thesis that users should reshape
  tools. This is the strongest conceptual match; the delta-over-immutable-versions model is a
  concrete mechanism they lacked.
- **Vibe-coding builders (Lovable, v0, Bolt):** generate whole apps. Lesson: output is static
  code with no runtime agent; maintenance falls on the user. Surfaces keep the agent in the
  loop after creation.
- **Nix / git forks / dotfiles:** delta-on-upstream and three-way merge problems are well known
  and hard. Directly relevant to "fork but keep upstream updates".
- **MCP Apps / A2UI / generative UI:** competing/complementary standards for agent-driven UI.
  SPEC already lists them as future renderers.

## 4. Why it could work

1. BYO agent removes the hosting/inference cost problem; marginal cost to the platform ~0.
2. Declarative apps are safe to share relative to code (no arbitrary JS), reviewable, diffable.
3. Versions-as-foundation makes personalization reversible, which is what makes users brave.
4. The job-shaped UI argument is real: chat is a poor interface for many tasks.
5. Packaging a *method* (SKILL.md) plus a runtime is hard for competitors to copy quickly.
6. Capability-bounded creation (only what the host offers) keeps generated apps working
   instead of hallucinating features.

## 5. Why it might not

1. **Local, single-user, SQLite, localhost-only by design.** Discovery/sharing needs identity,
   hosting, moderation, trust: SPEC explicitly defers these (PHASE3.md "deliberately not built").
2. **Declarative ceiling.** `list/columns/grid` views + forms cover review-style apps. A video
   *editor* needs a timeline, scrubbing, canvas: not expressible. A trimmer is borderline.
   The three views are explicitly "the complete set". The platform thesis needs a richer
   component vocabulary or an escape hatch (custom renderer = code = trust problem).
3. **Agent latency and cost per interaction.** Every `revise` is an LLM round trip. Deterministic
   apps (convert, trim, download) should *not* need an LLM per click. Need a first-class
   "tool action" path (run ffmpeg directly) so the agent is for judgment, not plumbing.
4. **Security of shared apps.** An app whose worker prompt runs in the user's agent with their
   shell access is a prompt-injection / malware vector. "Declarative" does not save you when the
   worker is an agent with a shell. Needs a permission/capability manifest and sandboxing.
5. **Delta/upgrade merge is genuinely hard.** Prompt+config forks diverge; semantic merge of an
   LLM-edited prompt is unsolved. "Keeps track of the delta" is easy; "keeps merging upstream"
   is not.
6. **Fragmented agents.** Behavior differs across codex/opencode/agy; an app that works on one
   may silently degrade on another. Needs conformance tests per app.
7. **Cold-start discovery problem.** A store is worthless until it has good apps; most apps
   will be made by the platform authors for a long time.
8. **Tool commoditisation.** "YouTube downloader" is solved by yt-dlp GUIs. The surface wins
   only where agent judgment adds value (e.g. "trim the boring parts", "make this a 30s ad").
9. **Legal:** YouTube downloading, copyrighted media, provider ToS.
10. **Maintenance:** repo is ~9.5k LOC of Python, a hand-written JS board, many phase logs.
    Scope creep toward a platform could outrun a small team.

## 6. What it should be (recommendation, v1 of the argument)

Worth it, **staged**, and not as a "store" first. Order of value vs. risk:

1. **Bundle format** (`.surface` dir/zip: stage config, worker prompt, capabilities manifest,
   assets, version, upstream id). Local install/list/open. Cheap, unlocks everything.
2. **Home/launcher surface** that lists installed apps (dogfoods the runtime).
3. **Capability registry + tool actions** (ffmpeg, yt-dlp, providers) so deterministic work
   skips the LLM. Ship 3 to 5 flagship media apps to prove the thesis.
4. **Surface-builder skill** (creator loop) that emits a bundle and validates it.
5. **Delta tracking**: store user edits as a patch over the installed bundle version; show
   "what changed"; support reset. Defer automatic upstream merge: offer "re-apply my changes
   with the agent" as a best-effort assisted merge.
6. **Discovery**: begin as a git-repo index (a curated `registry.json` in a public repo,
   install by URL/hash). No hosted service until there is pull.

Do not do: hosted multi-tenant store, ratings, payments, arbitrary-code renderers.

## 7. Open questions for reviewers

- Is the declarative ceiling fatal for the media-app examples, and what is the minimal
  component vocabulary / escape hatch?
- Is there a defensible trust model for third-party prompts running with a user's agent?
- Is "fluid app with tracked delta" a real differentiator or a feature of git?
- Is this the right product for the repo, or should it remain a developer tool?

---

## 8. Review round 1 (Opus 5.5): findings folded in

Verdict: right instincts, but section 2 overstates the foundation. The real foundation is a
validated DAG + versioned-artifact store with provenance + a renderer for *review-style* apps.
That supports "agent-maintained review boards", not yet "an app runtime for media tools".

### Corrections to section 2 (checked against code)

- **Delta tracking does not exist.** Versions/freshness (`store.py`, `schema.py`) track artifact
  *content*. The app definition (stage config JSON) is not stored or versioned in SQLite.
  `diff.py` is an 86-line `difflib` wrapper over two strings.
- **No per-app prompt.** Every agent call uses the generic `build_prompt()`
  (`surface/agents/prompt.py`). Stage configs have no worker-instructions field. "Config + prompt"
  as an app is a new feature.
- **The worker is a one-shot text call** (`agents/runner.py`), codex is `--sandbox read-only`;
  it cannot run tools or return produced files. opencode/agy have no sandbox flags, so the three
  backends are inconsistent today.
- **Providers are mocks only** (`MockImageProvider`, `MockVideoProvider`). No real provider ever ran.
- **No input path.** The board has no upload/ingest and no `create` action. A video converter
  cannot be handed a video. This blocks the flagship examples more fundamentally than the
  declarative ceiling.
- **Risk #3 was understated:** with no agent configured the worker is already deterministic, and
  `regenerate` -> job -> Poller -> `Provider.submit/status/collect` runs without an LLM. The
  `Provider` ABC is the right seam for tool actions (e.g. an ffmpeg provider).
- Size: ~11.5k lines Python + ~1.9k JS/CSS (not 9.5k).

### Risks added

- **Loopback is not a boundary once tools run.** Auth is off by default and `board_web.py` has no
  Origin/Host checks: CSRF or DNS rebinding could POST actions. Harmless for text boards, code
  execution once actions call ffmpeg/yt-dlp.
- **Argument injection** in tool actions (yt-dlp `--exec`): typed, allowlisted params only.
- **Schema evolution:** an app upgrade that renames/removes stages orphans stored artifacts. No
  migration story exists. Harder than merging prompts.
- **Agent capability drift:** the capability manifest must state the sandbox a backend must
  enforce, not just which tools exist.
- **Binary artifacts** make "what changed" a non-text-diff problem.
- **"Declarative = safe" is the weakest claim.** ComfyUI: shareable JSON graphs were safe, but
  custom node code became the malware vector. Same shape here once tools are involved.

### Added prior art

Home Assistant blueprints + HACS (closest match), ComfyUI workflows/custom nodes (the media
counter-example), Kustomize/Helm overlays (patch-over-base for config), Homebrew taps/Flatpak
manifests (git-index registry with declared permissions), Apple Shortcuts consent prompts, Deno
deny-by-default permissions.

### Revised roadmap (supersedes section 6)

0. **Prove one flagship app first**, before any bundle/registry work: a real local tool provider
   (ffmpeg trim), file ingest + `create` action, Origin/Host hardening, then `video_trim` stage
   plus an end-to-end test. If the trimmer cannot be expressed, the platform thesis is moot.
1. Add `worker.instructions` and `capabilities {tools, agent_sandbox}` to the stage config;
   backends refuse stages whose sandbox they cannot enforce.
2. Version the app definition: `configs` table keyed by hash, projects pinned to a hash.
3. Capability/permission manifest (before the builder skill).
4. Bundle format + `surface app list/install` CLI (no home surface until >5 apps).
5. Delta = JSON Patch over pinned upstream config hash + separate prompt overlay; deterministic
   conflicts. LLM-assisted merge is a fallback, not the mechanism.
6. Surface-builder skill emitting validated bundles with declared permissions.
7. Discovery as a curated git `registry.json`, installs pinned by hash. Last.

---

## 9. Final review (Fable): verdict and plan

Spot-check: 6 of 7 section-8 code claims hold. One is half wrong: the app definition **is**
stored (`cli.py:444` writes the full config to `kv_state['stage_config']`, read at `cli.py:520`),
but as one mutable, unversioned row. So "version the config" is a small change to an existing
write path, not a new subsystem.

### Verdict

Not a platform or store. Build it as a **developer tool: a durable, crash-safe review-and-job UI
that a coding agent can stand up for long-running, multi-artifact work.** The real asset is the
versioned DAG, stale propagation, leases and poller. That is what a one-off vibe-coded app lacks.
Keep one ffmpeg provider to prove the `Provider` seam works for real tools, then stop.

### What both earlier drafts missed

- **The real competitor is the user's own agent.** "Ask the agent for a trim script" takes ~60s
  and has no ceiling. Surface wins only where a job has many artifacts, needs review/selection/
  regeneration, and outlives a session. Selection rule for flagship apps: a trimmer fails it; a
  40-shot storyboard-to-video pipeline passes.
- **Fluid/delta solves a problem regeneration removes.** If the agent can re-derive an app from
  "upstream + my preferences note", store intent, not JSON Patch. The real delta problem is
  migrating stored artifacts when stages change.
- **Discovery is redundant.** A bundle is JSON + a prompt; git + URL distributes it. Agents do the
  discovering, so the right form is an agent-readable index, not a human browse tab.
- **BYO agent is not free.** Three backends with no sandbox parity means three untested behaviours
  per shared app. Commit to one tool-capable backend contract.
- **Trimmer is the wrong flagship** (needs scrubbing UI, gains nothing from agent judgment).

### Ordered PR-sized steps

1. Origin/Host allowlist middleware in `board_web.py` + tests (needed regardless of direction).
2. `worker.instructions` in the stage config, threaded into `build_prompt()`.
3. Version the config: hash it, append-only `configs` table, `kv_state` points at a hash.
4. `create` action + file ingest (path reference) with typed parameters.
5. `FfmpegProvider` implementing the `Provider` ABC with allowlisted args and real outputs.
6. Flagship: **batch clip review** (ingest N files, agent proposes cuts, user approves/revises,
   ffmpeg renders) with an end-to-end test.
7. `surface app export/install` for a directory bundle, pinned by hash.
8. SKILL.md extension so an agent emits a validated bundle.

First milestone = steps 1-6.

### Falsifiable success test

Three non-authors each run a real 10-clip job, compared against a bare coding agent on the same
task. Pass if Surface needs fewer human interventions, survives `kill -9` mid-run with no rework,
and at least two of three choose it again unprompted.

### Kill criteria

- Flagship loses to "ask the agent for a script" on time-to-done for multi-artifact jobs.
- No non-author creates a second surface within ~a month of the milestone.
- The flagship's UI needs a custom-code renderer (declarative premise collapses).
- Host agents ship native durable review UIs (MCP Apps / stateful artifact boards) covering the
  DAG + versioning case.
