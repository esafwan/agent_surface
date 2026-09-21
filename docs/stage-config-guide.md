# Stage Config Authoring Guide

This guide teaches you how to write custom stage configurations for Agent Surface Board workflows. A stage config is a JSON file that defines the multi-stage pipeline, artifacts, dependencies, allowed user actions, generation defaults, and completion rules for a persistent board.

**See also:** [SPEC.md sections 29–31, 33, 46](./SPEC.md) for the architectural rationale and design principles.

---

## 1. What Is a Stage Config?

A stage config is a JSON document that describes a workflow pipeline. It tells the board:

- What **stages** (work steps) exist and in what order
- What **artifact type** each stage produces (text, image, video, form, etc.)
- Which **actions** users can take on each stage (approve, revise, regenerate, etc.)
- How stages **depend** on each other (e.g., shots depend on script)
- Whether each stage requires **approval** before completion
- **Generation defaults** for async jobs (provider, parallelism)
- **Budget limits** and confirmation thresholds
- **Completion rules** (which stages must be approved to finish)

### When to Write a Custom Stage Config

**Use a shipped preset when your workflow matches one of these:**
- `movie.json` — Multi-stage media pipeline: script → shots → keyframes → clips → assembly
- `questionnaire.json` — Structured questions/forms with JSON Schema
- `plan_review.json` — Draft with sections, risks, and final approval
- `document_review.json` — Document sections with generated findings
- `diff_review.json` — Code/file diff approval
- `generic_media_pipeline.json` — Prompts → images → videos
- `task_board.json` — Agent-decomposed work: read-only progress tracking with status columns

**Create a custom stage config only when:**
- Your pipeline has a different structure or stage order
- You need a unique set of artifact types or actions per stage
- You have domain-specific dependencies or approval rules
- Budget or completion rules are project-specific

**Per SPEC section 46: prefer presets and create stage config only when needed.** Avoid writing custom configs for trivial interactions—keep those in chat.

---

## 2. JSON Schema: Fields and Validation Rules

All claims below are verified against `surface/stages/config.py`. This is the **actual** validation code, not prose.

### 2.1 Top-Level Fields

```json
{
  "schema_version": "1",
  "id": "my_workflow",
  "title": "My Workflow Title",
  "stages": [ /* array of stage objects */ ],
  "completion": { /* completion rules */ },
  "budget": { /* optional cost guardrails */ }
}
```

**Required Top-Level Fields:**

| Field | Type | Notes |
|-------|------|-------|
| `id` | string (non-empty) | Unique identifier for this config. MUST NOT be empty. Used to load presets: `load_preset("movie")` looks for `movie.json`. |
| `stages` | array (min 1 item) | At least one stage MUST be defined. Each stage is a dict with required `id` and optional fields. |

**Optional Top-Level Fields:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `schema_version` | string | `"1"` | Config format version. Currently always `"1"`. |
| `title` | string | `""` | Human-readable name shown on the board. |
| `completion` | object | `{}` | Rules for marking the pipeline complete. |
| `budget` | object | none | Cost guardrails for generation. |

---

### 2.2 Stage Fields (Inside `stages` Array)

Each object in the `stages` array MUST have an `id` and MAY have:

```json
{
  "id": "script",
  "artifact_type": "text",
  "depends_on": ["screenplay"],
  "allowed_actions": ["edit", "revise", "approve"],
  "approval_required": true,
  "board_view": "columns",
  "generation": { "provider": "...", "max_parallel": 2 },
  "form_schema": { /* JSON Schema object */ }
}
```

**Required Stage Fields:**

| Field | Type | Notes |
|-------|------|-------|
| `id` | string (non-empty) | Unique within this config. No duplicates allowed. MUST be a non-empty string. |

**Optional Stage Fields:**

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `artifact_type` | string (enum) | `"text"` | Valid values: `"text"`, `"image"`, `"video"`, `"audio"`, `"form"`, `"file"`, `"diff"`, `"task"`. Determines the board renderer and version content type. |
| `depends_on` | array of strings | `[]` | List of stage IDs this stage depends on. Each ID MUST reference an existing stage. No cycles allowed. |
| `allowed_actions` | array of strings | `[]` | User-triggerable actions. Valid values below. |
| `approval_required` | boolean | `false` | If `true`, this stage MUST be approved before board completion. |
| `board_view` | string | none | Optional board rendering mode. Valid values: `"columns"`. When set to `"columns"`, artifacts are rendered grouped by status in columns instead of a flat list. |
| `generation` | object | none | Configuration for async job generation. Only relevant if stage has `regenerate` or `bulk_regenerate` actions. |
| `form_schema` | object | none | JSON Schema defining a form for artifact type `"form"`. Only used if `artifact_type` is `"form"`. |

