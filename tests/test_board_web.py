"""Tests for the FastAPI web board renderer (surface.board_web).

The point of this renderer is that it is a *second* view over the same
logic: DisplayModelBuilder / ActionHandler are imported unchanged. So these
tests concentrate on the three things that are genuinely new --
  * the honest queued/working state that the Gradio header could not show,
  * the message box being bound to a real artifact instead of artifact_id
    null,
  * the HTTP surface (auth, state, action, media) --
rather than re-testing the shared display model, which test_board.py owns.
"""
import json

import pytest

from surface.board import DisplayModelBuilder, format_board_status, format_queued_note
from surface.board_web import (
    build_state_payload,
    check_basic_auth,
    dispatch_action,
    resolve_media_path,
)
from surface.board import ActionHandler
from surface.stages.config import StageConfig, load_preset
from surface.store import Store

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def poem_store():
    store = Store(":memory:")
    store.create_artifact(id="poem_001", stage="poem", title="A Poem")
    store.put_version(
        "poem_001",
        content="line one\nline two\n\nline four",
        created_by="worker",
        select=True,
    )
    return store


@pytest.fixture
def poem_config():
    return load_preset("poem")


@pytest.fixture
def client(poem_store, poem_config):
    from surface.board_web import create_app

    return TestClient(create_app(poem_store, poem_config))


# =============================================================================
# Store-level derived state: the actual bug this work exists to fix
# =============================================================================


def test_list_active_events_covers_pending_and_processing(poem_store):
    poem_store.enqueue_event(type="revise", payload={"note": "a"}, artifact_id="poem_001")
    poem_store.enqueue_event(type="revise", payload={"note": "b"}, artifact_id="poem_001")

    active = poem_store.list_active_events("poem_001")
    assert len(active) == 2
    assert {e["status"] for e in active} == {"pending"}

    # Claiming one moves it to 'processing' -- still active, differently so.
    poem_store.claim_next_event("w1")
    counts = poem_store.count_active_events("poem_001")
    assert counts == {"pending": 1, "processing": 1, "total": 2}


def test_list_active_events_ignores_resolved_events(poem_store):
    event = poem_store.enqueue_event(
        type="revise", payload={"note": "a"}, artifact_id="poem_001"
    )
    poem_store.claim_next_event("w1")
    poem_store.ack_event(event["id"])
    assert poem_store.list_active_events("poem_001") == []
    assert poem_store.count_active_events()["total"] == 0


def test_list_active_events_scopes_by_artifact(poem_store):
    poem_store.create_artifact(id="poem_002", stage="poem", title="Other")
    poem_store.enqueue_event(type="revise", payload={"note": "a"}, artifact_id="poem_001")
    poem_store.enqueue_event(type="revise", payload={"note": "b"}, artifact_id="poem_002")

    assert len(poem_store.list_active_events("poem_001")) == 1
    assert len(poem_store.list_active_events()) == 2


# =============================================================================
# Honest status: "Ready" must not survive an unclaimed click
# =============================================================================


def test_board_status_is_ready_only_when_the_inbox_is_empty(poem_store, poem_config):
    builder = DisplayModelBuilder(poem_store, poem_config)
    assert builder.build_board_display().status_message == "Ready"

    poem_store.enqueue_event(type="revise", payload={"note": "x"}, artifact_id="poem_001")
    display = builder.build_board_display()
    assert display.status_message == "1 queued"
    assert format_board_status(display) == "1 queued"

    poem_store.claim_next_event("w1")
    assert builder.build_board_display().status_message == "1 working"


def test_card_reports_its_own_queued_work(poem_store, poem_config):
    builder = DisplayModelBuilder(poem_store, poem_config)
    card = builder.build_board_display().stages[0].artifacts[0]
    assert card.active_event_count == 0
    assert format_queued_note(card) is None

    poem_store.enqueue_event(type="revise", payload={"note": "x"}, artifact_id="poem_001")
    card = builder.build_board_display().stages[0].artifacts[0]
    assert card.pending_event_count == 1
    assert card.processing_event_count == 0
    note = format_queued_note(card)
    assert "queued" in note and "waiting for a worker" in note

    poem_store.claim_next_event("w1")
    card = builder.build_board_display().stages[0].artifacts[0]
    assert card.processing_event_count == 1
    assert "worker is on it" in format_queued_note(card)


