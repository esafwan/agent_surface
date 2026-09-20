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
        title = f"{stage_config.title} ({display.pending_review_count} pending)"
        status = display.status_message
        artifacts_json = json.dumps(display.to_dict(), indent=2)
        return title, status, artifacts_json

    def handle_approve_artifact(artifact_id: str, force_unlock: bool = False) -> str:
        """Handle approve action."""
        force = force_unlock  # User confirmed override if true
        result = action_handler.approve(artifact_id, force=force)
        return json.dumps(result)

    def handle_select_version(artifact_id: str, version_id: str, expected_selected_version_id: Optional[str] = None) -> str:
        """Handle select_version action with optimistic concurrency."""
        result = action_handler.select_version(artifact_id, version_id, expected_selected_version_id)
        return json.dumps(result)

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
    ) as demo:
        gr.Markdown(f"# {stage_config.title}")

        with gr.Row():
            title_display = gr.Textbox(
                value=f"{stage_config.title} ({board_display.pending_review_count} pending)",
                label="Project",
                interactive=False,
            )
            status_display = gr.Textbox(
                value=board_display.status_message,
                label="Status",
                interactive=False,
            )

        # Tab for each stage
        with gr.Tabs():
            for stage_group in board_display.stages:
                with gr.Tab(label=stage_group.stage_id):
                    gr.Markdown(f"## {stage_group.stage_title}")

                    if stage_group.artifacts:
                        for artifact_card in stage_group.artifacts:
                            with gr.Group():
                                gr.Markdown(
                                    f"### {artifact_card.title} "
                                    f"[{artifact_card.status}]"
                                )

                                with gr.Row():
                                    status_badge = gr.Label(
                                        value=artifact_card.status,
                                        label="Status",
                                    )
                                    if artifact_card.locked:
                                        gr.Label(value="🔒 Locked")
                                    if artifact_card.has_generating_job:
                                        gr.Label(value="⚙️ Generating")
                                    if artifact_card.status == "stale":
                                        gr.Label(value="⚠️ Stale")

                                # Selected version display (with media rendering)
                                if artifact_card.selected_version:
                                    sel_ver = artifact_card.selected_version
                                    with gr.Group():
                                        gr.Markdown("#### Selected Version")
                                        gr.Textbox(
                                            value=f"Version {sel_ver.version_num}",
                                            label="Version",
                                            interactive=False,
                                        )

                                        # Render version content based on type
                                        content_type, content_value = render_version_content(sel_ver)
                                        if content_type == "image":
                                            resolved_path = _resolve_media_path(content_value)
                                            if resolved_path:
                                                gr.Image(
                                                    value=resolved_path,
                                                    label="Image Content",
                                                    interactive=False,
                                                )
                                        elif content_type == "video":
                                            resolved_path = _resolve_media_path(content_value)
                                            if resolved_path:
                                                gr.Video(
                                                    value=resolved_path,
                                                    label="Video Content",
                                                    interactive=False,
                                                )
                                        elif content_type == "audio":
                                            resolved_path = _resolve_media_path(content_value)
                                            if resolved_path:
                                                gr.Audio(
                                                    value=resolved_path,
                                                    label="Audio Content",
                                                    interactive=False,
                                                )
                                        elif content_type == "text" and content_value:
                                            # Show text content
                                            if len(content_value) > 500:
                                                gr.Textbox(
                                                    value=content_value,
                                                    label="Content",
                                                    interactive=False,
                                                    lines=8,
                                                )
                                            else:
                                                gr.Textbox(
                                                    value=content_value,
                                                    label="Content",
                                                    interactive=False,
                                                )

                                        if sel_ver.prompt:
                                            gr.Textbox(
                                                value=sel_ver.prompt,
                                                label="Prompt",
                                                interactive=False,
                                            )
                                        if sel_ver.note:
                                            gr.Textbox(
                                                value=sel_ver.note,
                                                label="Note",
                                                interactive=False,
                                            )

                                # Version history (gated on select_version in allowed_actions)
                                if len(artifact_card.all_versions) > 1 and "select_version" in artifact_card.allowed_actions:
                                    with gr.Group():
                                        gr.Markdown("#### Version History")
                                        for version in artifact_card.all_versions:
                                            with gr.Row():
                                                gr.Textbox(
                                                    value=f"v{version.version_num}",
                                                    scale=1,
                                                    interactive=False,
                                                )
                                                # Only show select button if action is allowed and version is not already selected
                                                if not version.is_selected:
                                                    btn_select = gr.Button(
                                                        "Select",
                                                        scale=1,
                                                    )
                                                    # Pass expected_selected_version_id from current display state
                                                    btn_select.click(
                                                        handle_select_version,
                                                        inputs=[
                                                            gr.Textbox(
                                                                value=artifact_card.artifact_id,
                                                                visible=False,
                                                            ),
                                                            gr.Textbox(
                                                                value=version.version_id,
                                                                visible=False,
                                                            ),
                                                            gr.Textbox(
                                                                value=artifact_card.selected_version.version_id if artifact_card.selected_version else "",
                                                                visible=False,
                                                            ),
                                                        ],
                                                        outputs=gr.Textbox(visible=False),
                                                    )
                                                else:
                                                    gr.Button(
                                                        "Selected",
                                                        scale=1,
                                                        interactive=False,
                                                    )

                                # Action buttons
                                with gr.Row():
                                    # Approve button (gated on allowed_actions and not locked)
                                    if "approve" in artifact_card.allowed_actions:
                                        approve_label = "Approve (Locked)" if artifact_card.locked else "Approve"
                                        btn_approve = gr.Button(
                                            approve_label,
                                            interactive=not artifact_card.locked,
                                        )
                                        btn_approve.click(
                                            handle_approve_artifact,
                                            inputs=[
                                                gr.Textbox(
                                                    value=artifact_card.artifact_id,
                                                    visible=False,
                                                ),
                                                gr.Checkbox(
                                                    value=False,
                                                    visible=False,
                                                ),
                                            ],
                                            outputs=gr.Textbox(visible=False),
                                        )

                                    if "lock" in artifact_card.allowed_actions:
                                        btn_lock = gr.Button(
                                            "Unlock" if artifact_card.locked else "Lock"
                                        )
                                        if artifact_card.locked:
                                            btn_lock.click(
                                                handle_unlock_artifact,
                                                inputs=gr.Textbox(
                                                    value=artifact_card.artifact_id,
                                                    visible=False,
                                                ),
                                                outputs=gr.Textbox(visible=False),
                                            )
                                        else:
                                            btn_lock.click(
                                                handle_lock_artifact,
                                                inputs=gr.Textbox(
                                                    value=artifact_card.artifact_id,
                                                    visible=False,
                                                ),
                                                outputs=gr.Textbox(visible=False),
                                            )

                                    if "regenerate" in artifact_card.allowed_actions:
                                        btn_regen = gr.Button("Regenerate")
                                        btn_regen.click(
                                            handle_regenerate_artifact,
                                            inputs=gr.Textbox(
                                                value=artifact_card.artifact_id,
                                                visible=False,
                                            ),
                                            outputs=gr.Textbox(visible=False),
                                        )

                                    if "cancel" in artifact_card.allowed_actions:
                                        btn_cancel = gr.Button("Cancel")
                                        btn_cancel.click(
                                            handle_cancel_artifact,
                                            inputs=gr.Textbox(
                                                value=artifact_card.artifact_id,
                                                visible=False,
                                            ),
                                            outputs=gr.Textbox(visible=False),
                                        )

                                # Revision input for generated content
                                if "revise" in artifact_card.allowed_actions:
                                    with gr.Row():
                                        revision_note = gr.Textbox(
                                            label="Revision",
                                            placeholder="Enter revision notes...",
                                        )
                                        btn_revise = gr.Button("Revise")
                                        btn_revise.click(
                                            handle_revise_artifact,
                                            inputs=[
                                                gr.Textbox(
                                                    value=artifact_card.artifact_id,
                                                    visible=False,
                                                ),
                                                revision_note,
                                            ],
                                            outputs=gr.Textbox(visible=False),
                                        )

                                # Edit input for text content
                                if "edit" in artifact_card.allowed_actions:
                                    with gr.Row():
                                        edit_content = gr.Textbox(
                                            label="Edit",
                                            placeholder="Enter new content...",
                                            lines=3,
                                        )
                                        btn_edit = gr.Button("Update")
                                        btn_edit.click(
                                            handle_edit_artifact,
                                            inputs=[
                                                gr.Textbox(
                                                    value=artifact_card.artifact_id,
                                                    visible=False,
                                                ),
                                                edit_content,
                                            ],
                                            outputs=gr.Textbox(visible=False),
                                        )

                    else:
                        gr.Markdown("_No artifacts in this stage yet._")

        # Message input at bottom
        with gr.Group():
            gr.Markdown("### Send Message to Worker")
            message_input = gr.Textbox(
                label="Message",
                placeholder="Send a message to the worker...",
                lines=2,
            )
            btn_message = gr.Button("Send")
            btn_message.click(
                handle_message,
                inputs=message_input,
                outputs=gr.Textbox(visible=False),
            )

        # Refresh button
        btn_refresh = gr.Button("Refresh")
        btn_refresh.click(
            refresh_board,
            outputs=[title_display, status_display, gr.Textbox(visible=False)],
        )

    return demo
