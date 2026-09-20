import json
import pytest
from pathlib import Path

from surface.stages.config import (
    StageConfig,
    StageConfigError,
    StageValidationError,
    DependencyCycleError,
    ActionPermissionError,
    load_stage_config,
    load_preset,
)


def test_movie_config_loading():
    """Verify movie.json preset loads correctly and matches SPEC.md Section 30."""
    config = load_preset("movie")
    assert config.id == "movie"
    assert config.title == "Movie Pipeline"
    assert config.schema_version == "1"

    # Stage order check
    expected_stages = ["script", "shots", "keyframes", "clips", "assembly"]
    assert config.stage_order == expected_stages

    # Verify stage details
    script = config.get_stage("script")
    assert script.artifact_type == "text"
    assert script.depends_on == []
    assert "edit" in script.allowed_actions
    assert script.approval_required is True

    shots = config.get_stage("shots")
    assert shots.artifact_type == "text"
    assert shots.depends_on == ["script"]
    assert shots.approval_required is True

    keyframes = config.get_stage("keyframes")
    assert keyframes.artifact_type == "image"
    assert keyframes.depends_on == ["shots"]
    assert keyframes.generation["provider"] == "image_default"
    assert keyframes.generation["max_parallel"] == 4

    clips = config.get_stage("clips")
    assert clips.artifact_type == "video"
    assert clips.depends_on == ["keyframes"]
    assert clips.generation["provider"] == "video_default"
    assert clips.generation["max_parallel"] == 2

    assembly = config.get_stage("assembly")
    assert assembly.artifact_type == "video"
    assert assembly.depends_on == ["clips"]

    # Verify completion rule
    assert config.completion["require_approved_stages"] == expected_stages


def test_stage_action_permissions():
    """Verify stage action validation and permission checks."""
    config = load_preset("movie")

    # Allowed actions
    config.validate_action("script", "edit")
    config.validate_action("keyframes", "regenerate")

    # Disallowed action
    with pytest.raises(ActionPermissionError):
        config.validate_action("script", "regenerate")

    with pytest.raises(ActionPermissionError):
        config.validate_action("assembly", "cancel")


def test_dependency_cycle_detection():
    """Verify cycle detection in stage dependencies."""
    cycle_data = {
        "id": "cycle_test",
        "stages": [
            {"id": "A", "artifact_type": "text", "depends_on": ["C"], "allowed_actions": ["edit"]},
            {"id": "B", "artifact_type": "text", "depends_on": ["A"], "allowed_actions": ["edit"]},
            {"id": "C", "artifact_type": "text", "depends_on": ["B"], "allowed_actions": ["edit"]},
        ]
    }
    with pytest.raises(DependencyCycleError) as exc_info:
        StageConfig(cycle_data)
    assert "Dependency cycle detected" in str(exc_info.value)

    # Self-dependency
    self_cycle_data = {
        "id": "self_cycle",
        "stages": [
            {"id": "A", "artifact_type": "text", "depends_on": ["A"], "allowed_actions": ["edit"]}
        ]
    }
    with pytest.raises(DependencyCycleError):
        StageConfig(self_cycle_data)


def test_completion_checks():
    """Verify pipeline completion evaluation."""
    config = load_preset("movie")

    approved = {"script", "shots", "keyframes"}
    assert not config.is_completed(approved)

    approved_all = {"script", "shots", "keyframes", "clips", "assembly"}
    assert config.is_completed(approved_all)

    approved_superset = {"script", "shots", "keyframes", "clips", "assembly", "extra"}
    assert config.is_completed(approved_superset)


def test_budget_validation():
    """Verify budget parsing and limit checks."""
    budget_data = {
        "id": "budget_test",
        "budget": {
            "stage_usd": 10.0,
            "project_usd": 50.0,
            "confirm_above_usd": 5.0
        },
        "stages": [
            {"id": "A", "artifact_type": "text", "allowed_actions": ["edit"]}
        ]
    }
    config = StageConfig(budget_data)

    res1 = config.validate_budget_limit(estimated_cost=2.0, current_project_cost=10.0, current_stage_cost=2.0)
    assert res1["exceeds_project_budget"] is False
    assert res1["exceeds_stage_budget"] is False
    assert res1["requires_confirmation"] is False

    res2 = config.validate_budget_limit(estimated_cost=6.0, current_project_cost=10.0, current_stage_cost=2.0)
    assert res2["exceeds_project_budget"] is False
    assert res2["exceeds_stage_budget"] is False
    assert res2["requires_confirmation"] is True

    res3 = config.validate_budget_limit(estimated_cost=10.0, current_project_cost=45.0, current_stage_cost=2.0)
    assert res3["exceeds_project_budget"] is True
    assert res3["exceeds_stage_budget"] is True
    assert res3["requires_confirmation"] is True


def test_json_schema_forms_validation():
    """Verify JSON Schema forms stage support."""
    form_data = {
        "id": "form_test",
        "stages": [
            {
                "id": "questionnaire",
                "artifact_type": "form",
                "allowed_actions": ["edit", "approve"],
                "form_schema": {
                    "type": "object",
                    "required": ["aspect_ratio"],
                    "properties": {
                        "aspect_ratio": {
                            "type": "string",
                            "enum": ["16:9", "9:16", "1:1"]
                        }
                    }
                }
            }
        ]
    }
    config = StageConfig(form_data)
    stage = config.get_stage("questionnaire")
    assert stage.form_schema["type"] == "object"
    assert "aspect_ratio" in stage.form_schema["properties"]


def test_invalid_configs():
    """Test various invalid configuration schema errors."""
    # Missing ID
    with pytest.raises(StageValidationError):
        StageConfig({"stages": [{"id": "A"}]})

    # Unknown dependency
    with pytest.raises(StageValidationError):
        StageConfig({
            "id": "test",
            "stages": [
                {"id": "A", "artifact_type": "text", "depends_on": ["NONEXISTENT"], "allowed_actions": ["edit"]}
            ]
        })

    # Unknown action
    with pytest.raises(StageValidationError):
        StageConfig({
            "id": "test",
            "stages": [
                {"id": "A", "artifact_type": "text", "allowed_actions": ["invalid_action"]}
            ]
        })

    # Invalid artifact type
    with pytest.raises(StageValidationError):
        StageConfig({
            "id": "test",
            "stages": [
                {"id": "A", "artifact_type": "invalid_type", "allowed_actions": ["edit"]}
            ]
        })