def test_queued_state_reaches_the_state_endpoint(client, poem_store):
    body = client.get("/api/state").json()
    assert body["status_message"] == "Ready"
    assert body["busy"] is False
    assert body["stages"][0]["artifacts"][0]["queued_note"] is None

    poem_store.enqueue_event(type="revise", payload={"note": "x"}, artifact_id="poem_001")

    body = client.get("/api/state").json()
    assert body["status_message"] == "1 queued"
    assert body["busy"] is True
    assert body["pending_event_count"] == 1
    assert body["stages"][0]["queued_count"] == 1
    card = body["stages"][0]["artifacts"][0]
    assert card["busy"] is True
    assert "waiting for a worker" in card["queued_note"]


# =============================================================================
# The unbound message box
# =============================================================================


def test_message_without_an_artifact_is_refused(poem_store, poem_config):
    handler = ActionHandler(poem_store, poem_config)
    result = dispatch_action(handler, "message", {"text": "hello"})
    assert result["ok"] is False
    assert "artifact" in result["error"]
    # Nothing was written to the inbox.
    assert poem_store.list_active_events() == []


def test_message_is_bound_to_the_artifact_in_view(client, poem_store):
    res = client.post(
        "/api/action",
        json={"action": "message", "artifact_id": "poem_001", "text": "tighten it"},
    )
    assert res.json()["result"]["ok"] is True
    events = poem_store.list_active_events()
    assert len(events) == 1
    assert events[0]["artifact_id"] == "poem_001"
    assert events[0]["payload"]["text"] == "tighten it"


# =============================================================================
# State payload
# =============================================================================


def test_state_payload_keeps_text_content_verbatim(client):
    card = client.get("/api/state").json()["stages"][0]["artifacts"][0]
    assert card["content_kind"] == "text"
    # Line breaks survive: the client renders this into a pre-wrap text node.
    assert card["content_text"] == "line one\nline two\n\nline four"


def test_state_payload_exposes_form_fields(poem_store):
    config = StageConfig(
        {
            "schema_version": "1",
            "id": "q",
            "title": "Questionnaire",
            "stages": [
                {
                    "id": "q",
                    "artifact_type": "form",
                    "allowed_actions": ["edit", "approve"],
                    "form_schema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string", "title": "Your name"},
                            "size": {"type": "string", "enum": ["S", "M", "L"]},
                        },
                    },
                }
            ],
        }
    )
    store = Store(":memory:")
    store.create_artifact(id="q_001", stage="q", title="Form")
    store.put_version(
        "q_001", content=json.dumps({"name": "Ada"}), created_by="user", select=True
    )

    payload = build_state_payload(
        DisplayModelBuilder(store, config).build_board_display(), config
    )
    card = payload["stages"][0]["artifacts"][0]
    assert card["content_kind"] == "form"
    names = [f["name"] for f in card["form_fields"]]
    assert names == ["name", "size"]
    assert card["form_fields"][0]["value"] == "Ada"
    assert card["form_fields"][0]["required"] is True
    assert card["form_fields"][1]["enum"] == ["S", "M", "L"]


def test_state_payload_lists_every_stage_of_a_multi_stage_config():
    config = load_preset("movie")
    store = Store(":memory:")
    store.create_artifact(id="script_001", stage="script", title="Script")
    store.create_artifact(id="kf_001", stage="keyframes", title="Keyframe")

    payload = build_state_payload(
        DisplayModelBuilder(store, config).build_board_display(), config
    )
    assert [s["stage_id"] for s in payload["stages"]] == [
        "script",
        "shots",
        "keyframes",
        "clips",
        "assembly",
    ]
    assert payload["stages"][0]["total_count"] == 1
    assert payload["stages"][1]["total_count"] == 0


