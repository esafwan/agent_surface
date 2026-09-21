"""
Unit tests for the version diff helper module.

Tests cover the diff_versions function with edge cases:
- Identical content
- Single line changes
- Additions and deletions
- Empty strings
- Content without trailing newlines
- Different line endings
- Custom labels
- Long content (basic sanity check)
"""

import pytest
from surface.diff import diff_versions


class TestDiffVersionsBasic:
    """Basic functionality tests."""

    def test_identical_content_returns_empty_diff(self):
        """Identical content should produce an empty diff."""
        content = "line 1\nline 2\nline 3\n"
        diff = diff_versions(content, content)
        assert diff == []

    def test_identical_multiline_no_trailing_newline(self):
        """Identical content without trailing newline should produce empty diff."""
        content = "line 1\nline 2\nline 3"
        diff = diff_versions(content, content)
        assert diff == []

    def test_single_line_modification(self):
        """Modifying a single line should show removal and addition."""
        old = "line 1\nline 2\nline 3\n"
        new = "line 1\nline 2 modified\nline 3\n"
        diff = diff_versions(old, new)

        # Should have header lines
        assert len(diff) > 0
        diff_text = "".join(diff)
        assert "line 2" in diff_text
        assert "line 2 modified" in diff_text
        # Check for diff markers
        assert "-" in diff[0]  # First line should be file header with ---
        assert "+" in diff[1]  # Second line should be file header with +++


class TestDiffVersionsAddition:
    """Tests for adding lines."""

    def test_single_line_addition(self):
        """Adding a single line should be reflected in the diff."""
        old = "line 1\nline 2\n"
        new = "line 1\nline 2\nnew line\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "new line" in diff_text
        # Check that there's an addition marker (lines starting with +)
        added_lines = [line for line in diff if line.startswith("+") and not line.startswith("+++")]
        assert len(added_lines) > 0
        assert "new line" in added_lines[0]

    def test_multiple_lines_addition(self):
        """Adding multiple lines should all appear in diff."""
        old = "line 1\n"
        new = "line 1\nadded 1\nadded 2\nadded 3\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "added 1" in diff_text
        assert "added 2" in diff_text
        assert "added 3" in diff_text


class TestDiffVersionsDeletion:
    """Tests for deleting lines."""

    def test_single_line_deletion(self):
        """Deleting a single line should be reflected in the diff."""
        old = "line 1\nline 2\nline 3\n"
        new = "line 1\nline 3\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "line 2" in diff_text
        # Check for deletion marker
        deleted_lines = [line for line in diff if line.startswith("-") and not line.startswith("---")]
        assert len(deleted_lines) > 0
        assert "line 2" in deleted_lines[0]

    def test_multiple_lines_deletion(self):
        """Deleting multiple lines should all appear in diff."""
        old = "line 1\nremove 1\nremove 2\nremove 3\nline 2\n"
        new = "line 1\nline 2\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "remove 1" in diff_text
        assert "remove 2" in diff_text
        assert "remove 3" in diff_text


class TestDiffVersionsEmpty:
    """Tests for empty string edge cases."""

    def test_both_empty_strings(self):
        """Two empty strings should produce an empty diff."""
        diff = diff_versions("", "")
        assert diff == []

    def test_empty_to_content(self):
        """Diff from empty to content should show additions."""
        old = ""
        new = "line 1\nline 2\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "line 1" in diff_text
        assert "line 2" in diff_text
        # All lines should be marked as additions (except headers)
        added_lines = [line for line in diff if line.startswith("+") and not line.startswith("+++")]
        assert len(added_lines) >= 2

    def test_content_to_empty(self):
        """Diff from content to empty should show deletions."""
        old = "line 1\nline 2\n"
        new = ""
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "line 1" in diff_text
        assert "line 2" in diff_text
        # All lines should be marked as deletions (except headers)
        deleted_lines = [line for line in diff if line.startswith("-") and not line.startswith("---")]
        assert len(deleted_lines) >= 2


class TestDiffVersionsLineEndings:
    """Tests for different line ending scenarios."""

    def test_no_trailing_newline_in_new_content(self):
        """Content without trailing newline should still diff correctly."""
        old = "line 1\nline 2\n"
        new = "line 1\nline 2 modified"  # No trailing newline
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "line 2" in diff_text
        assert "line 2 modified" in diff_text

    def test_no_trailing_newline_in_old_content(self):
        """Old content without trailing newline should still diff correctly."""
        old = "line 1\nline 2"  # No trailing newline
        new = "line 1\nline 2\n"
        diff = diff_versions(old, new)

        # Should show a difference
        assert len(diff) > 0

    def test_single_line_no_newline_to_multiple_lines(self):
        """Expanding a single line without newline to multiple lines."""
        old = "single line"
        new = "line 1\nline 2\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "single line" in diff_text
        assert "line 1" in diff_text
        assert "line 2" in diff_text