---

### 2.3 Valid Artifact Types

Defined in `config.py`: `VALID_ARTIFACT_TYPES = {"text", "image", "video", "audio", "form", "file", "diff", "task"}`

| Type | Use Case | Example |
|------|----------|---------|
| `text` | Plain text, markdown, code | Scripts, instructions, plans, diffs as text |
| `image` | Static images | Keyframes, thumbnails, concept art |
| `video` | Video files | Clips, sequences, animations |
| `audio` | Audio files | Voice-overs, music, sound effects |
| `form` | Structured input | Questionnaires, configuration forms (requires `form_schema`) |
| `file` | Generic files | Documents, archives, exports |
| `diff` | Code/file diffs | Git diffs, change sets |
| `task` | Progress-tracked work items | Decomposed sub-tasks with status tracking (typically used with `board_view: "columns"`) |

---

### 2.4 Valid Actions

Defined in `config.py`: `VALID_ACTIONS = {"create", "revise", "regenerate", "select_version", "edit", "approve", "reopen", "cancel", "lock", "unlock", "bulk_regenerate", "message"}`

| Action | Purpose | Notes |
|--------|---------|-------|
| `create` | Create a new artifact | Worker-driven; used internally. |
| `edit` | Directly edit artifact content | Synchronous; creates new version. |
| `revise` | Request a revision with notes | Worker-driven; creates async event. |
| `regenerate` | Regenerate from scratch | Starts a `generation` job. |
| `bulk_regenerate` | Regenerate multiple artifacts | For batch operations. |
| `select_version` | Choose among existing versions | Synchronous; marks dependents stale. |
| `approve` | Approve for completion | May be deterministic (no worker needed). |
| `reopen` | Revert from approved state | Re-opens for revision. |
| `cancel` | Cancel a pending job or regeneration | May be deterministic. |
| `lock` | Prevent auto-regeneration | Artifact flagged but not auto-updated on staleness. |
| `unlock` | Remove lock | Allow regeneration. |
| `message` | Free-form message to worker | Always worker-driven; used for context. |

---

### 2.5 Generation Block

If a stage has `regenerate` or `bulk_regenerate` actions, you MAY include a `generation` dict:

```json
"generation": {
  "provider": "image_default",
  "max_parallel": 4
}
```

The `generation` object is validated to be a dict but its contents (keys like `provider`, `max_parallel`) are NOT validated by config.py. They are passed to the generation/job system as-is. The code comment indicates these are "generation defaults" used by the poller and worker.

**Keys commonly used (not enforced):**
- `provider`: Which generation service (e.g., `"image_default"`, `"video_default"`, `"analysis"`).
- `max_parallel`: Max concurrent jobs for this stage.

---

### 2.6 Depends-On and Dependency Cycles

**Rules:**
- Each entry in `depends_on` MUST reference an existing stage ID (validation error if not).
- A stage CANNOT depend on itself (caught as `DependencyCycleError`).
- The entire dependency graph MUST be acyclic (DAG). Circular dependencies are rejected with `DependencyCycleError`.

**Example of a cycle (INVALID):**
```json
{
  "id": "pipeline",
  "stages": [
    { "id": "A", "depends_on": ["B"] },
    { "id": "B", "depends_on": ["C"] },
    { "id": "C", "depends_on": ["A"] }  // A -> B -> C -> A cycle
  ]
}
```

**Error message:** `Dependency cycle detected: A -> B -> C -> A`

**Multiple dependencies are allowed (valid):**
```json
{ "id": "final", "depends_on": ["sections", "risks"] }
```

---

### 2.7 Completion Block

```json
"completion": {
  "require_approved_stages": ["script", "shots", "keyframes", "clips", "assembly"]
}
```

**Fields:**
- `require_approved_stages` (array of strings): Stage IDs that MUST be in `approved` state for the pipeline to be complete. Each ID must reference an existing stage. Empty list defaults to requiring all stages.