def test_media_payload_points_at_the_media_route():
    config = load_preset("movie")
    store = Store(":memory:")
    store.create_artifact(id="kf_001", stage="keyframes", title="Keyframe")
    version = store.put_version(
        "kf_001",
        content_type="image/png",
        content_ref="frames/a.png",
        created_by="worker",
        select=True,
    )
    payload = build_state_payload(
        DisplayModelBuilder(store, config).build_board_display(), config
    )
    card = payload["stages"][2]["artifacts"][0]
    assert card["content_kind"] == "image"
    assert card["media_url"] == f"/api/media/{version['version_id']}"


# =============================================================================
# Actions over HTTP
# =============================================================================


def test_approve_updates_the_store_and_returns_fresh_state(client, poem_store):
    res = client.post("/api/action", json={"action": "approve", "artifact_id": "poem_001"})
    body = res.json()
    assert body["result"]["ok"] is True
    assert poem_store.get_artifact("poem_001")["status"] == "approved"
    # The same response already carries the repainted board.
    assert body["state"]["stages"][0]["artifacts"][0]["status"] == "approved"


def test_revise_enqueues_and_immediately_shows_as_queued(client):
    body = client.post(
        "/api/action",
        json={"action": "revise", "artifact_id": "poem_001", "note": "more rain"},
    ).json()
    assert body["result"]["ok"] is True
    assert body["state"]["status_message"] == "1 queued"
    assert body["state"]["stages"][0]["artifacts"][0]["pending_event_count"] == 1


def test_empty_revise_is_rejected(client, poem_store):
    body = client.post(
        "/api/action", json={"action": "revise", "artifact_id": "poem_001", "note": "   "}
    ).json()
    assert body["result"]["ok"] is False
    assert poem_store.list_active_events() == []


def test_unknown_action_is_rejected(client):
    body = client.post(
        "/api/action", json={"action": "detonate", "artifact_id": "poem_001"}
    ).json()
    assert body["result"]["ok"] is False
    assert "Unknown action" in body["result"]["error"]


def test_disallowed_action_is_gated_by_stage_config(client, poem_store):
    # lock/unlock/approve/select_version go through
    # ActionHandler._validate_action_allowed(); poem.json does not list
    # "lock", so this must be refused.
    body_lock = client.post(
        "/api/action", json={"action": "lock", "artifact_id": "poem_001"}
    ).json()
    assert body_lock["result"]["ok"] is False
    assert "not allowed" in body_lock["result"]["error"]


def test_edit_is_not_stage_gated_known_gap(client, poem_store):
    """Documents a real gap, does not celebrate it.

    enqueue_edit/enqueue_revise/enqueue_regenerate/enqueue_reopen/
    enqueue_cancel/enqueue_message never call _validate_action_allowed --
    only approve/lock/unlock/select_version do (surface/board.py). poem.json
    does not list "edit" in allowed_actions, so the UI never shows an Edit
    button for it, but this HTTP endpoint accepts it anyway and it lands in
    the inbox. That's pre-existing ActionHandler behavior this renderer
    inherits, now reachable over HTTP where the old Gradio board only ever
    called it from a button that didn't exist for a disallowed action. If
    this test starts failing because someone added the missing gating,
    that's a fix, not a regression -- delete this test and fold the
    assertion into test_disallowed_action_is_gated_by_stage_config instead.
    """
    body = client.post(
        "/api/action",
        json={"action": "edit", "artifact_id": "poem_001", "content": "new"},
    ).json()
    assert body["result"]["ok"] is True
    assert any(e["type"] == "edit" for e in poem_store.list_active_events("poem_001"))


def test_select_version_conflict_is_surfaced_not_swallowed(client, poem_store):
    v2 = poem_store.put_version("poem_001", content="second", created_by="worker", select=True)
    v3 = poem_store.put_version("poem_001", content="third", created_by="worker", select=True)

    body = client.post(
        "/api/action",
        json={
            "action": "select_version",
            "artifact_id": "poem_001",
            "version_id": v2["version_id"],
            # Stale: v3 is selected now.
            "expected_selected_version_id": "ver_stale",
        },
    ).json()
    assert body["result"]["ok"] is False
    assert "Selection conflict" in body["result"]["message"]
    # No side effect at the data layer.
    assert poem_store.get_artifact("poem_001")["selected_version_id"] == v3["version_id"]


