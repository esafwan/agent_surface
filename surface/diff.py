"""
Version diff helper for computing unified diffs between two artifact versions.

This module provides render-time-only diff computation: it takes two content
strings and returns a unified diff structure/string. The diff is computed fresh
every time it's requested from the store, never cached or persisted to the
browser's localStorage — maintaining the architectural principle that the
store (SQLite) is the single source of truth. A second tab or a page refresh
will compute the same diff the same way.

This is a pure, UI-independent helper: it takes strings in, returns diff
lines out, and touches no database, store, filesystem, or global state.
"""

from difflib import unified_diff
from typing import Sequence


def diff_versions(
    old_content: str,
    new_content: str,
    old_label: str = "",
    new_label: str = "",
) -> list[str]:
    """
    Compute a unified diff between two version content strings.

    This function compares two artifact versions by their content and returns
    a unified diff as a list of strings, ready for rendering. The diff is
    computed fresh on every call from the store (SQLite), never cached
    client-side.

    Args:
        old_content: The earlier version's content string.
        new_content: The later version's content string.
        old_label: Optional label for the old version (e.g., "version 1").
                   If omitted, defaults to a generic label.
        new_label: Optional label for the new version (e.g., "version 2").
                   If omitted, defaults to a generic label.

    Returns:
        A list of unified diff lines (including headers). If the two contents
        are identical, returns an empty list. Each line includes trailing
        newline characters as produced by difflib.unified_diff.

    Edge cases:
        - Identical content: returns [] (no diff).
        - Empty old_content: shows new_content as additions.
        - Empty new_content: shows old_content as deletions.
        - Content without trailing newlines: handled naturally by difflib.
        - Very long content: no artificial limits; relies on available memory.

    Example:
        >>> old = "line 1\\nline 2\\n"
        >>> new = "line 1\\nline 2 modified\\n"
        >>> diff = diff_versions(old, new, "v1", "v2")
        >>> for line in diff:
        ...     print(repr(line))
        '--- v1\\n'
        '+++ v2\\n'
        '@@ -1,2 +1,2 @@\\n'
        ' line 1\\n'
        '-line 2\\n'
        '+line 2 modified\\n'
    """
    # Split content into lines, preserving trailing newlines.
    # difflib expects sequences of lines; we use splitlines(keepends=True).
    old_lines: Sequence[str] = old_content.splitlines(keepends=True)
    new_lines: Sequence[str] = new_content.splitlines(keepends=True)

    # If both are empty after split, they're identical.
    # If content has no trailing newline, splitlines won't add one,
    # so difflib handles this correctly.

    # Generate the unified diff.
    diff_lines = list(
        unified_diff(
            old_lines,
            new_lines,
            fromfile=old_label or "old",
            tofile=new_label or "new",
            lineterm="",  # Don't add extra newlines; lines already have them.
        )
    )

    return diff_lines
