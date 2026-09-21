import pytest
from surface.stages.config import (
    StageConfig,
    DependencyCycleError,
    load_preset,
)


class TestQuestionnairePreset:
    """Tests for questionnaire.json preset."""

    def test_questionnaire_loads_successfully(self):
        """Verify questionnaire.json preset loads and validates."""
        config = load_preset("questionnaire")
        assert config.id == "questionnaire"
        assert config.title == "Project Kickoff Questionnaire"
        assert config.schema_version == "1"

    def test_questionnaire_stages(self):
        """Verify questionnaire stage structure."""
        config = load_preset("questionnaire")
        assert config.stage_order == ["intake", "summary"]

        intake = config.get_stage("intake")
        assert intake.artifact_type == "form"
        assert intake.depends_on == []
        assert intake.approval_required is True
        for action in ("edit", "revise", "approve", "reopen"):
            assert action in intake.allowed_actions

        summary = config.get_stage("summary")
        assert summary.artifact_type == "text"
        assert summary.depends_on == ["intake"]
        assert summary.approval_required is True

    def test_questionnaire_form_schema(self):
        """Verify questionnaire form_schema is present and valid."""
        config = load_preset("questionnaire")
        intake = config.get_stage("intake")
        assert intake.form_schema is not None
        assert intake.form_schema["type"] == "object"
        properties = intake.form_schema["properties"]
        for field in ("project_name", "goal", "target_users", "timeline",
                      "budget", "tech_stack", "biggest_risk"):
            assert field in properties
            assert properties[field]["title"]

        # The two enum fields constrain their answers; the rest are free text.
        assert properties["timeline"]["enum"] == [
            "1-2 weeks", "1 month", "2-3 months", "6+ months"
        ]
        assert properties["budget"]["enum"] == [
            "< $5k", "$5k-$20k", "$20k-$100k", "$100k+"
        ]

    def test_questionnaire_completion_rule(self):
        """Verify questionnaire completion rules."""
        config = load_preset("questionnaire")
        assert config.completion["require_approved_stages"] == ["intake", "summary"]
        assert config.is_completed({"intake", "summary"})
        # summary depends on intake, so intake alone must not complete it
        assert not config.is_completed({"intake"})


class TestPlanReviewPreset:
    """Tests for plan_review.json preset."""

    def test_plan_review_loads_successfully(self):
        """Verify plan_review.json preset loads and validates."""
        config = load_preset("plan_review")
        assert config.id == "plan_review"
        assert config.title == "Plan Review Pipeline"
        assert config.schema_version == "1"

    def test_plan_review_stages_order(self):
        """Verify plan_review stage order and structure."""
        config = load_preset("plan_review")
        expected_order = ["draft", "sections", "risks", "final"]
        assert config.stage_order == expected_order

    def test_plan_review_dependencies(self):
        """Verify plan_review dependency structure."""
        config = load_preset("plan_review")

        draft = config.get_stage("draft")
        assert draft.depends_on == []

        sections = config.get_stage("sections")
        assert sections.depends_on == ["draft"]

        risks = config.get_stage("risks")
        assert risks.depends_on == ["sections"]

        final = config.get_stage("final")
        assert set(final.depends_on) == {"risks", "sections"}

    def test_plan_review_stage_actions(self):
        """Verify plan_review allowed actions per stage."""
        config = load_preset("plan_review")

        draft = config.get_stage("draft")
        assert set(draft.allowed_actions) == {"edit", "revise", "approve"}

        sections = config.get_stage("sections")
        assert "reopen" in sections.allowed_actions

        final = config.get_stage("final")
        assert "message" in final.allowed_actions

    def test_plan_review_no_cycles(self):
        """Verify plan_review has no dependency cycles."""
        config = load_preset("plan_review")
        # If there were cycles, StageConfig init would raise DependencyCycleError
        # The fact that we loaded it successfully means no cycles exist
        assert len(config.stages) == 4

    def test_plan_review_completion_rule(self):
        """Verify plan_review completion rules."""
        config = load_preset("plan_review")
        required = set(config.completion["require_approved_stages"])
        assert required == {"draft", "sections", "risks", "final"}

        # Check is_completed logic
        assert not config.is_completed({"draft", "sections"})
        assert config.is_completed({"draft", "sections", "risks", "final"})


