"""
Shared prompt construction for agent backends.

build_prompt() takes an event_context dict describing the surface event and
produces a single deterministic prompt string instructing the model to write
only the final artifact content between fixed sentinels.
"""

from typing import Optional

SENTINEL_START = "<<<SURFACE_CONTENT"
SENTINEL_END = "SURFACE_CONTENT>>>"


def build_prompt(event_context: dict) -> str:
    """Build a deterministic prompt string from an event_context dict.

    Recognized keys (all optional except where noted):
      - event_type: str
      - note_or_message: str
      - selected_content: str
      - artifact_stage: str
      - rehydration_summary: Optional[str]
      - upstream_content: Optional[str]
    """
    event_type = event_context.get("event_type", "")
    note_or_message = event_context.get("note_or_message", "")
    selected_content = event_context.get("selected_content", "")
    artifact_stage = event_context.get("artifact_stage", "")
    rehydration_summary = event_context.get("rehydration_summary")
    upstream_content = event_context.get("upstream_content")

    lines = []
    lines.append("You are an autonomous agent producing a single artifact for a document pipeline.")
    lines.append(f"Event type: {event_type}")
    lines.append(f"Artifact stage: {artifact_stage}")
    lines.append("")
    lines.append("Instruction / note from the user:")
    lines.append(note_or_message)
    lines.append("")
    lines.append("Current selected content:")
    lines.append(selected_content)

    if rehydration_summary:
        lines.append("")
        lines.append("Context summary from prior state (rehydration):")
        lines.append(rehydration_summary)

    if upstream_content:
        lines.append("")
        lines.append("Upstream artifact content to consider:")
        lines.append(upstream_content)

    lines.append("")
    lines.append(
        "Write only the final artifact content, and nothing else — no explanation, "
        "no preamble, no commentary. Emit it between the exact sentinels shown below, "
        "each on its own line, with the artifact content in between:"
    )
    lines.append(SENTINEL_START)
    lines.append("...artifact content here...")
    lines.append(SENTINEL_END)

    return "\n".join(lines)
