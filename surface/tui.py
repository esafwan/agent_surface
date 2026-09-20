"""
Plain-terminal (stdlib-only) renderer and REPL for the Agent Surface board.

This module deliberately imports nothing beyond the Python standard library
(and surface's own gradio-independent modules). It exists precisely because
the Gradio board cannot be exercised or verified in every environment; this
renderer can be run and tested anywhere Python runs.

It reuses the pure display-model logic from surface.board (DisplayModelBuilder,
ActionHandler, render_version_content, describe_select_version_conflict) rather
than reimplementing any artifact/version display logic. Its only job is to
turn that data model into readable text and drive a simple interactive loop.
"""
import sys
from typing import Any, Dict, List, Optional, TextIO

from surface.store import Store
from surface.stages.config import StageConfig
from surface.board import (
    DisplayModelBuilder,
    ActionHandler,
    ArtifactDisplayCard,
    BoardDisplayModel,
    render_version_content,
    describe_select_version_conflict,
)


# =============================================================================
# Pure text formatting helpers
# =============================================================================


def _extract_content_text(card: ArtifactDisplayCard) -> str:
    """
    Extract a plain-text representation of an artifact's selected version
    content, using board.py's render_version_content for the type mapping.

    render_version_content returns (content_type, content_value) where
    content_type may be "text", "image", "video", or "audio" and
    content_value is a plain string (text content or a file ref/path) — never
    a Gradio component, so it's already safe to print directly.
    """
    if not card.selected_version:
        return "(no version selected)"

    kind, value = render_version_content(card.selected_version)
    if value is None:
        return "(empty)"
    if kind == "text":
        return str(value)
    # image / video / audio: value is a content_ref or raw content string.
    return f"[{kind}] {value}"


def _artifact_summary_line(card: ArtifactDisplayCard) -> str:
    """One compact line per artifact: id, title, status, version, badges."""
    badges = []
    if card.locked:
        badges.append("LOCKED")
    if card.has_generating_job:
        badges.append("GENERATING")
    if card.status == "stale" or card.stale_reason:
        badges.append("STALE")
    badge_str = f" [{', '.join(badges)}]" if badges else ""

    version_str = (
        f"v{card.selected_version.version_num}"
        if card.selected_version
        else "(no version)"
    )
    return (
        f"  - {card.artifact_id:<20} {card.title:<24} "
        f"status={card.status:<10} selected={version_str:<6}{badge_str}"
    )


def _version_history_lines(card: ArtifactDisplayCard) -> List[str]:
    lines = []
    for v in card.all_versions:
        marker = "*" if v.is_selected else " "
        note = f" - {v.note}" if v.note else ""
        lines.append(
            f"    {marker} v{v.version_num} id={v.version_id} "
            f"by={v.created_by} at={v.created_at}{note}"
        )
    return lines


def render_board_text(store: Store, stage_config: StageConfig, focus_artifact_id: Optional[str] = None) -> str:
    """
    Build the current BoardDisplayModel (via DisplayModelBuilder, the same
    gradio-independent path the Gradio board uses) and format it as plain
    text.

    If focus_artifact_id is given and found, that artifact's full content,
    version history, and allowed actions are also shown (mirrors the Gradio
    board's "selected artifact" detail panel).
    """
    builder = DisplayModelBuilder(store, stage_config)
    model: BoardDisplayModel = builder.build_board_display()

    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(
        f"Agent Surface Board | status: {model.status_message} | "
        f"pending review: {model.pending_review_count} | "
        f"pending events: {model.total_pending_events}"
    )
    lines.append("=" * 72)

    focused_card: Optional[ArtifactDisplayCard] = None

    for stage_group in model.stages:
        lines.append("")
        lines.append(
            f"[{stage_group.stage_title}] ({stage_group.artifact_type}) "
            f"approved {stage_group.approved_count}/{stage_group.total_count}"
            + (" [approval required]" if stage_group.approval_required else "")
        )
        if not stage_group.artifacts:
            lines.append("  (no artifacts)")
        for card in stage_group.artifacts:
            lines.append(_artifact_summary_line(card))
            if focus_artifact_id and card.artifact_id == focus_artifact_id:
                focused_card = card

    if focus_artifact_id:
        lines.append("")
        lines.append("-" * 72)
        if focused_card is None:
            lines.append(f"Artifact '{focus_artifact_id}' not found.")
        else:
            lines.append(f"Artifact: {focused_card.artifact_id} - {focused_card.title}")
            lines.append(f"Stage: {focused_card.stage}  Status: {focused_card.status}")
            lines.append(f"Locked: {focused_card.locked}")
            lines.append(f"Allowed actions: {', '.join(focused_card.allowed_actions) or '(none)'}")
            lines.append("")
            lines.append("Selected version content:")
            lines.append(_extract_content_text(focused_card))
            lines.append("")
            lines.append("Version history:")
            history = _version_history_lines(focused_card)
            lines.extend(history if history else ["    (no versions)"])
        lines.append("-" * 72)

    return "\n".join(lines) + "\n"


