"""
Board UI module for Agent Surface.

Core logic is in pure Python classes/functions that don't depend on gradio.
Gradio-specific wiring is in separate section at end.
"""
import json
import hashlib
from typing import Any, Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime

from surface.store import Store
from surface.stages.config import StageConfig, ActionPermissionError


# =============================================================================
# Pure Logic: Display Model Construction (no gradio dependency)
# =============================================================================


@dataclass
class VersionDisplayRow:
    """Represents a single version in the version history."""
    version_id: str
    version_num: int
    content_type: Optional[str] = None
    content: Optional[str] = None
    content_ref: Optional[str] = None
    prompt: Optional[str] = None
    note: Optional[str] = None
    created_by: str = "worker"
    created_at: str = ""
    is_selected: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version_id": self.version_id,
            "version_num": self.version_num,
            "content_type": self.content_type,
            "content": self.content,
            "content_ref": self.content_ref,
            "prompt": self.prompt,
            "note": self.note,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "is_selected": self.is_selected,
        }


@dataclass
class ArtifactDisplayCard:
    """Represents an artifact and its selected version for display."""
    artifact_id: str
    stage: str
    title: str
    status: str
    locked: bool
    selected_version: Optional[VersionDisplayRow] = None
    all_versions: List[VersionDisplayRow] = field(default_factory=list)
    allowed_actions: List[str] = field(default_factory=list)
    approval_required: bool = False
    has_generating_job: bool = False
    stale_reason: Optional[str] = None
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "stage": self.stage,
            "title": self.title,
            "status": self.status,
            "locked": self.locked,
            "selected_version": self.selected_version.to_dict() if self.selected_version else None,
            "all_versions": [v.to_dict() for v in self.all_versions],
            "allowed_actions": self.allowed_actions,
            "approval_required": self.approval_required,
            "has_generating_job": self.has_generating_job,
            "stale_reason": self.stale_reason,
            "updated_at": self.updated_at,
        }


@dataclass
class StageDisplayGroup:
    """Represents a stage and its artifacts."""
    stage_id: str
    stage_title: str
    artifact_type: str
    artifacts: List[ArtifactDisplayCard] = field(default_factory=list)
    approval_required: bool = False
    approved_count: int = 0
    total_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage_id": self.stage_id,
            "stage_title": self.stage_title,
            "artifact_type": self.artifact_type,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "approval_required": self.approval_required,
            "approved_count": self.approved_count,
            "total_count": self.total_count,
        }


@dataclass
class BoardDisplayModel:
    """Complete board display state."""
    stages: List[StageDisplayGroup] = field(default_factory=list)
    pending_review_count: int = 0
    total_pending_events: int = 0
    generating_artifacts: Set[str] = field(default_factory=set)
    stale_artifacts: Set[str] = field(default_factory=set)
    failed_artifacts: Set[str] = field(default_factory=set)
    locked_artifacts: Set[str] = field(default_factory=set)
    status_message: str = "Ready"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stages": [s.to_dict() for s in self.stages],
            "pending_review_count": self.pending_review_count,
            "total_pending_events": self.total_pending_events,
            "generating_artifacts": list(self.generating_artifacts),
            "stale_artifacts": list(self.stale_artifacts),
            "failed_artifacts": list(self.failed_artifacts),
            "locked_artifacts": list(self.locked_artifacts),
            "status_message": self.status_message,
        }