**Validation:** Each stage ID in `require_approved_stages` is checked to exist. If a stage is missing, error: `Completion rule references unknown stage '<id>'`

---

### 2.8 Budget Block

```json
"budget": {
  "stage_usd": 20.0,
  "project_usd": 100.0,
  "confirm_above_usd": 5.0
}
```

**Fields (all optional):**
- `stage_usd` (non-negative number): Max cost per stage. Exceeding this blocks regeneration.
- `project_usd` (non-negative number): Max cumulative cost for entire project.
- `confirm_above_usd` (non-negative number): Estimated cost above this triggers user confirmation.

**Validation:** Each field, if present, must be a non-negative int or float.

**Cost enforcement:** The `validate_budget_limit()` method returns:
```python
{
  "exceeds_project_budget": bool,
  "exceeds_stage_budget": bool,
  "requires_confirmation": bool
}
```

---

### 2.9 JSON Schema Forms (for artifact_type="form")

If `artifact_type` is `"form"`, include `form_schema`:

```json
{
  "id": "questions",
  "artifact_type": "form",
  "allowed_actions": ["edit", "approve"],
  "form_schema": {
    "type": "object",
    "required": ["email"],
    "properties": {
      "email": { "type": "string", "format": "email" },
      "choice": {
        "type": "string",
        "enum": ["Option A", "Option B", "Option C"]
      }
    }
  }
}
```

**Validation (config.py):**
- `form_schema` must be a dict.
- If `type` is present in `form_schema`, it MUST be `"object"` (not validated deeply; only top-level type checked).

**Standard:** Uses JSON Schema Draft 7 (or later). The board/renderer maps schema properties to native form controls.

---

## 3. Minimal Worked Example: Two-Stage Config

Let's build a simple config from scratch. Suppose you need to approve a document before it can be published.

### Step 1: Define Top Level

```json
{
  "schema_version": "1",
  "id": "doc_approval",
  "title": "Document Approval",
  "stages": []
}
```

### Step 2: Add First Stage (No Dependencies)

```json
{
  "schema_version": "1",
  "id": "doc_approval",
  "title": "Document Approval",
  "stages": [
    {
      "id": "draft",
      "artifact_type": "text",
      "allowed_actions": ["edit", "revise"],
      "approval_required": false
    }
  ]
}
```

- **`id`:** `"draft"` — unique stage identifier.
- **`artifact_type`:** `"text"` — stores text content.
- **`allowed_actions`:** Users can edit directly or request revisions (worker-driven).
- **`approval_required`:** `false` — this stage doesn't block completion.
- **No `depends_on`:** This is the first stage.

### Step 3: Add Second Stage (Depends on First)

```json
{
  "schema_version": "1",
  "id": "doc_approval",
  "title": "Document Approval",
  "stages": [
    {
      "id": "draft",
      "artifact_type": "text",
      "allowed_actions": ["edit", "revise"],
      "approval_required": false
    },
    {
      "id": "published",
      "artifact_type": "text",
      "depends_on": ["draft"],
      "allowed_actions": ["approve", "reopen"],
      "approval_required": true
    }
  ]
}
```

- **`depends_on: ["draft"]`:** Cannot create a published version without a draft.
- **`allowed_actions`:** Only approve/reopen; no editing here (users work in draft first).
- **`approval_required: true`:** This stage must be approved for completion.

### Step 4: Add Completion Rule

```json
{
  "schema_version": "1",
  "id": "doc_approval",
  "title": "Document Approval",
  "stages": [
    {
      "id": "draft",
      "artifact_type": "text",
      "allowed_actions": ["edit", "revise"],
      "approval_required": false
    },
    {
      "id": "published",
      "artifact_type": "text",
      "depends_on": ["draft"],
      "allowed_actions": ["approve", "reopen"],
      "approval_required": true
    }
  ],
  "completion": {
    "require_approved_stages": ["published"]
  ]
}
```

**Interpretation:** The pipeline is complete once the `published` stage is approved. The `draft` stage does not block completion (even though it exists), because it is not in `require_approved_stages`.

---

## 4. Common Mistakes and Validation Errors

This section lists real errors from `config.py` that you will encounter.

### 4.1 Missing Required Top-Level `id`

**Code:**
```json
{
  "stages": [
    { "id": "work", "artifact_type": "text", "allowed_actions": ["edit"] }
  ]
}
```