# =============================================================================
# Auth and media constraints
# =============================================================================


def test_basic_auth_accepts_the_runtime_token():
    import base64

    token = "s3cret-token"
    header = "Basic " + base64.b64encode(f"surface:{token}".encode()).decode()
    assert check_basic_auth(header, token) is True
    assert check_basic_auth(header, "other-token") is False
    assert check_basic_auth(None, token) is False
    assert check_basic_auth("Bearer x", token) is False
    assert check_basic_auth("Basic not-base64!!", token) is False
    # No token configured == auth disabled.
    assert check_basic_auth(None, None) is True


def test_routes_are_behind_auth_when_a_token_is_set(poem_store, poem_config):
    from surface.board_web import create_app

    token = "tok-123"
    authed = TestClient(create_app(poem_store, poem_config, token=token))
    assert authed.get("/api/state").status_code == 401
    assert authed.get("/").status_code == 401
    assert authed.post("/api/action", json={"action": "approve"}).status_code == 401

    ok = authed.get("/api/state", auth=("surface", token))
    assert ok.status_code == 200
    assert ok.json()["status_message"] == "Ready"


def test_media_path_traversal_is_refused(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "ok.png").write_bytes(b"\x89PNG")
    (tmp_path / "secret.txt").write_text("SENSITIVE")

    assert resolve_media_path(str(media), "ok.png") == (media / "ok.png").resolve()
    assert resolve_media_path(str(media), "../secret.txt") is None
    assert resolve_media_path(str(media), "/etc/passwd") is None
    assert resolve_media_path(str(media), "missing.png") is None
    assert resolve_media_path(None, "relative.png") is None


def test_media_route_serves_a_file_inside_media_dir(tmp_path):
    from surface.board_web import create_app

    media = tmp_path / "media"
    media.mkdir()
    (media / "a.png").write_bytes(b"\x89PNG-data")

    config = load_preset("movie")
    store = Store(":memory:")
    store.create_artifact(id="kf_001", stage="keyframes", title="Keyframe")
    version = store.put_version(
        "kf_001", content_type="image/png", content_ref="a.png",
        created_by="worker", select=True,
    )
    client = TestClient(create_app(store, config, media_dir=str(media)))

    res = client.get(f"/api/media/{version['version_id']}")
    assert res.status_code == 200
    assert res.content == b"\x89PNG-data"

    assert client.get("/api/media/ver_nope").status_code == 404


def test_media_route_refuses_an_escaping_content_ref(tmp_path):
    from surface.board_web import create_app

    media = tmp_path / "media"
    media.mkdir()
    (tmp_path / "secret.txt").write_text("SENSITIVE")

    config = load_preset("movie")
    store = Store(":memory:")
    store.create_artifact(id="kf_001", stage="keyframes", title="Keyframe")
    version = store.put_version(
        "kf_001", content_type="image/png", content_ref="../secret.txt",
        created_by="worker", select=True,
    )
    client = TestClient(create_app(store, config, media_dir=str(media)))
    assert client.get(f"/api/media/{version['version_id']}").status_code == 404


# =============================================================================
# The page itself
# =============================================================================


def test_index_and_static_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "/static/board.js" in page.text

    css = client.get("/static/board.css")
    assert css.status_code == 200
    # The two rules that were regressions once already.
    assert "white-space: pre-wrap" in css.text
    assert "button:disabled" in css.text

    js = client.get("/static/board.js")
    assert js.status_code == 200