class DisplayModelBuilder:
    """Builds display models from store state."""

    def __init__(self, store: Store, stage_config: StageConfig):
        self.store = store
        self.stage_config = stage_config

    def build_board_display(self) -> BoardDisplayModel:
        """Build complete board display model."""
        display_model = BoardDisplayModel()

        # Count pending events using store method
        display_model.total_pending_events = self._count_pending_events()

        # Collect all artifacts by stage
        for stage_id in self.stage_config.stage_order:
            stage_obj = self.stage_config.get_stage(stage_id)
            stage_group = self._build_stage_group(stage_id, stage_obj)
            display_model.stages.append(stage_group)

            # Track counts
            for artifact_card in stage_group.artifacts:
                if artifact_card.status == "review":
                    display_model.pending_review_count += 1
                if artifact_card.status == "generating":
                    display_model.generating_artifacts.add(artifact_card.artifact_id)
                if artifact_card.status == "stale":
                    display_model.stale_artifacts.add(artifact_card.artifact_id)
                if artifact_card.status == "failed":
                    display_model.failed_artifacts.add(artifact_card.artifact_id)
                if artifact_card.locked:
                    display_model.locked_artifacts.add(artifact_card.artifact_id)

        return display_model

    def _build_stage_group(self, stage_id: str, stage_obj: Any) -> StageDisplayGroup:
        """Build display group for a single stage."""
        stage_group = StageDisplayGroup(
            stage_id=stage_id,
            stage_title=stage_id.title(),  # Simple title case
            artifact_type=stage_obj.artifact_type,
            approval_required=stage_obj.approval_required,
        )

        artifacts = self.store.list_artifacts(stage=stage_id)
        stage_group.total_count = len(artifacts)

        for artifact in artifacts:
            card = self._build_artifact_card(artifact, stage_obj)
            stage_group.artifacts.append(card)

            if artifact["status"] == "approved":
                stage_group.approved_count += 1

        return stage_group

    def _count_pending_events(self) -> int:
        """Count pending events in the store."""
        # Use event querying: list all events and filter
        cursor = self.store.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events WHERE status = 'pending'")
        return cursor.fetchone()[0]

    def _has_generating_jobs(self, artifact_id: str) -> bool:
        """Check if artifact has any generating (queued/running) jobs."""
        cursor = self.store.conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM jobs WHERE artifact_id = ? AND status IN ('queued', 'running')",
            (artifact_id,),
        )
        return cursor.fetchone()[0] > 0

    def _build_artifact_card(
        self, artifact: Dict[str, Any], stage_obj: Any
    ) -> ArtifactDisplayCard:
        """Build display card for a single artifact."""
        artifact_id = artifact["id"]
        all_versions = self.store.list_versions(artifact_id)
        selected_version_row = None

        if artifact["selected_version_id"]:
            sel_ver = self.store.get_version(artifact["selected_version_id"])
            if sel_ver:
                selected_version_row = VersionDisplayRow(
                    version_id=sel_ver["id"],
                    version_num=sel_ver["n"],
                    content_type=sel_ver.get("content_type"),
                    content=sel_ver.get("content"),
                    content_ref=sel_ver.get("content_ref"),
                    prompt=sel_ver.get("prompt"),
                    note=sel_ver.get("note"),
                    created_by=sel_ver.get("created_by", "worker"),
                    created_at=sel_ver.get("created_at", ""),
                    is_selected=True,
                )

        version_rows = [
            VersionDisplayRow(
                version_id=v["id"],
                version_num=v["n"],
                content_type=v.get("content_type"),
                content=v.get("content"),
                content_ref=v.get("content_ref"),
                prompt=v.get("prompt"),
                note=v.get("note"),
                created_by=v.get("created_by", "worker"),
                created_at=v.get("created_at", ""),
                is_selected=(v["id"] == artifact["selected_version_id"]),
            )
            for v in all_versions
        ]

        # Check for generating jobs
        has_generating_job = self._has_generating_jobs(artifact_id)

        card = ArtifactDisplayCard(
            artifact_id=artifact_id,
            stage=artifact["stage"],
            title=artifact["title"],
            status=artifact["status"],
            locked=artifact["locked"],
            selected_version=selected_version_row,
            all_versions=version_rows,
            allowed_actions=stage_obj.allowed_actions,
            approval_required=stage_obj.approval_required,
            has_generating_job=has_generating_job,
            updated_at=artifact.get("updated_at", ""),
        )

        return card


# =============================================================================
# Action Classification & Handlers (pure logic, no gradio)
# =============================================================================


class ActionClassification:
    """Classifies board actions as deterministic or worker-event."""

    DETERMINISTIC_ACTIONS = {
        "select_version",  # Direct store operation, triggers stale propagation
        "approve",  # Direct status update
        "lock",  # Direct lock update
        "unlock",  # Direct lock update
    }

    WORKER_EVENT_ACTIONS = {
        "edit",  # Content replacement, usually text
        "revise",  # Prompt revision for generation
        "regenerate",  # Job creation
        "reopen",  # Status change + worker awareness
        "cancel",  # Job cancellation + worker awareness
        "message",  # Free-form message to worker
    }

    @classmethod
    def is_deterministic(cls, action: str) -> bool:
        """Returns True if action can be handled directly via store."""
        return action in cls.DETERMINISTIC_ACTIONS

    @classmethod
    def is_worker_event(cls, action: str) -> bool:
        """Returns True if action requires worker event."""
        return action in cls.WORKER_EVENT_ACTIONS


