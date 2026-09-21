import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

class StageConfigError(Exception):
    """Base exception for stage configuration errors."""
    pass

class StageValidationError(StageConfigError):
    """Raised when stage configuration validation fails."""
    pass

class DependencyCycleError(StageValidationError):
    """Raised when a circular dependency is detected between stages."""
    pass

class ActionPermissionError(StageConfigError):
    """Raised when an action is not permitted for a stage."""
    pass


class Stage:
    def __init__(self, data: Dict[str, Any]):
        self.id: str = data["id"]
        self.artifact_type: str = data.get("artifact_type", "text")
        self.depends_on: List[str] = data.get("depends_on", [])
        self.allowed_actions: List[str] = data.get("allowed_actions", [])
        self.approval_required: bool = data.get("approval_required", False)
        self.generation: Optional[Dict[str, Any]] = data.get("generation")
        self.form_schema: Optional[Dict[str, Any]] = data.get("form_schema")
        self.board_view: Optional[str] = data.get("board_view")
        self.raw: Dict[str, Any] = data

    def is_action_allowed(self, action: str) -> bool:
        return action in self.allowed_actions

    def validate_action(self, action: str) -> None:
        if not self.is_action_allowed(action):
            raise ActionPermissionError(
                f"Action '{action}' is not allowed for stage '{self.id}'. "
                f"Allowed actions: {self.allowed_actions}"
            )