**Error:**
```
StageValidationError: StageConfig must have a non-empty 'id'
```

**Fix:** Add a non-empty `id` field at the top level.

---

### 4.2 No Stages

**Code:**
```json
{
  "id": "empty",
  "stages": []
}
```

**Error:**
```
StageValidationError: StageConfig must contain at least one stage
```

**Fix:** Add at least one stage object to the `stages` array.

---

### 4.3 Stage Missing `id` or `id` is Empty

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "artifact_type": "text" }
  ]
}
```

**Error:**
```
StageValidationError: Stage must have a non-empty string 'id'
```

**Fix:** Every stage must have a non-empty `id` field.

---

### 4.4 Duplicate Stage IDs

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "id": "work", "artifact_type": "text", "allowed_actions": ["edit"] },
    { "id": "work", "artifact_type": "image", "allowed_actions": ["regenerate"] }
  ]
}
```

**Error:**
```
StageValidationError: Duplicate stage id: 'work'
```

**Fix:** Every stage ID must be unique within the config.

---

### 4.5 Invalid Artifact Type

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "id": "work", "artifact_type": "hologram", "allowed_actions": ["edit"] }
  ]
}
```

**Error:**
```
StageValidationError: Stage 'work' has invalid artifact_type: 'hologram'
```

**Valid types:** `text`, `image`, `video`, `audio`, `form`, `file`, `diff`, `task`

---

### 4.6 Reference to Nonexistent Stage in `depends_on`

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "id": "A", "artifact_type": "text", "depends_on": ["B"], "allowed_actions": ["edit"] }
  ]
}
```

**Error:**
```
StageValidationError: Stage 'A' depends on unknown stage 'B'
```

**Fix:** All stage IDs in `depends_on` must exist in the same config.

---

### 4.7 Self-Dependency

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "id": "A", "depends_on": ["A"], "artifact_type": "text", "allowed_actions": ["edit"] }
  ]
}
```

**Error:**
```
DependencyCycleError: Stage 'A' cannot depend on itself
```

**Fix:** Remove the stage ID from its own `depends_on` list.

---

### 4.8 Circular Dependency

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "id": "A", "depends_on": ["B"], "artifact_type": "text", "allowed_actions": ["edit"] },
    { "id": "B", "depends_on": ["C"], "artifact_type": "text", "allowed_actions": ["edit"] },
    { "id": "C", "depends_on": ["A"], "artifact_type": "text", "allowed_actions": ["edit"] }
  ]
}
```

**Error:**
```
DependencyCycleError: Dependency cycle detected: A -> B -> C -> A
```

**Fix:** Restructure your stages so the dependency graph is acyclic (DAG). Only forward dependencies are allowed.

---

### 4.9 Invalid Action

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "id": "work", "artifact_type": "text", "allowed_actions": ["fly", "transform"] }
  ]
}
```

**Error:**
```
StageValidationError: Stage 'work' specifies unknown action 'fly'
```

**Valid actions:** `create`, `revise`, `regenerate`, `select_version`, `edit`, `approve`, `reopen`, `cancel`, `lock`, `unlock`, `bulk_regenerate`, `message`

---

### 4.10 Completion Rule References Nonexistent Stage

**Code:**
```json
{
  "id": "test",
  "stages": [
    { "id": "work", "artifact_type": "text", "allowed_actions": ["edit"] }
  ],
  "completion": {
    "require_approved_stages": ["work", "publish"]
  }
}
```

**Error:**
```
StageValidationError: Completion rule references unknown stage 'publish'
```

**Fix:** All stage IDs in `completion.require_approved_stages` must exist.

---

### 4.11 Invalid Budget Value

**Code:**
```json
{
  "id": "test",
  "budget": {
    "project_usd": "fifty"
  },
  "stages": [
    { "id": "work", "artifact_type": "text", "allowed_actions": ["edit"] }
  ]
}
```

**Error:**
```
StageValidationError: budget field 'project_usd' must be a non-negative number
```

**Fix:** Budget fields must be numbers (int or float) and non-negative. Strings are not allowed.

---

### 4.12 Negative Budget

**Code:**
```json
{
  "id": "test",
  "budget": {
    "project_usd": -10.0
  },
  "stages": [
    { "id": "work", "artifact_type": "text", "allowed_actions": ["edit"] }
  ]
}
```

**Error:**
```
StageValidationError: budget field 'project_usd' must be a non-negative number
```

**Fix:** All budget fields must be >= 0.

---

### 4.13 Non-Dict `generation` or `form_schema`

**Code:**
```json
{
  "id": "test",
  "stages": [
    {
      "id": "work",
      "artifact_type": "text",
      "allowed_actions": ["regenerate"],
      "generation": "image_provider"
    }
  ]
}
```

**Error:**
```
StageValidationError: Stage 'work' generation must be a dict
```

**Fix:** `generation` and `form_schema` must be JSON objects (dicts), not strings or other types.

---

## 5. Testing a Custom Stage Config

Before shipping, validate your config using the patterns from `tests/test_stages.py`.

### 5.1 Load and Basic Validation

```python
from surface.stages.config import load_stage_config, StageValidationError
import json

