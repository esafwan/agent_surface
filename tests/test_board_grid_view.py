"""End-to-end display-model test for the `grid` board view.

Loads a stage config that declares `board_view` in object form, seeds an
image artifact with two versions through the real Store API, and checks the
state payload that board_web serves to the browser.
"""
import json
import pathlib

from surface.board import DisplayModelBuilder
from surface.board_web import build_state_payload
from surface.stages.config import load_stage_config
from surface.store import Store

GRID_CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "surface" / "stages" / "image_grid_example.json"
)

GRID_VIEW = {
    "type": "grid",
    "group_by": "status",
    "media_field": "media_url",
    "title_field": "title",
}


def _grid_store():
    store = Store(":memory:")
    store.create_artifact(
        id="shot_001", stage="shots", title="Sunset over the harbour", status="review"
    )
    store.put_version(
        "shot_001",
        content_type="image/png",
        content_ref="shots/shot_001_v1.png",
        prompt="harbour at sunset, wide",
        created_by="worker",
        select=True,
    )
    second = store.put_version(
        "shot_001",
        content_type="image/png",
        content_ref="shots/shot_001_v2.png",
        prompt="harbour at sunset, close",
        created_by="worker",
        select=True,
    )
    return store, second


def test_grid_stage_config_declares_board_view_object():
    config = load_stage_config(GRID_CONFIG_PATH)
    stage = config.get_stage("shots")
    assert stage.artifact_type == "image"
    assert stage.board_view == GRID_VIEW


def test_grid_view_state_payload_end_to_end():
    config = load_stage_config(GRID_CONFIG_PATH)
    store, second = _grid_store()

    payload = build_state_payload(
        DisplayModelBuilder(store, config).build_board_display(), config
    )
    # The payload must survive a JSON round trip exactly as the server sends it.
    payload = json.loads(json.dumps(payload))

    stage = payload["stages"][0]
    assert stage["stage_id"] == "shots"
    assert stage["board_view"] == GRID_VIEW

    card = stage["artifacts"][0]
    assert card["title"] == "Sunset over the harbour"

    # Two versions, each with a distinct, non-empty version id.
    versions = card["all_versions"]
    assert len(versions) >= 2
    version_ids = [v["version_id"] for v in versions]
    assert all(version_ids)
    assert len(set(version_ids)) == len(version_ids)
    assert second["version_id"] in version_ids

    # The selected version drives the media fields.
    assert card["content_kind"] == "image"
    assert card["selected_version"]["version_id"] == second["version_id"]
    assert card["media_url"] == f"/api/media/{second['version_id']}"
    assert card["media_ref"] == "shots/shot_001_v2.png"