def test_mermaid_markdown_content_is_passed_through():
    """Verify that markdown with mermaid fenced blocks is correctly included in state payload.

    The mermaid rendering happens client-side in board.js, so the server just needs to
    pass the content through unchanged. This test verifies that.
    """
    config = load_preset("poem")
    store = Store(":memory:")

    mermaid_content = """# Architecture

Here's a diagram:

```mermaid
graph TD
    A[Start] --> B[Process]
    B --> C{Decision}
    C -->|Yes| D[End]
```

And some text after.
"""

    store.create_artifact(id="poem_001", stage="poem", title="Mermaid Diagram")
    store.put_version(
        "poem_001",
        content=mermaid_content,
        content_type="text/markdown",
        created_by="test",
        select=True,
    )

    payload = build_state_payload(
        DisplayModelBuilder(store, config).build_board_display(), config
    )

    # Verify the artifact is in the payload
    assert len(payload["stages"][0]["artifacts"]) == 1
    card = payload["stages"][0]["artifacts"][0]
    assert card["title"] == "Mermaid Diagram"
    assert card["content_kind"] == "text"

    # Verify the mermaid content is included unchanged (client-side code will render it)
    assert "```mermaid" in card["content_text"]
    assert "graph TD" in card["content_text"]
    assert "And some text after" in card["content_text"]


def test_plain_text_without_mermaid_is_passed_through():
    """Verify that plain text content without mermaid blocks works correctly."""
    config = load_preset("poem")
    store = Store(":memory:")

    plain_content = """This is plain text.
It has multiple lines.
No special formatting here."""

    store.create_artifact(id="poem_001", stage="poem", title="Plain Text")
    store.put_version(
        "poem_001",
        content=plain_content,
        content_type="text/plain",
        created_by="test",
        select=True,
    )

    payload = build_state_payload(
        DisplayModelBuilder(store, config).build_board_display(), config
    )

    card = payload["stages"][0]["artifacts"][0]
    assert card["title"] == "Plain Text"
    assert card["content_kind"] == "text"
    assert card["content_text"] == plain_content


# =============================================================================
# Diff endpoint
# =============================================================================


def test_diff_between_two_versions(client, poem_store):
    v1 = poem_store.put_version(
        "poem_001",
        content="line one\nline two\n",
        created_by="test",
        select=True,
    )
    v2 = poem_store.put_version(
        "poem_001",
        content="line one\nline two modified\n",
        created_by="test",
        select=True,
    )

    res = client.get(
        f"/api/diff/poem_001?from={v1['version_id']}&to={v2['version_id']}"
    )
    assert res.status_code == 200
    body = res.json()
    assert "lines" in body
    assert "old_label" in body
    assert "new_label" in body
    # The labels contain the version numbers
    assert body["old_label"].startswith("v")
    assert body["new_label"].startswith("v")
    # Check that the diff contains the actual changes
    diff_text = "\n".join(body["lines"])
    assert "line two modified" in diff_text


def test_diff_with_missing_query_params(client):
    res = client.get("/api/diff/poem_001")
    assert res.status_code == 400
    assert "missing query parameters" in res.json()["error"]


def test_diff_with_nonexistent_version(client):
    res = client.get("/api/diff/poem_001?from=nonexistent&to=nonexistent")
    assert res.status_code == 404
    assert "not found" in res.json()["error"]


def test_diff_with_versions_from_different_artifacts(poem_store, client):
    # Create another artifact
    poem_store.create_artifact(id="poem_002", stage="poem", title="Other Poem")
    v1 = poem_store.put_version(
        "poem_001",
        content="content 1\n",
        created_by="test",
        select=True,
    )
    v2 = poem_store.put_version(
        "poem_002",
        content="content 2\n",
        created_by="test",
        select=True,
    )

    res = client.get(
        f"/api/diff/poem_001?from={v1['version_id']}&to={v2['version_id']}"
    )
    assert res.status_code == 400
    assert "do not match artifact" in res.json()["error"]


def test_diff_only_works_with_text_content(poem_store, client):
    config = load_preset("movie")
    store = Store(":memory:")
    store.create_artifact(id="kf_001", stage="keyframes", title="Keyframe")
    v1 = store.put_version(
        "kf_001",
        content_type="image/png",
        content_ref="frames/a.png",
        created_by="test",
        select=True,
    )
    v2 = store.put_version(
        "kf_001",
        content_type="image/png",
        content_ref="frames/b.png",
        created_by="test",
        select=True,
    )

    from surface.board_web import create_app

    test_client = TestClient(create_app(store, config))
    res = test_client.get(
        f"/api/diff/kf_001?from={v1['version_id']}&to={v2['version_id']}"
    )
    assert res.status_code == 400
    assert "only supports text content" in res.json()["error"]
