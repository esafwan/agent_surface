"""
Web board renderer for Agent Surface: FastAPI + hand-written HTML/CSS/JS.

Why this exists
---------------
The Gradio board (`surface.board.build_board`) works, but its UX failed in
live testing in three specific ways:

  1. the header said "Ready" whether the inbox was empty or held a dozen
     unclaimed clicks, so a queued action looked exactly like a dead button;
  2. the "Message the worker" box at the foot of the page posted an event
     with `artifact_id: null`, unbound to anything on screen;
  3. the layout was full-bleed and flat, with no hierarchy between the thing
     under review and the chrome around it.

Fighting Gradio's component model and default theme to fix (3) is a losing
battle -- its dynamic control set and its own CSS tokens push back. So this
module is a second renderer over the SAME logic layer: it imports
`DisplayModelBuilder`, `ActionHandler`, `BoardDisplayModel` and the
formatting helpers from `surface.board` unchanged, exactly as
`surface.tui` does for the terminal. Nothing here reaches into `store.conn`
or re-derives state a display model already owns.

Contract
--------
  GET  /                      the page (HTTP basic auth when a token is set)
  GET  /static/*              stylesheet + script (authed, same as / -- no build step, no npm)
  GET  /api/state             the whole board as JSON, plus a fingerprint
  POST /api/action            one board action, returns result + fresh state
  GET  /api/media/{ver_id}    a version's media file, constrained to media_dir

The client polls /api/state and repaints only when the fingerprint changes.
"""
import hmac
import base64
import binascii
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# FastAPI is imported at module scope, guarded, for one concrete reason:
# from Python 3.14 annotations are evaluated lazily (PEP 649), and FastAPI
# resolves a route handler's `request: Request` annotation against the
# *module* globals. Importing Request inside create_app() leaves that name
# unresolvable, and every route then 422s asking for a `request` query
# parameter. create_app() re-raises the stored ImportError so a missing
# dependency still degrades the way the CLI expects.
try:
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    HAS_FASTAPI = True
    _FASTAPI_IMPORT_ERROR: Optional[ImportError] = None
except ImportError as _exc:  # pragma: no cover - exercised only without fastapi
    HAS_FASTAPI = False
    _FASTAPI_IMPORT_ERROR = _exc
    FastAPI = Request = FileResponse = JSONResponse = StaticFiles = None  # type: ignore

from surface.board import (
    ActionHandler,
    ArtifactDisplayCard,
    BoardDisplayModel,
    DisplayModelBuilder,
    VersionDisplayRow,
    board_state_fingerprint,
    describe_select_version_conflict,
    field_label_for,
    format_age,
    format_queued_note,
    parse_form_answers,
    parse_iso_timestamp,
    render_version_content,
    schema_properties,
)
from surface.diff import diff_versions
from surface.stages.config import StageConfig
from surface.store import Store

STATIC_DIR = Path(__file__).parent / "static"

BASIC_AUTH_USER = "surface"
BASIC_AUTH_REALM = "Agent Surface Board"


# =============================================================================
# Media resolution (path traversal is a real risk here)
# =============================================================================


def resolve_media_path(media_dir: Optional[str], content_ref: Optional[str]) -> Optional[Path]:
    """Resolve a version's `content_ref` to a file on disk, or None.

    A `content_ref` is worker-supplied data, so it is never trusted as a
    path. When `media_dir` is configured, the resolved path must stay inside
    it -- `../../etc/passwd` resolves out of the directory and is rejected
    rather than served. When no `media_dir` is configured the ref is only
    honoured if it is already absolute and exists (there is no base to
    constrain a relative ref against, so we decline instead of guessing).
    """
    if not content_ref:
        return None

    if media_dir:
        base = Path(media_dir).resolve()
        candidate = (base / content_ref).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return None  # escaped the media directory
        return candidate if candidate.is_file() else None

    candidate = Path(content_ref)
    if not candidate.is_absolute():
        return None
    candidate = candidate.resolve()
    return candidate if candidate.is_file() else None


# =============================================================================
# State payload (pure: display model -> JSON-able dict)
# =============================================================================


def _version_payload(version) -> Dict[str, Any]:
    data = version.to_dict()
    data["age"] = _age_of(version.created_at)
    return data