# Load from file
try:
    config = load_stage_config("my_workflow.json")
    print(f"✓ Config '{config.id}' loaded successfully")
except StageValidationError as e:
    print(f"✗ Validation error: {e}")
    exit(1)
```

### 5.2 Assert Expected Stages

```python
expected_stage_ids = ["stage1", "stage2", "stage3"]
assert config.stage_order == expected_stage_ids, \
    f"Expected stages {expected_stage_ids}, got {config.stage_order}"
print("✓ Stages in expected order")
```

### 5.3 Verify Dependency Structure

```python
# Check a specific dependency
assert config.get_stage("stage2").depends_on == ["stage1"]
print("✓ Dependencies correct")

# Verify no cycles (this is done automatically in __init__, but you can check)
try:
    config._validate_dag()
    print("✓ DAG is acyclic")
except Exception as e:
    print(f"✗ Cycle detected: {e}")
    exit(1)
```

### 5.4 Check Allowed Actions

```python
# Verify an action is permitted
try:
    config.validate_action("stage1", "edit")
    print("✓ 'edit' allowed on stage1")
except ActionPermissionError as e:
    print(f"✗ Action not allowed: {e}")
    exit(1)

# Verify a disallowed action is rejected
try:
    config.validate_action("stage1", "regenerate")
    print("✗ Should have rejected 'regenerate'")
    exit(1)
except ActionPermissionError:
    print("✓ 'regenerate' correctly rejected on stage1")
```

### 5.5 Verify Completion Rules

```python
# Check if completion is satisfied when certain stages are approved
approved_stages = {"stage1", "stage2", "stage3"}
is_complete = config.is_completed(approved_stages)
assert is_complete, "Expected config to be complete with all stages approved"
print("✓ Completion rules work")
```

### 5.6 Check Budget Parsing (if present)

```python
if config.budget:
    result = config.validate_budget_limit(
        estimated_cost=5.0,
        current_project_cost=10.0,
        current_stage_cost=2.0
    )
    assert isinstance(result, dict)
    assert "exceeds_project_budget" in result
    print("✓ Budget validation works")
else:
    print("  (No budget block; skipped)")
```

### 5.7 Minimal Test Script

Save this to `test_my_config.py`:

```python
#!/usr/bin/env python3
import sys
from surface.stages.config import load_stage_config, StageValidationError

config_path = sys.argv[1] if len(sys.argv) > 1 else "my_config.json"

try:
    config = load_stage_config(config_path)
    print(f"✓ Config ID: {config.id}")
    print(f"✓ Stages: {', '.join(config.stage_order)}")
    
    for stage_id in config.stage_order:
        stage = config.get_stage(stage_id)
        print(f"  - {stage_id}")
        print(f"      artifact_type: {stage.artifact_type}")
        print(f"      depends_on: {stage.depends_on}")
        print(f"      allowed_actions: {stage.allowed_actions}")
        print(f"      approval_required: {stage.approval_required}")
    
    print(f"✓ Completion rule: {config.completion}")
    print("✓ All validations passed!")
    