class StageConfig:
    VALID_ARTIFACT_TYPES = {"text", "image", "video", "audio", "form", "file", "diff", "task"}
    VALID_ACTIONS = {
        "create", "revise", "regenerate", "select_version", "edit",
        "approve", "reopen", "cancel", "lock", "unlock",
        "bulk_regenerate", "message"
    }
    VALID_BOARD_VIEWS = {"columns"}

    def __init__(self, data: Dict[str, Any]):
        self.raw: Dict[str, Any] = data
        self.schema_version: str = str(data.get("schema_version", "1"))
        self.id: str = data.get("id", "")
        self.title: str = data.get("title", "")
        self.budget: Optional[Dict[str, Any]] = data.get("budget")
        self.completion: Dict[str, Any] = data.get("completion", {})
        
        stages_data = data.get("stages", [])
        self.stages: Dict[str, Stage] = {}
        self.stage_order: List[str] = []

        for stage_data in stages_data:
            if not isinstance(stage_data, dict):
                raise StageValidationError("Stage item must be a dictionary")
            stage_id = stage_data.get("id")
            if not stage_id or not isinstance(stage_id, str):
                raise StageValidationError("Stage must have a non-empty string 'id'")
            if stage_id in self.stages:
                raise StageValidationError(f"Duplicate stage id: '{stage_id}'")
            
            stage = Stage(stage_data)
            self.stages[stage_id] = stage
            self.stage_order.append(stage_id)

        self._validate()

    def _validate(self) -> None:
        if not self.id:
            raise StageValidationError("StageConfig must have a non-empty 'id'")
        if not self.stages:
            raise StageValidationError("StageConfig must contain at least one stage")

        # Validate stage dependencies and actions
        for stage_id, stage in self.stages.items():
            # Validate artifact_type
            if stage.artifact_type not in self.VALID_ARTIFACT_TYPES:
                raise StageValidationError(
                    f"Stage '{stage_id}' has invalid artifact_type: '{stage.artifact_type}'"
                )

            # Validate depends_on references
            for dep_id in stage.depends_on:
                if dep_id not in self.stages:
                    raise StageValidationError(
                        f"Stage '{stage_id}' depends on unknown stage '{dep_id}'"
                    )
                if dep_id == stage_id:
                    raise DependencyCycleError(
                        f"Stage '{stage_id}' cannot depend on itself"
                    )

            # Validate allowed_actions
            for action in stage.allowed_actions:
                if action not in self.VALID_ACTIONS:
                    raise StageValidationError(
                        f"Stage '{stage_id}' specifies unknown action '{action}'"
                    )

            # Validate generation defaults if present
            if stage.generation:
                if not isinstance(stage.generation, dict):
                    raise StageValidationError(f"Stage '{stage_id}' generation must be a dict")

            # Validate form_schema if present
            if stage.form_schema is not None:
                self._validate_form_schema(stage_id, stage.form_schema)

            # Validate board_view if present
            if stage.board_view is not None:
                if not isinstance(stage.board_view, str):
                    raise StageValidationError(
                        f"Stage '{stage_id}' board_view must be a string"
                    )
                if stage.board_view not in self.VALID_BOARD_VIEWS:
                    raise StageValidationError(
                        f"Stage '{stage_id}' has invalid board_view: '{stage.board_view}' "
                        f"(valid values: {', '.join(sorted(self.VALID_BOARD_VIEWS))})"
                    )

        # Validate DAG (Dependency Cycle Detection)
        self._validate_dag()

        # Validate completion rules
        req_stages = self.completion.get("require_approved_stages", [])
        if not isinstance(req_stages, list):
            raise StageValidationError("completion.require_approved_stages must be a list")
        for req_id in req_stages:
            if req_id not in self.stages:
                raise StageValidationError(
                    f"Completion rule references unknown stage '{req_id}'"
                )

        # Validate budget if present
        if self.budget is not None:
            self._validate_budget(self.budget)

    def _validate_form_schema(self, stage_id: str, form_schema: Dict[str, Any]) -> None:
        if not isinstance(form_schema, dict):
            raise StageValidationError(f"Stage '{stage_id}' form_schema must be a dict")
        if "type" in form_schema and form_schema["type"] != "object":
            raise StageValidationError(f"Stage '{stage_id}' form_schema top-level type must be 'object'")

    def _validate_budget(self, budget: Dict[str, Any]) -> None:
        if not isinstance(budget, dict):
            raise StageValidationError("budget must be a dict")
        for key in ["stage_usd", "project_usd", "confirm_above_usd"]:
            if key in budget:
                val = budget[key]
                if not isinstance(val, (int, float)) or val < 0:
                    raise StageValidationError(f"budget field '{key}' must be a non-negative number")

    def _validate_dag(self) -> None:
        # DFS for cycle detection
        visited: Dict[str, int] = {s: 0 for s in self.stages}  # 0: unvisited, 1: visiting, 2: visited

        def dfs(node_id: str, path: List[str]) -> None:
            visited[node_id] = 1
            path.append(node_id)
            stage = self.stages[node_id]
            for dep_id in stage.depends_on:
                if visited[dep_id] == 1:
                    cycle_path = " -> ".join(path[path.index(dep_id):] + [dep_id])
                    raise DependencyCycleError(f"Dependency cycle detected: {cycle_path}")
                if visited[dep_id] == 0:
                    dfs(dep_id, path)
            path.pop()
            visited[node_id] = 2

        for stage_id in self.stages:
            if visited[stage_id] == 0:
                dfs(stage_id, [])

    def get_stage(self, stage_id: str) -> Stage:
        if stage_id not in self.stages:
            raise KeyError(f"Stage '{stage_id}' not found in configuration")
        return self.stages[stage_id]

    def validate_action(self, stage_id: str, action: str) -> None:
        stage = self.get_stage(stage_id)
        stage.validate_action(action)

    def is_completed(self, approved_stage_ids: Set[str]) -> bool:
        """
        Check whether the pipeline is completed based on completion rules.
        """
        req_stages = set(self.completion.get("require_approved_stages", []))
        if not req_stages:
            req_stages = set(self.stages.keys())
        return req_stages.issubset(approved_stage_ids)

    def validate_budget_limit(
        self,
        estimated_cost: float,
        current_project_cost: float = 0.0,
        current_stage_cost: float = 0.0,
        stage_id: Optional[str] = None
    ) -> Dict[str, bool]:
        """
        Validates cost against budget limits.
        Returns dict with status indicators:
        - exceeds_project_budget: bool
        - exceeds_stage_budget: bool
        - requires_confirmation: bool
        """
        if not self.budget:
            return {
                "exceeds_project_budget": False,
                "exceeds_stage_budget": False,
                "requires_confirmation": False
            }

        project_usd = self.budget.get("project_usd")
        stage_usd = self.budget.get("stage_usd")
        confirm_above_usd = self.budget.get("confirm_above_usd")

        exceeds_project = False
        if project_usd is not None:
            if (current_project_cost + estimated_cost) > project_usd:
                exceeds_project = True

        exceeds_stage = False
        if stage_usd is not None:
            if (current_stage_cost + estimated_cost) > stage_usd:
                exceeds_stage = True

        requires_confirm = False
        if confirm_above_usd is not None:
            if estimated_cost >= confirm_above_usd:
                requires_confirm = True

        return {
            "exceeds_project_budget": exceeds_project,
            "exceeds_stage_budget": exceeds_stage,
            "requires_confirmation": requires_confirm
        }


def load_stage_config(path_or_data: Union[str, Path, Dict[str, Any]]) -> StageConfig:
    """
    Load and validate stage configuration from file path or dict.
    """
    if isinstance(path_or_data, (str, Path)):
        path = Path(path_or_data)
        if not path.exists():
            raise FileNotFoundError(f"Stage config file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError as e:
                raise StageValidationError(f"Invalid JSON in stage config file '{path}': {e}")
    elif isinstance(path_or_data, dict):
        data = path_or_data
    else:
        raise TypeError("path_or_data must be a str, Path, or dict")

    return StageConfig(data)


def load_preset(name: str) -> StageConfig:
    """
    Load a built-in preset stage configuration by name (e.g., 'movie').
    """
    preset_dir = Path(__file__).parent
    preset_path = preset_dir / f"{name}.json"
    if not preset_path.exists():
        raise FileNotFoundError(f"Stage preset '{name}' not found at {preset_path}")
    return load_stage_config(preset_path)