class TestDiffVersionsLabels:
    """Tests for custom labels in diff headers."""

    def test_default_labels(self):
        """Without custom labels, should use default labels."""
        old = "old\n"
        new = "new\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        # Default labels are "old" and "new"
        assert "old" in diff_text or "new" in diff_text

    def test_custom_labels(self):
        """Custom labels should appear in diff headers."""
        old = "content\n"
        new = "modified\n"
        diff = diff_versions(old, new, old_label="version 1", new_label="version 2")

        diff_text = "".join(diff)
        assert "version 1" in diff_text
        assert "version 2" in diff_text

    def test_custom_old_label_only(self):
        """Custom old label only."""
        old = "old\n"
        new = "new\n"
        diff = diff_versions(old, new, old_label="custom_old")

        diff_text = "".join(diff)
        assert "custom_old" in diff_text

    def test_custom_new_label_only(self):
        """Custom new label only."""
        old = "old\n"
        new = "new\n"
        diff = diff_versions(old, new, new_label="custom_new")

        diff_text = "".join(diff)
        assert "custom_new" in diff_text


class TestDiffVersionsComplex:
    """Tests for more complex scenarios."""

    def test_mixed_additions_and_deletions(self):
        """Mix of additions, deletions, and unchanged lines."""
        old = "line 1\nline 2\nline 3\nline 4\n"
        new = "line 1\nline 2 modified\nline 5\nline 4\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        # line 1 and line 4 should be unchanged (no markers)
        # line 2 should be removed and modified version added
        # line 3 should be removed
        # line 5 should be added
        assert "line 1" in diff_text
        assert "line 2" in diff_text
        assert "line 2 modified" in diff_text
        assert "line 3" in diff_text
        assert "line 5" in diff_text

    def test_long_content(self):
        """Long content should diff without crashing."""
        # Create content with many lines
        old_lines = [f"line {i}\n" for i in range(1000)]
        new_lines = [f"line {i}\n" for i in range(1000)]
        # Modify line 500
        new_lines[500] = "line 500 modified\n"

        old = "".join(old_lines)
        new = "".join(new_lines)
        diff = diff_versions(old, new)

        # Should have a diff (at minimum the headers and the change)
        assert len(diff) > 0
        diff_text = "".join(diff)
        assert "line 500" in diff_text
        assert "line 500 modified" in diff_text

    def test_entire_file_replacement(self):
        """Replacing entire content should show full diff."""
        old = "old content\nmore old\n"
        new = "brand new\ncontent here\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "old content" in diff_text
        assert "more old" in diff_text
        assert "brand new" in diff_text
        assert "content here" in diff_text

    def test_markdown_content(self):
        """Markdown content with various structures should diff."""
        old = """# Title
## Section 1
Content here
## Section 2
More content
"""
        new = """# Title
## Section 1
Content here (updated)
## Section 2
More content
## Section 3
New section
"""
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "Content here" in diff_text
        assert "Content here (updated)" in diff_text
        assert "Section 3" in diff_text
        assert "New section" in diff_text


class TestDiffVersionsEdgeCases:
    """Edge cases and boundary conditions."""

    def test_whitespace_only_change(self):
        """Change in whitespace should be detected."""
        old = "line 1\nline 2\n"
        new = "line 1\nline  2\n"  # Extra space
        diff = diff_versions(old, new)

        # Should show a difference due to whitespace
        assert len(diff) > 0
        diff_text = "".join(diff)
        assert "line 2" in diff_text

    def test_empty_lines(self):
        """Content with empty lines should diff correctly."""
        old = "line 1\n\nline 2\n"
        new = "line 1\nline 2\n"
        diff = diff_versions(old, new)

        # Should show the removal of the empty line
        assert len(diff) > 0

    def test_single_character_content(self):
        """Single character content should diff."""
        old = "a"
        new = "b"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "a" in diff_text
        assert "b" in diff_text

    def test_single_character_with_newlines(self):
        """Single character with newlines should diff."""
        old = "a\n"
        new = "b\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "a" in diff_text
        assert "b" in diff_text

    def test_repeated_lines(self):
        """Content with repeated lines should handle diffs correctly."""
        old = "line\nline\nline\n"
        new = "line\ndifferent\nline\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        assert "different" in diff_text


class TestDiffVersionsDiffStructure:
    """Tests to verify the structure of returned diff."""

    def test_diff_lines_are_strings(self):
        """All returned diff lines should be strings."""
        old = "old\n"
        new = "new\n"
        diff = diff_versions(old, new)

        for line in diff:
            assert isinstance(line, str)

    def test_diff_headers_present_on_change(self):
        """When there's a change, diff should include file headers."""
        old = "old\n"
        new = "new\n"
        diff = diff_versions(old, new)

        # Should have at least the --- and +++ headers
        diff_text = "".join(diff)
        assert "---" in diff_text
        assert "+++" in diff_text

    def test_diff_preserves_line_content(self):
        """Diff lines should preserve the original content."""
        old = "test line with special chars: !@#$%^&*()\n"
        new = "modified: !@#$%^&*()\n"
        diff = diff_versions(old, new)

        diff_text = "".join(diff)
        # The special characters should be preserved in the diff
        assert "!@#$%^&*()" in diff_text