except StageValidationError as e:
    print(f"✗ Validation error: {e}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"✗ Error: {e}", file=sys.stderr)
    sys.exit(1)
```

Run it:
```bash
python test_my_config.py my_config.json
```

---

## 6. When NOT to Write a Custom Stage Config

Per SPEC section 3 (Non-Goals) and section 46:

**Keep interactions in chat if:**
- The workflow is a simple back-and-forth (e.g., "write → review → done").
- There are fewer than 3 related decisions or stages.
- No version history, parallel work, or long-running jobs are needed.
- The work is truly one-off and doesn't need persistence.

**Do NOT create a board (and thus do not need a config) for:**
- Simple questions that block and wait for an answer. Use a normal tool call.
- Trivial confirmations. Keep them in chat.
- Single-artifact workflows. Use the store directly if at all.
- Workflows with only synchronous, instant operations (no generation jobs).

**Board is appropriate when:**
- Multiple related artifacts need parallel review (e.g., scenes, clips, sections).
- Non-linear work: users approve items in any order, not linearly.
- Long-running generation: jobs may take minutes; worker should not block.
- Version history matters: multiple attempts, picking the best version.
- Complex dependencies: changing one artifact marks others stale.
- Persistent state: the board outlives the worker session.

---

## 7. Reference: Real Shipped Presets

Here are the shipped presets you can reference or extend:

### `movie.json` (5 stages, media pipeline)
```
script → shots → keyframes → clips → assembly
```
- **Script & Shots:** Text, user edits.
- **Keyframes:** Images, generated in parallel (max 4).
- **Clips:** Videos, generated (max 2).
- **Assembly:** Final video.
- **Completion rule:** All stages must be approved.

### `questionnaire.json` (1 stage, forms)
```
questions (form artifact with JSON Schema)
```
- Single stage with structured form input.
- Useful for bounded Q&A workflows.

### `plan_review.json` (4 stages, approval chain)
```
draft → sections → risks → final
```
- `final` depends on both `sections` and `risks`.
- Example of multiple upstream dependencies.

### `document_review.json` (2 stages, analysis + review)
```
sections → findings
```
- Sections edited by user; findings generated (async job).
- Shows how generation is tied to a stage.

### `diff_review.json` (1 stage, minimal)
```
changes (diff artifact)
```
- Simplest config: one stage, three actions (approve, reopen, message).

### `generic_media_pipeline.json` (3 stages, flexible)
```
prompts → images → videos
```
- Like movie but simpler; good starting point for custom media workflows.

### `task_board.json` (1 stage, read-only progress tracking)
```
tasks (task artifacts, status-column layout)
```
- **Single stage for progress tracking:** Displays task artifacts grouped by status in columns (Planned, In Progress, Needs Approval, Done, Failed, Cancelled).
- **Read-only for end-users:** Only `approve` and `reopen` actions are allowed. Workers create and update tasks via the store API using task artifacts.
- **Perfect for agent decomposition:** When a parent agent breaks a task into sub-tasks (or hands work to sub-agents), the task board shows the decomposition and its live status. The user can approve tasks in the "Needs Approval" column; no edit/revise/message controls appear.
- **No dependencies:** This is a standalone, single-stage preset. Task artifacts carry their own `status` field, which determines their column placement.

---

## 8. FAQ

**Q: Can I have a stage that depends on multiple stages?**

A: Yes. Set `"depends_on": ["stage1", "stage2", "stage3"]`. Each stage ID must exist. Cycles still are forbidden.

**Q: Can I use external references or IDs that don't appear in my config?**

A: No. Every stage ID in `depends_on`, `completion.require_approved_stages`, and everywhere else must be defined in the `stages` array.

**Q: What happens if I don't set `completion.require_approved_stages`?**

A: The default (line 188 in config.py): `req_stages = set(self.stages.keys())` — all stages must be approved.

**Q: Can I add custom fields to stages or the top-level?**

A: Yes. The `Stage` object stores all data in `self.raw`. Custom fields are preserved but not validated. Use them for documentation or tool-specific extensions (but mark them clearly to avoid collisions).

**Q: Is `approval_required` used by the board or just for documentation?**

A: It's stored but not enforced by config.py itself. The board/store uses it to enforce approval before allowing downstream operations. It's semantically important.

**Q: Can I have zero actions on a stage?**

A: Yes, technically `"allowed_actions": []` is valid. But such a stage is useless on the board—users can't do anything. Avoid it.

**Q: Where should I store my custom config file?**

A: By convention, custom configs live alongside presets in `surface/stages/`. To use with `load_preset("myconfig")`, name it `myconfig.json`. Otherwise, use `load_stage_config("/path/to/myconfig.json")` with the full path.

**Q: Is JSON the only supported format?**

A: Yes, currently. The code uses `json.load()`. No YAML, TOML, or other formats.

**Q: Can I use comments in JSON?**

A: No, JSON does not support comments. Pre-process or use a separate documentation file if you need annotations.

---

## 9. Debugging a Config Error

If you get a validation error, follow this checklist:

1. **Is the JSON syntactically valid?**
   ```bash
   python -m json.tool my_config.json > /dev/null && echo "✓ Valid JSON" || echo "✗ Syntax error"
   ```

2. **Does the top-level have `id` and `stages`?**
   - `id` must be non-empty string.
   - `stages` must be a non-empty array.

3. **Does every stage have a unique non-empty `id`?**
   - No duplicates, no empty strings.

4. **Are all `depends_on` references valid?**
   - Every ID in `depends_on` must be another stage's `id`.
   - No forward references to stages you haven't defined yet.

5. **Are `allowed_actions` all valid?**
   - Check against the list: `create`, `revise`, `regenerate`, `select_version`, `edit`, `approve`, `reopen`, `cancel`, `lock`, `unlock`, `bulk_regenerate`, `message`.

6. **Is `artifact_type` valid?**
   - Must be one of: `text`, `image`, `video`, `audio`, `form`, `file`, `diff`.

7. **Are budget values non-negative numbers?**
   - No strings, no negative values.

8. **Does `completion.require_approved_stages` reference only existing stages?**
   - Every ID must match a stage in the `stages` array.

9. **Run the test script:**
   ```bash
   python test_my_config.py my_config.json
   ```

10. **Load the config programmatically:**
    ```python
    from surface.stages.config import load_stage_config
    config = load_stage_config("my_config.json")
    ```
    If this succeeds, your config is valid.

---

## 10. Example: Extending an Existing Preset

Suppose you want to extend `movie.json` to add a pre-production stage before `script`. Start with the original:

```json
{
  "schema_version": "1",
  "id": "movie_extended",
  "title": "Extended Movie Pipeline",
  "stages": [
    {
      "id": "preproduction",
      "artifact_type": "text",
      "allowed_actions": ["edit", "revise", "approve"],
      "approval_required": true
    },
    {
      "id": "script",
      "artifact_type": "text",
      "depends_on": ["preproduction"],
      "allowed_actions": ["edit", "revise", "approve", "reopen"],
      "approval_required": true
    },
    {
      "id": "shots",
      "artifact_type": "text",
      "depends_on": ["script"],
      "allowed_actions": ["edit", "revise", "approve", "reopen"],
      "approval_required": true
    },
    {
      "id": "keyframes",
      "artifact_type": "image",
      "depends_on": ["shots"],
      "allowed_actions": ["regenerate", "revise", "select_version", "approve", "cancel"],
      "generation": {
        "provider": "image_default",
        "max_parallel": 4
      }
    },
    {
      "id": "clips",
      "artifact_type": "video",
      "depends_on": ["keyframes"],
      "allowed_actions": ["regenerate", "revise", "select_version", "approve", "cancel"],
      "generation": {
        "provider": "video_default",
        "max_parallel": 2
      }
    },
    {
      "id": "assembly",
      "artifact_type": "video",
      "depends_on": ["clips"],
      "allowed_actions": ["regenerate", "approve", "reopen"]
    }
  ],
  "completion": {
    "require_approved_stages": ["preproduction", "script", "shots", "keyframes", "clips", "assembly"]
  }
}
```

**Changes:**
- Added `preproduction` as the first stage (no dependencies).
- Updated `script` to depend on `preproduction`.
- Updated `completion.require_approved_stages` to include `preproduction`.

---

## Summary Checklist

Before shipping your stage config:

- [ ] Top-level has non-empty `id` and non-empty `stages` array.
- [ ] Every stage has a unique non-empty `id`.
- [ ] All `artifact_type` values are valid.
- [ ] All actions in `allowed_actions` are valid.
- [ ] All stage IDs in `depends_on` exist.
- [ ] No cycles in dependencies.
- [ ] All stage IDs in `completion.require_approved_stages` exist.
- [ ] All budget fields are non-negative numbers (if present).
- [ ] If using `form_schema`, it's a dict with `type: "object"` at top level.
- [ ] If using `generation`, it's a dict.
- [ ] Config loads without error using `load_stage_config()`.
- [ ] Test script passes and shows expected stages/actions/dependencies.

---

**For more details on the architecture and reasoning behind stage configs, see [SPEC.md sections 29–31, 33, 46](./SPEC.md).**