class TestDocumentReviewPreset:
    """Tests for document_review.json preset."""

    def test_document_review_loads_successfully(self):
        """Verify document_review.json preset loads and validates."""
        config = load_preset("document_review")
        assert config.id == "document_review"
        assert config.title == "Document Review Pipeline"
        assert config.schema_version == "1"

    def test_document_review_stages(self):
        """Verify document_review stage structure."""
        config = load_preset("document_review")
        assert config.stage_order == ["sections", "findings"]

    def test_document_review_dependencies(self):
        """Verify document_review dependency structure."""
        config = load_preset("document_review")

        sections = config.get_stage("sections")
        assert sections.depends_on == []

        findings = config.get_stage("findings")
        assert findings.depends_on == ["sections"]

    def test_document_review_generation_block(self):
        """Verify document_review findings stage has generation block."""
        config = load_preset("document_review")
        findings = config.get_stage("findings")

        assert findings.generation is not None
        assert findings.generation["provider"] == "analysis"
        assert findings.generation["max_parallel"] == 2

    def test_document_review_actions(self):
        """Verify document_review allowed actions."""
        config = load_preset("document_review")

        sections = config.get_stage("sections")
        assert "edit" in sections.allowed_actions
        assert "approve" in sections.allowed_actions

        findings = config.get_stage("findings")
        assert "regenerate" in findings.allowed_actions
        assert "select_version" in findings.allowed_actions
        assert "cancel" in findings.allowed_actions

    def test_document_review_completion_rule(self):
        """Verify document_review completion rules."""
        config = load_preset("document_review")
        required = set(config.completion["require_approved_stages"])
        assert required == {"sections", "findings"}


class TestDiffReviewPreset:
    """Tests for diff_review.json preset."""

    def test_diff_review_loads_successfully(self):
        """Verify diff_review.json preset loads and validates."""
        config = load_preset("diff_review")
        assert config.id == "diff_review"
        assert config.title == "Diff Review Pipeline"
        assert config.schema_version == "1"

    def test_diff_review_stages(self):
        """Verify diff_review stage structure."""
        config = load_preset("diff_review")
        assert config.stage_order == ["changes"]

        changes = config.get_stage("changes")
        assert changes.artifact_type == "diff"
        assert changes.depends_on == []
        assert changes.approval_required is True

    def test_diff_review_actions(self):
        """Verify diff_review allowed actions."""
        config = load_preset("diff_review")
        changes = config.get_stage("changes")

        # Diffs are reviewed/approved, not edited
        assert "approve" in changes.allowed_actions
        assert "reopen" in changes.allowed_actions
        assert "message" in changes.allowed_actions
        assert "edit" not in changes.allowed_actions
        assert "regenerate" not in changes.allowed_actions

    def test_diff_review_no_generation(self):
        """Verify diff_review has no generation block."""
        config = load_preset("diff_review")
        changes = config.get_stage("changes")
        assert changes.generation is None

    def test_diff_review_completion_rule(self):
        """Verify diff_review completion rules."""
        config = load_preset("diff_review")
        assert config.completion["require_approved_stages"] == ["changes"]
        assert config.is_completed({"changes"})