def _age_of(timestamp: Optional[str], now: Optional[datetime] = None) -> str:
    parsed = parse_iso_timestamp(timestamp)
    if not parsed:
        return ""
    now = now or datetime.now(timezone.utc)
    return format_age((now - parsed).total_seconds())


def _artifact_payload(
    card: ArtifactDisplayCard,
    form_schema: Optional[Dict[str, Any]],
    now: datetime,
) -> Dict[str, Any]:
    """One artifact card, flattened for the client.

    Everything the browser needs to draw a card is decided here, in Python,
    from the display model: the client does no state derivation of its own,
    it only turns this dict into DOM.
    """
    data = card.to_dict()
    data["queued_note"] = format_queued_note(card, now=now)
    data["busy"] = bool(card.active_event_count) or card.has_generating_job
    data["updated_age"] = _age_of(card.updated_at, now)
    data["all_versions"] = [_version_payload(v) for v in card.all_versions]

    content_kind: Optional[str] = None
    content_value: Optional[str] = None
    if card.selected_version:
        content_kind, content_value = render_version_content(card.selected_version)
        data["selected_version"] = _version_payload(card.selected_version)

    # A form stage renders native fields instead of a raw JSON blob
    # (SPEC section 31). Only text content can carry form answers.
    fields: List[Dict[str, Any]] = []
    if form_schema and content_kind == "text":
        answers = parse_form_answers(content_value)
        required = set(
            form_schema.get("required", []) if isinstance(form_schema, dict) else []
        )
        for prop_name, prop_schema in schema_properties(form_schema):
            prop_schema = prop_schema if isinstance(prop_schema, dict) else {}
            fields.append(
                {
                    "name": prop_name,
                    "label": field_label_for(prop_name, prop_schema),
                    "description": prop_schema.get("description"),
                    "required": prop_name in required,
                    "type": prop_schema.get("type", "string"),
                    "enum": prop_schema.get("enum"),
                    "value": answers.get(prop_name, prop_schema.get("default", "")),
                }
            )

    data["form_fields"] = fields
    data["content_kind"] = "form" if fields else (content_kind or "none")
    data["content_text"] = content_value if content_kind == "text" else None
    data["media_url"] = (
        f"/api/media/{card.selected_version.version_id}"
        if card.selected_version
        and content_kind in ("image", "video", "audio")
        and content_value
        else None
    )
    data["media_ref"] = content_value if data["media_url"] else None
    return data