class ActionHandler:
    """Handles board actions."""

    def __init__(self, store: Store, stage_config: Optional[StageConfig] = None):
        self.store = store
        self.stage_config = stage_config

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _compute_dedupe_key(self, artifact_id: str, action_type: str, content: Optional[str] = None) -> str:
        """
        Compute a deterministic dedupe key from artifact_id, action_type, and content.
        The key is based on the actual submitted content so identical resubmissions dedupe,
        but different content creates different events.
        """
        key_parts = [artifact_id, action_type]
        if content:
            key_parts.append(content)
        key_str = "|".join(key_parts)
        return hashlib.sha256(key_str.encode()).hexdigest()[:16]

    def _validate_action_allowed(self, artifact_id: str, action: str) -> Optional[str]:
        """
        Validate that an action is allowed for the artifact's stage.
        Returns error message if validation fails, None if valid.
        """
        if not self.stage_config:
            return None  # No stage config, skip validation

        artifact = self.store.get_artifact(artifact_id)
        if not artifact:
            return f"Artifact {artifact_id} not found"

        try:
            stage = self.stage_config.get_stage(artifact["stage"])
            stage.validate_action(action)
            return None  # Action is valid
        except ActionPermissionError as e:
            return str(e)
        except KeyError:
            return f"Stage {artifact['stage']} not found in config"

    # =========================================================================
    # Deterministic Actions (call store directly)
    # =========================================================================

    def select_version(
        self,
        artifact_id: str,
        version_id: str,
        expected_selected_version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Select a version (deterministic).
        Triggers DAG stale propagation.
        Gates on allowed_actions.
        """
        # Validate action is allowed
        error = self._validate_action_allowed(artifact_id, "select_version")
        if error:
            return {"ok": False, "error": error}

        result = self.store.select_version(
            artifact_id, version_id, expected_selected_version_id
        )
        response = {
            "ok": result.get("ok", False),
            "error": result.get("error"),
            "stale_descendants": result.get("stale_descendants", []),
        }
        # Pass through current_selected_version_id on conflict
        if "current_selected_version_id" in result:
            response["current_selected_version_id"] = result["current_selected_version_id"]
        return response

    def approve(self, artifact_id: str, force: bool = False) -> Dict[str, Any]:
        """
        Approve an artifact (deterministic).
        Direct status update to 'approved'.
        Validates: approval_required flag, locked status, terminal states.

        Args:
            artifact_id: ID of artifact to approve
            force: If True, skip locked check (requires explicit confirmation)
        """
        artifact = self.store.get_artifact(artifact_id)
        if not artifact:
            return {"ok": False, "error": f"Artifact {artifact_id} not found"}

        # Validate action is allowed
        error = self._validate_action_allowed(artifact_id, "approve")
        if error:
            return {"ok": False, "error": error}

        # Check if artifact is locked
        if artifact.get("locked"):
            if not force:
                return {
                    "ok": False,
                    "error": "approval_blocked_by_lock",
                    "message": "Artifact is locked. Use force=True to override."
                }

        # Per SPEC section 11: approved MAY be reopened, so most states are ok to approve.
        # But reject already-approved (idempotent but explicit), or terminal states like failed/cancelled
        current_status = artifact.get("status")
        if current_status == "failed":
            return {
                "ok": False,
                "error": "approval_blocked_by_failed_status",
                "message": "Cannot approve a failed artifact."
            }
        if current_status == "cancelled":
            return {
                "ok": False,
                "error": "approval_blocked_by_cancelled_status",
                "message": "Cannot approve a cancelled artifact."
            }

        result = self.store.set_status(artifact_id, "approved")
        return {"ok": True, "artifact": result}

    def lock(self, artifact_id: str) -> Dict[str, Any]:
        """
        Lock an artifact (deterministic).
        Prevents automated regeneration.
        """
        artifact = self.store.get_artifact(artifact_id)
        if not artifact:
            return {"ok": False, "error": f"Artifact {artifact_id} not found"}

        error = self._validate_action_allowed(artifact_id, "lock")
        if error:
            return {"ok": False, "error": error}

        result = self.store.set_lock(artifact_id, True)
        return {"ok": True, "artifact": result}

    def unlock(self, artifact_id: str) -> Dict[str, Any]:
        """
        Unlock an artifact (deterministic).
        Allows automated regeneration.
        """
        artifact = self.store.get_artifact(artifact_id)
        if not artifact:
            return {"ok": False, "error": f"Artifact {artifact_id} not found"}

        error = self._validate_action_allowed(artifact_id, "unlock")
        if error:
            return {"ok": False, "error": error}

        result = self.store.set_lock(artifact_id, False)
        return {"ok": True, "artifact": result}

    # =========================================================================
    # Worker Event Actions (create inbox events)
    # =========================================================================

    def enqueue_edit(self, artifact_id: str, content: str) -> Dict[str, Any]:
        """
        Queue an edit action (requires worker).
        For direct content updates (text/form).
        Idempotent: identical content dedupes at store level.
        """
        dedupe_key = self._compute_dedupe_key(artifact_id, "edit", content)
        payload = {"content": content}
        event = self.store.enqueue_event(
            type="edit",
            payload=payload,
            artifact_id=artifact_id,
            dedupe_key=dedupe_key,
        )
        return {
            "ok": True,
            "event_id": event.get("id"),
            "event": event,
        }

    def enqueue_revise(self, artifact_id: str, note: str) -> Dict[str, Any]:
        """
        Queue a revise action (requires worker).
        For generation revision prompts (image/video).
        Idempotent: identical note dedupes at store level.
        """
        dedupe_key = self._compute_dedupe_key(artifact_id, "revise", note)
        payload = {"note": note}
        event = self.store.enqueue_event(
            type="revise",
            payload=payload,
            artifact_id=artifact_id,
            dedupe_key=dedupe_key,
        )
        return {
            "ok": True,
            "event_id": event.get("id"),
            "event": event,
        }

    def enqueue_regenerate(self, artifact_id: str) -> Dict[str, Any]:
        """
        Queue a regenerate action (requires worker).
        For retriggering generation from current version.
        Idempotent: no content to dedupe, key is deterministic per artifact.
        """
        dedupe_key = self._compute_dedupe_key(artifact_id, "regenerate")
        payload = {}
        event = self.store.enqueue_event(
            type="regenerate",
            payload=payload,
            artifact_id=artifact_id,
            dedupe_key=dedupe_key,
        )
        return {
            "ok": True,
            "event_id": event.get("id"),
            "event": event,
        }

    def enqueue_reopen(self, artifact_id: str) -> Dict[str, Any]:
        """
        Queue a reopen action (requires worker).
        Move from 'approved' back to 'draft' for further work.
        Idempotent: no content to dedupe.
        """
        dedupe_key = self._compute_dedupe_key(artifact_id, "reopen")
        payload = {}
        event = self.store.enqueue_event(
            type="reopen",
            payload=payload,
            artifact_id=artifact_id,
            dedupe_key=dedupe_key,
        )
        return {
            "ok": True,
            "event_id": event.get("id"),
            "event": event,
        }

    def enqueue_cancel(self, artifact_id: str) -> Dict[str, Any]:
        """
        Queue a cancel action (requires worker).
        Requests cancellation of in-progress generation.
        Idempotent: no content to dedupe.
        """
        dedupe_key = self._compute_dedupe_key(artifact_id, "cancel")
        payload = {}
        event = self.store.enqueue_event(
            type="cancel",
            payload=payload,
            artifact_id=artifact_id,
            dedupe_key=dedupe_key,
        )
        return {
            "ok": True,
            "event_id": event.get("id"),
            "event": event,
        }

    def enqueue_message(self, text: str, artifact_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Queue a free-form message (requires worker).
        Can be artifact-scoped or project-wide.
        Idempotent: identical text dedupes at store level.
        """
        # For messages, include artifact_id if provided in the dedupe key
        key_parts = ["message"]
        if artifact_id:
            key_parts.append(artifact_id)
        key_parts.append(text)
        key_str = "|".join(key_parts)
        dedupe_key = hashlib.sha256(key_str.encode()).hexdigest()[:16]

        payload = {"text": text}
        event = self.store.enqueue_event(
            type="message",
            payload=payload,
            artifact_id=artifact_id,
            dedupe_key=dedupe_key,
        )
        return {
            "ok": True,
            "event_id": event.get("id"),
            "event": event,
        }


# =============================================================================
# Content Renderers (simple mapping for display)
# =============================================================================


def render_version_content(version_row: VersionDisplayRow) -> Tuple[str, Optional[Any]]:
    """
    Convert a version to display format.
    Returns (content_type, content_value) for rendering.
    """
    content_type = version_row.content_type or "text/plain"

    if content_type in ("text/plain", "text/markdown", "application/json"):
        # Text content
        return ("text", version_row.content)
    elif content_type.startswith("image/"):
        # Image content (stored as file ref or embedded)
        return ("image", version_row.content_ref or version_row.content)
    elif content_type.startswith("video/"):
        # Video content (stored as file ref)
        return ("video", version_row.content_ref or version_row.content)
    elif content_type.startswith("audio/"):
        # Audio content (stored as file ref)
        return ("audio", version_row.content_ref or version_row.content)
    else:
        # Unknown type, treat as text
        return ("text", f"[{content_type}] {version_row.content_ref or version_row.content}")


def describe_select_version_conflict(result: Dict[str, Any]) -> Optional[str]:
    """
    Build a user-facing conflict message for a select_version result.

    Per SPEC section 41 (Selection Conflicts): if the caller's
    expected_selected_version_id no longer matches the artifact's current
    selection, the store/handler layer returns a conflict (ok=False,
    error="conflict", current_selected_version_id=<actual current>) instead
    of silently overwriting a newer selection. This helper turns that
    conflict shape into a message the UI can surface directly (e.g. in a
    status/message textbox), so the conflict is never silently swallowed.

    Returns None when `result` is not a select_version conflict (i.e. the
    action succeeded, or failed for some other reason such as gating).
    """
    if result.get("ok"):
        return None
    if result.get("error") != "conflict":
        return None
    current = result.get("current_selected_version_id")
    return (
        f"Selection conflict: another version was selected since you loaded "
        f"this view (current: {current}). Refresh to see the latest state "
        f"before retrying."
    )


# =============================================================================
# Presentation helpers (pure, no gradio dependency)
# =============================================================================


BOARD_CSS = """
/* --- Agent Surface board: tighten Gradio's defaults into a review tool --- */

.gradio-container {
  max-width: 1080px !important;
  margin: 0 auto !important;
  font-family: ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}

/* Gradio stacks generous gaps everywhere; pull them in globally. */
.gradio-container .gap { gap: 8px !important; }
.gradio-container .form { gap: 8px !important; }

/* Top bar: project name + live status on one compact line. */
#board-header {
  align-items: center;
  border-bottom: 1px solid var(--border-color-primary);
  padding: 4px 0 10px 0;
  margin-bottom: 4px;
}
#board-header .prose h1,
#board-header .prose h2 { margin: 0 !important; }
.board-title p, .board-title h1, .board-title h2 {
  margin: 0 !important;
  font-size: 17px !important;
  font-weight: 650 !important;
  letter-spacing: -0.01em;
}
.board-status p {
  margin: 0 !important;
  text-align: right;
  font-size: 12.5px !important;
  color: var(--body-text-color-subdued);
}

/* Artifact card: one light container per artifact, not per field. */
.artifact-card {
  border: 1px solid var(--border-color-primary) !important;
  border-radius: 10px !important;
  padding: 14px 16px 12px 16px !important;
  margin-bottom: 14px !important;
  background: var(--background-fill-primary) !important;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
}
.artifact-title p, .artifact-title h3 {
  margin: 0 0 2px 0 !important;
  font-size: 15.5px !important;
  font-weight: 650 !important;
  line-height: 1.3;
}
/* The metadata line: small, single row, never its own boxed section. */
.artifact-meta p {
  margin: 0 0 10px 0 !important;
  font-size: 12px !important;
  color: var(--body-text-color-subdued);
  line-height: 1.5;
}
.artifact-meta strong { font-weight: 600; color: var(--body-text-color); }

/* The review surface itself: the thing the page exists for. */
.content-surface textarea {
  font-size: 15px !important;
  line-height: 1.6 !important;
  border: none !important;
  box-shadow: none !important;
  background: var(--background-fill-secondary) !important;
  padding: 12px 14px !important;
  border-radius: 8px !important;
  resize: vertical;
}
.content-surface .prose {
  font-size: 15px !important;
  line-height: 1.6 !important;
  padding: 12px 14px !important;
  background: var(--background-fill-secondary);
  border-radius: 8px;
}
.content-surface { margin-bottom: 8px !important; }

/* Version history rows: a compact list, one line each. */
.version-row { align-items: center !important; margin: 0 !important; }
.version-line p {
  margin: 0 !important;
  font-size: 12.5px !important;
  color: var(--body-text-color-subdued);
}
.version-line strong { color: var(--body-text-color); }

/* Action bar at the foot of each card. */
.action-bar {
  margin-top: 10px !important;
  padding-top: 10px !important;
  border-top: 1px solid var(--border-color-primary);
  flex-wrap: wrap;
}
.action-bar button { min-height: 32px !important; }

.subtle-note p {
  margin: 0 !important;
  font-size: 12px !important;
  color: var(--body-text-color-subdued);
  font-style: italic;
}

footer { display: none !important; }
"""


_STATUS_DOTS = {
    "approved": "🟢",
    "review": "🟡",
    "generating": "🔵",
    "stale": "🟠",
    "failed": "🔴",
    "cancelled": "⚪",
    "draft": "⚪",
}


def format_status_badge(status: str) -> str:
    """Render a status as a small inline badge (dot + label)."""
    dot = _STATUS_DOTS.get(status, "⚪")
    return f"{dot} {status}"


def format_artifact_meta(card: ArtifactDisplayCard) -> str:
    """
    Build the single-line metadata row for an artifact card.

    All of status / version / stage / lock / generating / stale collapse into
    one small markdown line rather than one boxed section each.
    """
    parts = [f"**Status:** {format_status_badge(card.status)}"]
    if card.selected_version:
        parts.append(f"**Version:** {card.selected_version.version_num}")
    if card.all_versions:
        parts.append(f"**Versions:** {len(card.all_versions)}")
    parts.append(f"**Stage:** {card.stage}")
    if card.locked:
        parts.append("🔒 locked")
    if card.has_generating_job:
        parts.append("⚙️ generating")
    if card.status == "stale" or card.stale_reason:
        parts.append("⚠️ stale")
    return " &nbsp;·&nbsp; ".join(parts)


def format_version_line(version: VersionDisplayRow) -> str:
    """Build the compact one-line description of a version in the history list."""
    marker = "●" if version.is_selected else "○"
    bits = [f"{marker} **v{version.version_num}**"]
    if version.created_by:
        bits.append(version.created_by)
    if version.created_at:
        bits.append(version.created_at)
    if version.note:
        note = version.note if len(version.note) <= 60 else version.note[:57] + "..."
        bits.append(note)
    return " · ".join(bits)


def content_textbox_lines(content: Optional[str]) -> int:
    """
    Pick a sensible height for the content review surface.

    The content is the point of the page, so it gets real vertical space,
    but never so much that a long script pushes the actions off-screen.
    """
    if not content:
        return 4
    newline_estimate = content.count("\n") + 1
    wrap_estimate = len(content) // 90 + 1
    return max(8, min(28, max(newline_estimate, wrap_estimate) + 1))


# =============================================================================
# Gradio Board Wiring (conditional on gradio availability)
# =============================================================================

try:
    import gradio as gr
    HAS_GRADIO = True
except ImportError:
    HAS_GRADIO = False


def build_board(store: Store, stage_config: StageConfig, media_dir: Optional[str] = None) -> Optional[Any]:
    """
    Build a Gradio Blocks UI for the board.
    Returns None if gradio is not available.

    Args:
        store: SQLite artifact store
        stage_config: Stage configuration
        media_dir: Base directory for media file resolution (default: None, treat content_ref as absolute)
    """
    if not HAS_GRADIO:
        return None

    display_builder = DisplayModelBuilder(store, stage_config)
    action_handler = ActionHandler(store, stage_config)

    # Initial board state
    board_display = display_builder.build_board_display()

    def _resolve_media_path(content_ref: Optional[str]) -> Optional[str]:
        """Resolve media path for display, accounting for media_dir if set."""
        if not content_ref:
            return None
        if media_dir and not content_ref.startswith("/"):
            # Relative path: resolve against media_dir
            from pathlib import Path
            return str(Path(media_dir) / content_ref)
        return content_ref

    def refresh_board() -> Tuple[str, str, str]:
        """Refresh board state from store."""
        display = display_builder.build_board_display()
        title = (
            f"{stage_config.title} &nbsp;·&nbsp; "
            f"{display.pending_review_count} pending review"
        )
        status = display.status_message
        artifacts_json = json.dumps(display.to_dict(), indent=2)
        return title, status, artifacts_json

    def handle_approve_artifact(artifact_id: str, force_unlock: bool = False) -> str:
        """Handle approve action."""
        force = force_unlock  # User confirmed override if true
        result = action_handler.approve(artifact_id, force=force)
        return json.dumps(result)

    def handle_select_version(
        artifact_id: str, version_id: str, expected_selected_version_id: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Handle select_version action with optimistic concurrency.

        On conflict (stale expected_selected_version_id), no side effect is
        applied at the data layer (verified in store.select_version /
        ActionHandler.select_version) and the conflict is surfaced to the
        user via the status textbox instead of failing silently.
        """
        result = action_handler.select_version(artifact_id, version_id, expected_selected_version_id)
        conflict_message = describe_select_version_conflict(result)
        if conflict_message:
            status_text = conflict_message
        else:
            status_text = display_builder.build_board_display().status_message
        return json.dumps(result), status_text

    def handle_lock_artifact(artifact_id: str) -> str:
        """Handle lock action."""
        result = action_handler.lock(artifact_id)
        return json.dumps(result)

    def handle_unlock_artifact(artifact_id: str) -> str:
        """Handle unlock action."""
        result = action_handler.unlock(artifact_id)
        return json.dumps(result)

    def handle_revise_artifact(artifact_id: str, note: str) -> str:
        """Handle revise action."""
        if not note:
            return json.dumps({"ok": False, "error": "Note cannot be empty"})
        result = action_handler.enqueue_revise(artifact_id, note)
        return json.dumps(result)

    def handle_regenerate_artifact(artifact_id: str) -> str:
        """Handle regenerate action."""
        result = action_handler.enqueue_regenerate(artifact_id)
        return json.dumps(result)

    def handle_cancel_artifact(artifact_id: str) -> str:
        """Handle cancel action."""
        result = action_handler.enqueue_cancel(artifact_id)
        return json.dumps(result)

    def handle_edit_artifact(artifact_id: str, content: str) -> str:
        """Handle edit action."""
        if not content:
            return json.dumps({"ok": False, "error": "Content cannot be empty"})
        result = action_handler.enqueue_edit(artifact_id, content)
        return json.dumps(result)

    def handle_message(message_text: str) -> str:
        """Handle message action."""
        if not message_text:
            return json.dumps({"ok": False, "error": "Message cannot be empty"})
        result = action_handler.enqueue_message(message_text)
        return json.dumps(result)

    # Build Gradio interface
    with gr.Blocks(
        title=f"{stage_config.title} - Agent Surface Board",
        theme=gr.themes.Soft(),
        css=BOARD_CSS,
    ) as demo:
        # Single hidden sink for action results (JSON). Keeps every handler's
        # return value wired somewhere without adding visible chrome.
        action_sink = gr.Textbox(visible=False, label="result")

        # --- Compact header bar: project on the left, live status on the right.
        with gr.Row(elem_id="board-header"):
            title_display = gr.Markdown(
                value=f"{stage_config.title} &nbsp;·&nbsp; {board_display.pending_review_count} pending review",
                elem_classes=["board-title"],
            )
            status_display = gr.Markdown(
                value=board_display.status_message,
                elem_classes=["board-status"],
            )
            btn_refresh = gr.Button(
                "Refresh", variant="secondary", size="sm", scale=0, min_width=90
            )

        # --- One tab per stage.
        with gr.Tabs():
            for stage_group in board_display.stages:
                tab_label = (
                    f"{stage_group.stage_title} ({stage_group.approved_count}/{stage_group.total_count})"
                    if stage_group.total_count
                    else stage_group.stage_title
                )
                with gr.Tab(label=tab_label):
                    if not stage_group.artifacts:
                        gr.Markdown(
                            "_No artifacts in this stage yet._",
                            elem_classes=["subtle-note"],
                        )
                        continue

                    for artifact_card in stage_group.artifacts:
                        artifact_state = gr.State(artifact_card.artifact_id)

                        with gr.Column(elem_classes=["artifact-card"]):
                            # Title + one-line metadata badge row.
                            gr.Markdown(
                                f"### {artifact_card.title}",
                                elem_classes=["artifact-title"],
                            )
                            gr.Markdown(
                                format_artifact_meta(artifact_card),
                                elem_classes=["artifact-meta"],
                            )

                            # --- The review surface: the dominant element.
                            if artifact_card.selected_version:
                                sel_ver = artifact_card.selected_version
                                content_kind, content_value = render_version_content(sel_ver)

                                if content_kind == "image":
                                    resolved_path = _resolve_media_path(content_value)
                                    if resolved_path:
                                        gr.Image(
                                            value=resolved_path,
                                            show_label=False,
                                            interactive=False,
                                            height=460,
                                            elem_classes=["content-surface"],
                                        )
                                elif content_kind == "video":
                                    resolved_path = _resolve_media_path(content_value)
                                    if resolved_path:
                                        gr.Video(
                                            value=resolved_path,
                                            show_label=False,
                                            interactive=False,
                                            height=460,
                                            elem_classes=["content-surface"],
                                        )
                                elif content_kind == "audio":
                                    resolved_path = _resolve_media_path(content_value)
                                    if resolved_path:
                                        gr.Audio(
                                            value=resolved_path,
                                            show_label=False,
                                            interactive=False,
                                            elem_classes=["content-surface"],
                                        )
                                elif content_kind == "text" and content_value:
                                    lines = content_textbox_lines(content_value)
                                    gr.Textbox(
                                        value=content_value,
                                        show_label=False,
                                        container=False,
                                        interactive=False,
                                        lines=lines,
                                        max_lines=lines,
                                        elem_classes=["content-surface"],
                                    )

                                # Prompt / note stay secondary: collapsed by default.
                                if sel_ver.prompt or sel_ver.note:
                                    with gr.Accordion("Prompt & notes", open=False):
                                        if sel_ver.prompt:
                                            gr.Markdown(f"**Prompt** — {sel_ver.prompt}")
                                        if sel_ver.note:
                                            gr.Markdown(f"**Note** — {sel_ver.note}")
                            else:
                                gr.Markdown(
                                    "_No version selected yet._",
                                    elem_classes=["subtle-note"],
                                )

                            # --- Version history: a compact collapsed list.
                            if (
                                len(artifact_card.all_versions) > 1
                                and "select_version" in artifact_card.allowed_actions
                            ):
                                current_selection = (
                                    artifact_card.selected_version.version_id
                                    if artifact_card.selected_version
                                    else ""
                                )
                                with gr.Accordion(
                                    f"Version history ({len(artifact_card.all_versions)})",
                                    open=False,
                                ):
                                    for version in artifact_card.all_versions:
                                        with gr.Row(elem_classes=["version-row"]):
                                            gr.Markdown(
                                                format_version_line(version),
                                                elem_classes=["version-line"],
                                            )
                                            if version.is_selected:
                                                gr.Button(
                                                    "Selected",
                                                    variant="secondary",
                                                    size="sm",
                                                    interactive=False,
                                                    scale=0,
                                                    min_width=88,
                                                )
                                            else:
                                                btn_select = gr.Button(
                                                    "Select",
                                                    variant="secondary",
                                                    size="sm",
                                                    scale=0,
                                                    min_width=88,
                                                )
                                                btn_select.click(
                                                    handle_select_version,
                                                    inputs=[
                                                        artifact_state,
                                                        gr.State(version.version_id),
                                                        gr.State(current_selection),
                                                    ],
                                                    # Result to the hidden sink; any
                                                    # conflict message to the visible
                                                    # status line (SPEC section 41:
                                                    # conflicts must be surfaced).
                                                    outputs=[action_sink, status_display],
                                                )

                            # --- Inputs for text edits / revision notes.
                            if "edit" in artifact_card.allowed_actions:
                                with gr.Row():
                                    edit_content = gr.Textbox(
                                        show_label=False,
                                        container=False,
                                        placeholder="Replace content…",
                                        lines=2,
                                        scale=5,
                                    )
                                    btn_edit = gr.Button(
                                        "Update", variant="secondary", size="sm",
                                        scale=0, min_width=96,
                                    )
                                    btn_edit.click(
                                        handle_edit_artifact,
                                        inputs=[artifact_state, edit_content],
                                        outputs=action_sink,
                                    )

                            if "revise" in artifact_card.allowed_actions:
                                with gr.Row():
                                    revision_note = gr.Textbox(
                                        show_label=False,
                                        container=False,
                                        placeholder="Revision note for the worker…",
                                        lines=1,
                                        scale=5,
                                    )
                                    btn_revise = gr.Button(
                                        "Revise", variant="secondary", size="sm",
                                        scale=0, min_width=96,
                                    )
                                    btn_revise.click(
                                        handle_revise_artifact,
                                        inputs=[artifact_state, revision_note],
                                        outputs=action_sink,
                                    )

                            # --- Action bar: one row, weighted by importance.
                            with gr.Row(elem_classes=["action-bar"]):
                                if "approve" in artifact_card.allowed_actions:
                                    btn_approve = gr.Button(
                                        "Approve (locked)" if artifact_card.locked else "Approve",
                                        variant="primary",
                                        size="sm",
                                        interactive=not artifact_card.locked,
                                        scale=0,
                                        min_width=120,
                                    )
                                    btn_approve.click(
                                        handle_approve_artifact,
                                        inputs=[artifact_state, gr.State(False)],
                                        outputs=action_sink,
                                    )

                                if "regenerate" in artifact_card.allowed_actions:
                                    btn_regen = gr.Button(
                                        "Regenerate", variant="secondary", size="sm",
                                        scale=0, min_width=110,
                                    )
                                    btn_regen.click(
                                        handle_regenerate_artifact,
                                        inputs=artifact_state,
                                        outputs=action_sink,
                                    )

                                if "lock" in artifact_card.allowed_actions:
                                    btn_lock = gr.Button(
                                        "Unlock" if artifact_card.locked else "Lock",
                                        variant="secondary",
                                        size="sm",
                                        scale=0,
                                        min_width=96,
                                    )
                                    btn_lock.click(
                                        handle_unlock_artifact
                                        if artifact_card.locked
                                        else handle_lock_artifact,
                                        inputs=artifact_state,
                                        outputs=action_sink,
                                    )

                                if "cancel" in artifact_card.allowed_actions:
                                    btn_cancel = gr.Button(
                                        "Cancel", variant="stop", size="sm",
                                        scale=0, min_width=96,
                                    )
                                    btn_cancel.click(
                                        handle_cancel_artifact,
                                        inputs=artifact_state,
                                        outputs=action_sink,
                                    )

        # --- Worker message box: one compact line at the foot of the page.
        with gr.Row():
            message_input = gr.Textbox(
                show_label=False,
                container=False,
                placeholder="Message the worker…",
                lines=1,
                scale=6,
            )
            btn_message = gr.Button(
                "Send", variant="secondary", size="sm", scale=0, min_width=96
            )
            btn_message.click(
                handle_message,
                inputs=message_input,
                outputs=action_sink,
            )

        btn_refresh.click(
            refresh_board,
            outputs=[title_display, status_display, action_sink],
        )

    return demo