class TestGenericMediaPipelinePreset:
    """Tests for generic_media_pipeline.json preset."""

    def test_generic_media_pipeline_loads_successfully(self):
        """Verify generic_media_pipeline.json preset loads and validates."""
        config = load_preset("generic_media_pipeline")
        assert config.id == "generic_media_pipeline"
        assert config.title == "Generic Media Pipeline"
        assert config.schema_version == "1"

    def test_generic_media_pipeline_stages_order(self):
        """Verify generic_media_pipeline stage order."""
        config = load_preset("generic_media_pipeline")
        expected_order = ["prompts", "images", "videos"]
        assert config.stage_order == expected_order

    def test_generic_media_pipeline_artifact_types(self):
        """Verify generic_media_pipeline artifact types."""
        config = load_preset("generic_media_pipeline")

        prompts = config.get_stage("prompts")
        assert prompts.artifact_type == "text"

        images = config.get_stage("images")
        assert images.artifact_type == "image"

        videos = config.get_stage("videos")
        assert videos.artifact_type == "video"

    def test_generic_media_pipeline_dependencies(self):
        """Verify generic_media_pipeline dependency structure."""
        config = load_preset("generic_media_pipeline")

        prompts = config.get_stage("prompts")
        assert prompts.depends_on == []

        images = config.get_stage("images")
        assert images.depends_on == ["prompts"]

        videos = config.get_stage("videos")
        assert videos.depends_on == ["images"]

    def test_generic_media_pipeline_generation_blocks(self):
        """Verify generic_media_pipeline generation blocks."""
        config = load_preset("generic_media_pipeline")

        prompts = config.get_stage("prompts")
        assert prompts.generation is None

        images = config.get_stage("images")
        assert images.generation is not None
        assert images.generation["provider"] == "image_default"
        assert images.generation["max_parallel"] == 4

        videos = config.get_stage("videos")
        assert videos.generation is not None
        assert videos.generation["provider"] == "video_default"
        assert videos.generation["max_parallel"] == 2

    def test_generic_media_pipeline_actions(self):
        """Verify generic_media_pipeline allowed actions."""
        config = load_preset("generic_media_pipeline")

        prompts = config.get_stage("prompts")
        assert set(prompts.allowed_actions) == {"edit", "revise", "approve"}

        images = config.get_stage("images")
        assert "regenerate" in images.allowed_actions
        assert "select_version" in images.allowed_actions
        assert "cancel" in images.allowed_actions

        videos = config.get_stage("videos")
        assert "regenerate" in videos.allowed_actions
        assert "cancel" in videos.allowed_actions

    def test_generic_media_pipeline_completion_rule(self):
        """Verify generic_media_pipeline completion rules."""
        config = load_preset("generic_media_pipeline")
        required = set(config.completion["require_approved_stages"])
        assert required == {"prompts", "images", "videos"}

        # Partial approval should not complete
        assert not config.is_completed({"prompts", "images"})

        # Full approval should complete
        assert config.is_completed({"prompts", "images", "videos"})

    def test_generic_media_pipeline_no_cycles(self):
        """Verify generic_media_pipeline has no dependency cycles."""
        config = load_preset("generic_media_pipeline")
        # If there were cycles, StageConfig init would raise DependencyCycleError
        # The fact that we loaded it successfully means no cycles exist
        assert len(config.stages) == 3


class TestAllPresetsIntegration:
    """Integration tests across all presets."""

    def test_all_presets_have_required_fields(self):
        """Verify all presets have required top-level fields."""
        preset_names = ["questionnaire", "plan_review", "document_review", "diff_review", "generic_media_pipeline"]

        for name in preset_names:
            config = load_preset(name)
            assert config.schema_version == "1"
            assert config.id == name
            assert config.title is not None
            assert len(config.stages) > 0
            assert len(config.stage_order) > 0

    def test_all_presets_have_valid_completion_rules(self):
        """Verify all presets have valid completion rules."""
        preset_names = ["questionnaire", "plan_review", "document_review", "diff_review", "generic_media_pipeline"]

        for name in preset_names:
            config = load_preset(name)
            required_stages = set(config.completion.get("require_approved_stages", []))
            assert len(required_stages) > 0

            # All required stages should exist
            for stage_id in required_stages:
                assert stage_id in config.stages

    def test_all_presets_no_dependency_cycles(self):
        """Verify no preset has dependency cycles."""
        preset_names = ["questionnaire", "plan_review", "document_review", "diff_review", "generic_media_pipeline"]

        for name in preset_names:
            # If there were cycles, this would raise DependencyCycleError
            config = load_preset(name)
            assert config is not None

    def test_all_presets_valid_artifact_types(self):
        """Verify all presets use valid artifact types."""
        preset_names = ["questionnaire", "plan_review", "document_review", "diff_review", "generic_media_pipeline"]
        valid_types = {"text", "image", "video", "audio", "form", "file", "diff"}

        for name in preset_names:
            config = load_preset(name)
            for stage in config.stages.values():
                assert stage.artifact_type in valid_types

    def test_all_presets_valid_actions(self):
        """Verify all presets use valid actions."""
        preset_names = ["questionnaire", "plan_review", "document_review", "diff_review", "generic_media_pipeline"]

        for name in preset_names:
            config = load_preset(name)
            for stage_id, stage in config.stages.items():
                for action in stage.allowed_actions:
                    # This will raise ActionPermissionError if invalid
                    config.validate_action(stage_id, action)

    def test_all_presets_dependencies_resolve(self):
        """Verify all preset dependencies reference existing stages."""
        preset_names = ["questionnaire", "plan_review", "document_review", "diff_review", "generic_media_pipeline"]

        for name in preset_names:
            config = load_preset(name)
            for stage_id, stage in config.stages.items():
                for dep_id in stage.depends_on:
                    assert dep_id in config.stages, \
                        f"Preset {name}: stage {stage_id} depends on unknown stage {dep_id}"