def build_state_payload(
    display: BoardDisplayModel,
    stage_config: StageConfig,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The whole board, as the client sees it."""
    now = now or datetime.now(timezone.utc)

    stages = []
    for group in display.stages:
        stage_obj = stage_config.get_stage(group.stage_id)
        form_schema = getattr(stage_obj, "form_schema", None)
        board_view = getattr(stage_obj, "board_view", None)
        stages.append(
            {
                "stage_id": group.stage_id,
                "stage_title": group.stage_title,
                "artifact_type": group.artifact_type,
                "approval_required": group.approval_required,
                "approved_count": group.approved_count,
                "total_count": group.total_count,
                "queued_count": sum(
                    a.active_event_count for a in group.artifacts
                ),
                "board_view": board_view,
                "artifacts": [
                    _artifact_payload(card, form_schema, now)
                    for card in group.artifacts
                ],
            }
        )

    return {
        "title": stage_config.title or stage_config.id,
        "fingerprint": board_state_fingerprint(display),
        "status_message": display.status_message,
        "pending_review_count": display.pending_review_count,
        "pending_event_count": display.total_pending_events,
        "processing_event_count": display.processing_event_count,
        # True whenever any click is unresolved anywhere on the board. The
        # header dot uses this, so "something is happening" is never a guess.
        "busy": display.active_event_count > 0,
        "stages": stages,
    }


STATUS_STYLE = {
    "approved": "fill:#2e7d32,color:#fff,stroke:#1b5e20",
    "stale": "fill:#e69500,color:#fff,stroke:#a86700",
    "failed": "fill:#c62828,color:#fff,stroke:#7f0000",
}
CENTER_STYLE = "fill:#1565c0,color:#fff,stroke:#0d47a1,stroke-width:3px"


def _mermaid_node_id(artifact_id: str) -> str:
    """A Mermaid-safe node id derived from an artifact id.

    Mermaid node ids can't contain spaces or several punctuation characters
    that legitimately show up in artifact ids, so this maps to a plain
    alnum/underscore token while staying stable and unique per input.
    """
    return "n_" + "".join(c if c.isalnum() else "_" for c in artifact_id)


def _mermaid_label(title: Optional[str], artifact_id: str, locked: bool) -> str:
    label = title or artifact_id
    if locked:
        label = "\U0001F512 " + label
    # Escape quotes so the label stays inside Mermaid's quoted node text.
    label = label.replace('"', "'")
    return label


def build_dependency_graph_mermaid(store: Store, artifact_id: str) -> Dict[str, Any]:
    """Build Mermaid `graph TD` text for an artifact's immediate DAG neighborhood.

    Reuses `Store.get_graph`, which already returns only direct upstream and
    downstream neighbors (no recursive/full-project walk). Node status is
    encoded as fill color (approved=green, stale=amber, failed=red); a locked
    artifact gets a padlock prefix on its label; the center artifact gets its
    own distinct style so it reads clearly against its neighbors.

    Returns {"artifact_id", "mermaid", "has_dependencies"}. When the artifact
    has no upstream or downstream neighbors, `mermaid` still renders a single
    labeled node rather than an empty/invalid diagram, and
    `has_dependencies` is False so the client can show a "no dependencies"
    hint alongside it.
    """
    center = store.get_artifact(artifact_id)
    center_title = center.get("title") if center else artifact_id
    center_locked = bool(center.get("locked")) if center else False

    graph = store.get_graph(artifact_id)
    upstreams = graph.get("upstreams", [])
    downstreams = graph.get("downstreams", [])
    has_dependencies = bool(upstreams) or bool(downstreams)

    center_node = _mermaid_node_id(artifact_id)
    lines = ["graph TD"]
    lines.append(f'    {center_node}["{_mermaid_label(center_title, artifact_id, center_locked)}"]')
    style_lines = [f"    style {center_node} {CENTER_STYLE}"]

    seen_ids = {artifact_id}
    for up in upstreams:
        up_id = up.get("upstream_artifact_id")
        if up_id is None or up_id in seen_ids:
            continue
        seen_ids.add(up_id)
        neighbor = store.get_artifact(up_id)
        title = neighbor.get("title") if neighbor else up_id
        status = neighbor.get("status") if neighbor else None
        locked = bool(neighbor.get("locked")) if neighbor else False
        node = _mermaid_node_id(up_id)
        lines.append(f'    {node}["{_mermaid_label(title, up_id, locked)}"] --> {center_node}')
        if status in STATUS_STYLE:
            style_lines.append(f"    style {node} {STATUS_STYLE[status]}")

    for down in downstreams:
        down_id = down.get("downstream_artifact_id")
        if down_id is None or down_id in seen_ids:
            continue
        seen_ids.add(down_id)
        neighbor = store.get_artifact(down_id)
        title = neighbor.get("title") if neighbor else down_id
        status = neighbor.get("status") if neighbor else None
        locked = bool(neighbor.get("locked")) if neighbor else False
        node = _mermaid_node_id(down_id)
        lines.append(f'    {center_node} --> {node}["{_mermaid_label(title, down_id, locked)}"]')
        if status in STATUS_STYLE:
            style_lines.append(f"    style {node} {STATUS_STYLE[status]}")

    lines.extend(style_lines)

    return {
        "artifact_id": artifact_id,
        "mermaid": "\n".join(lines),
        "has_dependencies": has_dependencies,
    }


# =============================================================================
# Action dispatch (thin adapter over ActionHandler -- no logic of its own)
# =============================================================================


def dispatch_action(
    handler: ActionHandler, action: str, body: Dict[str, Any]
) -> Dict[str, Any]:
    """Route one client action to `ActionHandler`.

    Every branch is a straight call into the shared handler; the only work
    done here is argument shaping and turning a select_version conflict into
    a message the user can read (SPEC section 41 -- a conflict must never be
    swallowed).
    """
    artifact_id = body.get("artifact_id")

    if action == "approve":
        result = handler.approve(artifact_id, force=bool(body.get("force")))
    elif action == "lock":
        result = handler.lock(artifact_id)
    elif action == "unlock":
        result = handler.unlock(artifact_id)
    elif action == "select_version":
        result = handler.select_version(
            artifact_id,
            body.get("version_id"),
            body.get("expected_selected_version_id") or None,
        )
        conflict = describe_select_version_conflict(result)
        if conflict:
            result = dict(result)
            result["message"] = conflict
    elif action == "edit":
        content = body.get("content") or ""
        if not content.strip():
            return {"ok": False, "error": "Content cannot be empty"}
        result = handler.enqueue_edit(artifact_id, content)
    elif action == "revise":
        note = body.get("note") or ""
        if not note.strip():
            return {"ok": False, "error": "Revision note cannot be empty"}
        result = handler.enqueue_revise(artifact_id, note)
    elif action == "regenerate":
        result = handler.enqueue_regenerate(artifact_id)
    elif action == "reopen":
        result = handler.enqueue_reopen(artifact_id)
    elif action == "cancel":
        result = handler.enqueue_cancel(artifact_id)
    elif action == "message":
        text = body.get("text") or ""
        if not text.strip():
            return {"ok": False, "error": "Message cannot be empty"}
        # Unlike the old Gradio footer box, a message is ALWAYS bound to the
        # artifact the user is looking at. An unbound message (artifact_id
        # null) reaches nothing in particular, so the client never sends one
        # and the server refuses it rather than dropping it into the inbox.
        if not artifact_id:
            return {
                "ok": False,
                "error": "A message must name the artifact it is about",
            }
        result = handler.enqueue_message(text, artifact_id=artifact_id)
    else:
        return {"ok": False, "error": f"Unknown action '{action}'"}

    return result


# =============================================================================
# Auth
# =============================================================================


def check_basic_auth(header: Optional[str], token: Optional[str]) -> bool:
    """HTTP basic auth against the per-run runtime token (SPEC section 36).

    Same credential shape the Gradio board used -- user "surface", password
    = the token in `.surface-board/run/token` -- so nothing downstream of
    the renderer has to change. `token=None` means auth is disabled, which
    only happens in tests and in explicitly unauthenticated local runs.
    """
    if not token:
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, _, password = decoded.partition(":")
    return hmac.compare_digest(user, BASIC_AUTH_USER) and hmac.compare_digest(
        password, token
    )


# =============================================================================
# The app
# =============================================================================


def create_app(
    store: Store,
    stage_config: StageConfig,
    media_dir: Optional[str] = None,
    token: Optional[str] = None,
) -> Any:
    """Build the FastAPI application for the web board.

    Raises ImportError if FastAPI is unavailable, so the CLI can fall back
    the same way it does for a missing Gradio.
    """
    if not HAS_FASTAPI:
        raise _FASTAPI_IMPORT_ERROR or ImportError("fastapi is not installed")

    display_builder = DisplayModelBuilder(store, stage_config)
    action_handler = ActionHandler(store, stage_config)

    app = FastAPI(title=f"{stage_config.title} — Agent Surface Board")

    def _unauthorized() -> Any:
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{BASIC_AUTH_REALM}"'},
        )

    def _authed(request: Request) -> bool:
        return check_basic_auth(request.headers.get("authorization"), token)

    def _state() -> Dict[str, Any]:
        return build_state_payload(
            display_builder.build_board_display(), stage_config
        )

    @app.get("/")
    def index(request: Request):
        if not _authed(request):
            return _unauthorized()
        return FileResponse(STATIC_DIR / "board.html")

    @app.get("/api/state")
    def state(request: Request):
        if not _authed(request):
            return _unauthorized()
        return JSONResponse(_state())

    @app.post("/api/action")
    async def action(request: Request):
        if not _authed(request):
            return _unauthorized()
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        action_name = str(body.get("action") or "")
        result = dispatch_action(action_handler, action_name, body)
        # The fresh state is returned WITH the result so a click repaints
        # immediately instead of waiting for the next poll tick.
        return JSONResponse({"result": result, "state": _state()})

    @app.get("/api/media/{version_id}")
    def media(version_id: str, request: Request):
        if not _authed(request):
            return _unauthorized()
        version = store.get_version(version_id)
        if not version:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = resolve_media_path(media_dir, version.get("content_ref"))
        if not path:
            return JSONResponse({"error": "not found"}, status_code=404)
        media_type = version.get("content_type") or (
            mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        )
        return FileResponse(path, media_type=media_type)

    @app.get("/api/diff/{artifact_id}")
    def diff(artifact_id: str, request: Request):
        """Compute and return a unified diff between two versions of an artifact.

        Query parameters:
            from: version_id of the earlier version
            to: version_id of the later version

        Returns a JSON object with:
            lines: list of unified diff lines
            old_label: label for the old version
            new_label: label for the new version
            error: error message if something goes wrong
        """
        if not _authed(request):
            return _unauthorized()

        from_version_id = request.query_params.get("from")
        to_version_id = request.query_params.get("to")

        if not from_version_id or not to_version_id:
            return JSONResponse(
                {"error": "missing query parameters: from and to"},
                status_code=400
            )

        # Fetch both versions
        old_version = store.get_version(from_version_id)
        new_version = store.get_version(to_version_id)

        if not old_version:
            return JSONResponse(
                {"error": f"version not found: {from_version_id}"},
                status_code=404
            )
        if not new_version:
            return JSONResponse(
                {"error": f"version not found: {to_version_id}"},
                status_code=404
            )

        # Verify they belong to the same artifact
        if old_version.get("artifact_id") != artifact_id or new_version.get("artifact_id") != artifact_id:
            return JSONResponse(
                {"error": "versions do not match artifact"},
                status_code=400
            )

        # Convert dicts to VersionDisplayRow objects for render_version_content
        old_version_row = VersionDisplayRow(
            version_id=old_version.get("version_id", ""),
            version_num=old_version.get("n", 0),
            content_type=old_version.get("content_type"),
            content=old_version.get("content"),
            content_ref=old_version.get("content_ref"),
            prompt=old_version.get("prompt"),
            note=old_version.get("note"),
            created_by=old_version.get("created_by", "worker"),
            created_at=old_version.get("created_at", ""),
        )
        new_version_row = VersionDisplayRow(
            version_id=new_version.get("version_id", ""),
            version_num=new_version.get("n", 0),
            content_type=new_version.get("content_type"),
            content=new_version.get("content"),
            content_ref=new_version.get("content_ref"),
            prompt=new_version.get("prompt"),
            note=new_version.get("note"),
            created_by=new_version.get("created_by", "worker"),
            created_at=new_version.get("created_at", ""),
        )

        # Extract content from both versions
        old_kind, old_content = render_version_content(old_version_row)
        new_kind, new_content = render_version_content(new_version_row)

        # Only compute diff for text content
        if old_kind != "text" or new_kind != "text":
            return JSONResponse(
                {"error": "diff only supports text content"},
                status_code=400
            )

        old_content = old_content or ""
        new_content = new_content or ""

        # Compute the diff
        old_label = f"v{old_version_row.version_num}"
        new_label = f"v{new_version_row.version_num}"

        diff_lines = diff_versions(
            old_content,
            new_content,
            old_label=old_label,
            new_label=new_label
        )

        return JSONResponse({
            "lines": diff_lines,
            "old_label": old_label,
            "new_label": new_label
        })

    @app.get("/api/graph/{artifact_id}")
    def graph(artifact_id: str, request: Request):
        """Mermaid text for an artifact's immediate upstream/downstream neighbors."""
        if not _authed(request):
            return _unauthorized()

        if not store.get_artifact(artifact_id):
            return JSONResponse({"error": "not found"}, status_code=404)

        return JSONResponse(build_dependency_graph_mermaid(store, artifact_id))

    if STATIC_DIR.is_dir():
        @app.get("/static/{path:path}")
        def static_asset(path: str, request: Request):
            # A hand-rolled route, not app.mount(StaticFiles(...)): mount()
            # serves the whole directory outside the app's own routing, so
            # it never reached _authed() -- CSS/JS leaked unauthenticated.
            # Only board.css/board.html/board.js live here today, but the
            # directory is a mount point for whatever lands in it later.
            if not _authed(request):
                return _unauthorized()
            base = STATIC_DIR.resolve()
            candidate = (base / path).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                return JSONResponse({"error": "not found"}, status_code=404)
            if not candidate.is_file():
                return JSONResponse({"error": "not found"}, status_code=404)
            media_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            return FileResponse(candidate, media_type=media_type)

    return app


def run_web_board(
    store: Store,
    stage_config: StageConfig,
    host: str = "127.0.0.1",
    port: int = 7860,
    media_dir: Optional[str] = None,
    token: Optional[str] = None,
    log_level: str = "warning",
) -> None:
    """Block serving the web board with uvicorn."""
    import uvicorn

    app = create_app(store, stage_config, media_dir=media_dir, token=token)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