# =============================================================================
# Interactive REPL
# =============================================================================


HELP_TEXT = (
    "Commands:\n"
    "  list                          - show board overview\n"
    "  show <artifact_id>            - focus an artifact (content, history, actions)\n"
    "  approve <artifact_id>         - approve an artifact\n"
    "  select <artifact_id> <version_id> [expected_version_id]\n"
    "                                 - select a version (conflict-safe)\n"
    "  lock <artifact_id>            - lock an artifact\n"
    "  unlock <artifact_id>          - unlock an artifact\n"
    "  help                          - show this help\n"
    "  quit | exit                   - leave the TUI\n"
)


def run_tui(
    store: Store,
    stage_config: StageConfig,
    input_stream: Optional[TextIO] = None,
    output_stream: Optional[TextIO] = None,
) -> None:
    """
    Interactive, plain-stdlib REPL loop for driving the board.

    Reads commands line-by-line from `input_stream` (default sys.stdin) and
    writes all output to `output_stream` (default sys.stdout). Exits cleanly
    on EOF (empty read) or on a `quit`/`exit` command, so this is fully
    testable by feeding a finite io.StringIO of commands.
    """
    in_stream = input_stream if input_stream is not None else sys.stdin
    out_stream = output_stream if output_stream is not None else sys.stdout

    handler = ActionHandler(store, stage_config=stage_config)
    focus: Optional[str] = None

    out_stream.write(render_board_text(store, stage_config, focus))
    out_stream.write(HELP_TEXT)

    while True:
        out_stream.write("\n> ")
        try:
            line = in_stream.readline()
        except Exception as exc:  # pragma: no cover - defensive
            out_stream.write(f"Error reading input: {exc}\n")
            break

        if line == "":
            # EOF
            out_stream.write("\n(EOF) exiting.\n")
            break

        command = line.strip()
        if command == "":
            continue

        parts = command.split()
        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd in ("quit", "exit"):
                out_stream.write("Goodbye.\n")
                break

            elif cmd == "help":
                out_stream.write(HELP_TEXT)

            elif cmd == "list":
                focus = None
                out_stream.write(render_board_text(store, stage_config, focus))

            elif cmd == "show":
                if len(args) != 1:
                    out_stream.write("Usage: show <artifact_id>\n")
                    continue
                focus = args[0]
                out_stream.write(render_board_text(store, stage_config, focus))

            elif cmd == "approve":
                if len(args) != 1:
                    out_stream.write("Usage: approve <artifact_id>\n")
                    continue
                result = handler.approve(args[0])
                if result.get("ok"):
                    out_stream.write(f"Approved {args[0]}.\n")
                else:
                    out_stream.write(
                        f"Approve failed: {result.get('message') or result.get('error')}\n"
                    )

            elif cmd == "select":
                if len(args) not in (2, 3):
                    out_stream.write("Usage: select <artifact_id> <version_id> [expected_version_id]\n")
                    continue
                artifact_id, version_id = args[0], args[1]
                expected = args[2] if len(args) == 3 else None
                result = handler.select_version(artifact_id, version_id, expected)
                if result.get("ok"):
                    out_stream.write(f"Selected version {version_id} for {artifact_id}.\n")
                    stale = result.get("stale_descendants") or []
                    if stale:
                        out_stream.write(f"Stale descendants: {', '.join(stale)}\n")
                else:
                    conflict_msg = describe_select_version_conflict(result)
                    if conflict_msg:
                        out_stream.write(conflict_msg + "\n")
                    else:
                        out_stream.write(f"Select failed: {result.get('error')}\n")

            elif cmd == "lock":
                if len(args) != 1:
                    out_stream.write("Usage: lock <artifact_id>\n")
                    continue
                result = handler.lock(args[0])
                if result.get("ok"):
                    out_stream.write(f"Locked {args[0]}.\n")
                else:
                    out_stream.write(f"Lock failed: {result.get('error')}\n")

            elif cmd == "unlock":
                if len(args) != 1:
                    out_stream.write("Usage: unlock <artifact_id>\n")
                    continue
                result = handler.unlock(args[0])
                if result.get("ok"):
                    out_stream.write(f"Unlocked {args[0]}.\n")
                else:
                    out_stream.write(f"Unlock failed: {result.get('error')}\n")

            else:
                out_stream.write(f"Unknown command: {cmd!r}. Type 'help' for a list.\n")

        except Exception as exc:  # pragma: no cover - defensive, keep loop alive
            out_stream.write(f"Error handling command {command!r}: {exc}\n")

    try:
        out_stream.flush()
    except Exception:
        pass
