#!/usr/bin/env python3
"""pycodesearch - Python source code search utility with Textual TUI.

Search within file contents across
configurable directory trees. Supports literal string matching and regular
expression patterns, with multiple search scopes to control directory
traversal depth and exclusions.

Results are returned as structured metadata objects for programmatic use,
and a Textual-based TUI provides an interactive, scrollable interface for
browsing hits with context.

Usage:
    # CLI mode (console output)
    python pycodesearch.py "search_term" --path /path/to/projects

    # CLI mode (TUI)
    python pycodesearch.py "def process" --path K:\\PycharmProjects --tui

    # Regex search, case-insensitive
    python pycodesearch.py "import os" -p /projects -m user --regex --ignore-case

    # Programmatic usage
    from pycodesearch import SearchPythonCode, SearchMode, MatchType

    searcher = SearchPythonCode(
        "K:\\PycharmProjects",
        mode=SearchMode.SEARCH_ALL_USER_PROJECT_LEVEL_CODE,
        file_mask="*.py;*.pyx",
        case_sensitive=False,
    )
    result = searcher.search("process_data")
    for file_result in result.file_results:
        print(file_result.relative_path)

Copyright (c) 2026 Stephen Genusa. All rights reserved.
Licensed under the MIT License.
"""

# =============================================================================
# SECTION 1: IMPORTS
# =============================================================================

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime
import enum
import fnmatch
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Self
from concurrent.futures import ThreadPoolExecutor

from rich.console import Console
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)


# =============================================================================
# SECTION 2: CONSTANTS & VERSION
# =============================================================================

__version__: str = "1.2.0"

DEFAULT_FILE_MASK: str = "*.py;*.ts;*.tsx;*.js"
DEFAULT_ENCODING: str = "utf-8"
CONTEXT_LINES: int = 20

DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", "__pycache__", ".git", ".idea", ".vscode",
    "node_modules", ".tox", "dist", "build", ".eggs",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

MODE_TOP: str = "top"
MODE_USER: str = "user"
MODE_ALL: str = "all"

MODE_OPTIONS: list[tuple[str, str]] = [
    ("Top", MODE_TOP),
    ("User", MODE_USER),
    ("All", MODE_ALL),
]

_NOTEPADPP_PATH: str = r"C:\Program Files\Notepad++\notepad++.exe"


# =============================================================================
# SECTION 3: CUSTOM EXCEPTIONS
# =============================================================================

class PyCodeSearchError(Exception):
    """Base exception for pycodesearch."""


class RootPathNotFoundError(PyCodeSearchError):
    """Raised when the root path does not exist."""


class RootPathNotDirectoryError(PyCodeSearchError):
    """Raised when the root path is not a directory."""


class InvalidRegexError(PyCodeSearchError):
    """Raised when an invalid regex pattern is provided."""


class FileReadError(PyCodeSearchError):
    """Raised when a file cannot be read due to encoding or I/O issues."""

class InvalidFileMaskError(PyCodeSearchError):
    """Raised when the file mask resolves to zero valid patterns."""

class EmptySearchValueError(PyCodeSearchError):
    """Raised when the search value is empty or only whitespace."""


# =============================================================================
# SECTION 4: ENUMS
# =============================================================================

class SearchMode(enum.Enum):
    """Defines the directory traversal scope for searches.

    Attributes:
        SEARCH_TOP_LEVEL_PROJECT_CODE: Search only files directly inside each
            immediate subdirectory of the root path.
        SEARCH_ALL_USER_PROJECT_LEVEL_CODE: Search all subdirectories
            recursively, excluding configured noise directories.
        SEARCH_ALL_CODE_INCLUDING_VENV_CODE: Search all subdirectories
            recursively with no exclusions.
    """

    SEARCH_TOP_LEVEL_PROJECT_CODE = "search_top_level_project_code"
    SEARCH_ALL_USER_PROJECT_LEVEL_CODE = "search_all_user_project_level_code"
    SEARCH_ALL_CODE_INCLUDING_VENV_CODE = "search_all_code_including_venv_code"


class MatchType(enum.Enum):
    """Defines how the search value is interpreted.

    Attributes:
        MATCH_LITERAL: Match the search value as a literal substring.
        MATCH_REGEX: Match the search value as a regular expression pattern.
    """

    MATCH_LITERAL = "match_literal"
    MATCH_REGEX = "match_regex"


# =============================================================================
# SECTION 5: DATA MODELS
# =============================================================================

@dataclasses.dataclass(frozen=True)
class LineMatch:
    """Represents a single line-level match within a file.

    Attributes:
        line_number: 1-based line number of the match.
        line_content: Full content of the matched line.
        match_spans: Tuple of (start, end) character offsets for each match
            within the line. Supports multiple matches per line.
    """

    line_number: int
    line_content: str
    match_spans: tuple[tuple[int, int], ...]


@dataclasses.dataclass(frozen=True)
class FileResult:
    """Represents all matches found within a single file.

    Attributes:
        file_path: Absolute path to the file.
        relative_path: Path relative to the search root path.
        file_size_bytes: File size in bytes.
        last_modified: Last modification timestamp.
        encoding: Encoding used to read the file.
        total_matches: Number of matching lines in this file.
        line_matches: Tuple of individual line-level matches.
    """

    file_path: pathlib.Path
    relative_path: str
    file_size_bytes: int
    last_modified: datetime.datetime
    encoding: str
    total_matches: int
    line_matches: tuple[LineMatch, ...]


@dataclasses.dataclass(frozen=True)
class SearchResult:
    """Represents the complete result of a search operation.

    Attributes:
        root_path: The root path that was searched.
        search_value: The search term used.
        match_type: Whether the search was literal or regex.
        case_sensitive: Whether the search was case-sensitive.
        mode: The search mode used.
        file_mask: The file mask used.
        total_files_scanned: Number of files examined.
        total_hits: Total number of line matches across all files.
        duration_seconds: Time taken to perform the search.
        file_results: Tuple of files containing matches.
    """

    root_path: pathlib.Path
    search_value: str
    match_type: MatchType
    case_sensitive: bool
    mode: SearchMode
    file_mask: str
    total_files_scanned: int
    total_hits: int
    duration_seconds: float
    file_results: tuple[FileResult, ...]


# =============================================================================
# SECTION 6: CORE SEARCH ENGINE
# =============================================================================

class SearchPythonCode:
    """Core search engine for searching file contents within a directory tree.

    This class is instantiated with a root path and search configuration.
    The ``search()`` method can then be called multiple times with different
    search values, reusing the same configuration.

    Attributes:
        root_path: Resolved absolute root path for searches.
        mode: Search scope mode.
        file_mask: Semicolon-delimited file glob patterns.
        case_sensitive: Whether searches are case-sensitive.
        match_type: Literal or regex matching.
        exclude_dirs: Active set of excluded directory names.
    """

    def __init__(
        Self: Self,
        root_path: str | pathlib.Path,
        *,
        mode: SearchMode = SearchMode.SEARCH_TOP_LEVEL_PROJECT_CODE,
        file_mask: str = DEFAULT_FILE_MASK,
        case_sensitive: bool = True,
        match_type: MatchType = MatchType.MATCH_LITERAL,
        exclude_dirs: list[str] | None = None,
    ) -> None:
        """Initialize the search engine.

        Args:
            root_path: Root directory to search under.
            mode: Search scope mode.
            file_mask: Semicolon-delimited file glob patterns.
            case_sensitive: Whether searches are case-sensitive.
            match_type: Literal or regex matching.
            exclude_dirs: Custom exclusion list. None uses built-in defaults
                per mode. Provided lists replace (not augment) the defaults.

        Raises:
            RootPathNotFoundError: If the root path does not exist.
            RootPathNotDirectoryError: If the root path is not a directory.
        """
        # Normalize and validate root path
        Self._root_path = pathlib.Path(root_path).resolve()
        if not Self._root_path.exists():
            raise RootPathNotFoundError(
                f"Root path does not exist: {Self._root_path}"
            )
        if not Self._root_path.is_dir():
            raise RootPathNotDirectoryError(
                f"Root path is not a directory: {Self._root_path}"
            )

        Self._mode = mode
        Self._file_mask = file_mask
        Self._file_mask_regex: re.Pattern[str] = Self._compile_file_mask(file_mask)
        Self._case_sensitive = case_sensitive
        Self._match_type = match_type

        # Determine exclusion set
        if exclude_dirs is not None:
            Self._exclude_dirs = frozenset(exclude_dirs)
        elif mode == SearchMode.SEARCH_ALL_CODE_INCLUDING_VENV_CODE:
            Self._exclude_dirs = frozenset()
        else:
            Self._exclude_dirs = DEFAULT_EXCLUDE_DIRS

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def search(self: Self, search_value: str) -> SearchResult:
        """Execute a search and return structured results.

        This method can be called multiple times with different search values,
        reusing the same engine configuration.

        Args:
            search_value: The literal string or regex pattern to search for.
                Must be a non-empty string.

        Returns:
            A SearchResult containing all matches and search metadata.

        Raises:
            EmptySearchValueError: If ``search_value`` is empty or
                whitespace-only.
            InvalidRegexError: If match_type is MATCH_REGEX and the pattern
                is invalid.
            FileReadError: If a file cannot be read due to encoding or I/O
                errors.
        """
        if not search_value.strip():
            raise EmptySearchValueError(
                "search_value must be a non-empty, non-whitespace string."
            )

        start_time = time.monotonic()

        # Pre-compile regex if needed
        compiled_pattern: re.Pattern[str] | None = None
        if self._match_type == MatchType.MATCH_REGEX:
            flags = 0 if self._case_sensitive else re.IGNORECASE
            try:
                compiled_pattern = re.compile(search_value, flags)
            except re.error as exc:
                raise InvalidRegexError(
                    f"Invalid regex pattern '{search_value}': {exc}"
                ) from exc

        # Collect candidate files
        candidate_files = self._collect_files()

        # Search each file
        file_results: list[FileResult] = []
        total_hits = 0

        for file_path in candidate_files:
            file_result = self._search_file(
                file_path, search_value, compiled_pattern
            )
            if file_result is not None:
                file_results.append(file_result)
                total_hits += file_result.total_matches

        end_time = time.monotonic()

        return SearchResult(
            root_path=self._root_path,
            search_value=search_value,
            match_type=self._match_type,
            case_sensitive=self._case_sensitive,
            mode=self._mode,
            file_mask=self._file_mask,
            total_files_scanned=len(candidate_files),
            total_hits=total_hits,
            duration_seconds=end_time - start_time,
            file_results=tuple(file_results),
        )

    # -------------------------------------------------------------------------
    # Private: File Collection
    # -------------------------------------------------------------------------

    @staticmethod
    def _compile_file_mask(file_mask: str) -> re.Pattern[str]:
        """Compile a semicolon-delimited glob mask into a single regex.

        Each segment is converted via ``fnmatch.translate`` and combined
        with alternation. The resulting pattern is always case-insensitive.

        Args:
            file_mask: Semicolon-delimited glob pattern string
                (e.g., ``"*.py;*.pyx;*.ts"``).

        Returns:
            A compiled regex that matches any filename satisfying any of
            the constituent glob patterns.

        Raises:
            InvalidFileMaskError: If no valid patterns remain after
                parsing.
        """
        patterns = [p.strip() for p in file_mask.split(";") if p.strip()]
        if not patterns:
            raise InvalidFileMaskError(
                f"File mask contains no valid patterns: '{file_mask}'"
            )

        regex_parts = [fnmatch.translate(p) for p in patterns]
        combined = "(?:" + "|".join(regex_parts) + ")"
        return re.compile(combined, re.IGNORECASE)

    def _collect_files(self: Self) -> list[pathlib.Path]:
        """Collect candidate files based on the search mode.

        Returns:
            List of file paths matching the file mask, filtered by mode.
        """
        if self._mode == SearchMode.SEARCH_TOP_LEVEL_PROJECT_CODE:
            return self._collect_files_top_level()
        return self._collect_files_recursive()

    def _collect_files_top_level(self: Self) -> list[pathlib.Path]:
        """Collect files directly inside each immediate subdirectory.

        Only files that are direct children of each immediate subdirectory
        of the root path are included. Files directly in the root path or
        in deeper subdirectories are excluded.

        Returns:
            List of matching file paths.
        """
        files: list[pathlib.Path] = []
        try:
            for item in self._root_path.iterdir():
                if item.is_dir() and item.name not in self._exclude_dirs:
                    for child in item.iterdir():
                        if child.is_file() and self._matches_file_mask(child.name):
                            files.append(child)
        except PermissionError as exc:
            raise FileReadError(
                f"Permission denied accessing directory: {exc.filename}"
            ) from exc
        return files

    def _collect_files_recursive(self: Self) -> list[pathlib.Path]:
        """Collect files recursively from all eligible subdirectories.

        Walks each immediate subdirectory of the root path in parallel,
        filtering out excluded directory names at every level. Files
        sitting directly in the root path are always included regardless
        of whether any subdirectories exist.

        Returns:
            List of file paths matching the file mask within the
            traversal scope.
        """
        files: list[pathlib.Path] = []

        # Get immediate subdirectories, filtering exclusions
        try:
            top_dirs = [
                d for d in self._root_path.iterdir()
                if d.is_dir() and d.name not in self._exclude_dirs
            ]
        except PermissionError:
            return files

        # Walk each top-level subdir in parallel (only if there is work)
        if top_dirs:

            def walk_one(start_dir: pathlib.Path) -> list[pathlib.Path]:
                """Walk a single directory tree and return matching files.

                Args:
                    start_dir: Root of the subtree to walk.

                Returns:
                    List of matching file paths within the subtree.
                """
                local_files: list[pathlib.Path] = []
                for root, dirs, filenames in os.walk(
                        start_dir, onerror=lambda e: None
                ):
                    dirs[:] = [d for d in dirs if d not in self._exclude_dirs]
                    for fn in filenames:
                        if self._matches_file_mask(fn):
                            local_files.append(pathlib.Path(root) / fn)
                return local_files

            with ThreadPoolExecutor(
                    max_workers=min(len(top_dirs), os.cpu_count() or 4),
            ) as pool:
                for result in pool.map(walk_one, top_dirs):
                    files.extend(result)

        # Always check files directly in the root directory
        try:
            for fn in os.listdir(self._root_path):
                fp = self._root_path / fn
                if fp.is_file() and self._matches_file_mask(fn):
                    files.append(fp)
        except PermissionError:
            pass

        return files

    def _matches_file_mask(self, filename: str) -> bool:
        """Determine whether a filename matches the configured file mask.

        Args:
            filename: The bare filename to test (not a full path).

        Returns:
            True if the filename matches any pattern in the file mask.
        """
        return self._file_mask_regex.match(filename) is not None

    # -------------------------------------------------------------------------
    # Private: File Searching
    # -------------------------------------------------------------------------

    def _search_file(
            self: Self,
            file_path: pathlib.Path,
            search_value: str,
            compiled_pattern: re.Pattern[str] | None,
    ) -> FileResult | None:
        """Search a single file for matches.

        Uses a tiered binary pre-filter strategy for literal searches to
        avoid expensive text decoding and line-by-line scanning on files
        that cannot possibly contain the search term:

        - **Tier 1** (case-sensitive): Exact byte-level containment check.
        - **Tier 2** (case-insensitive, ASCII term): Lowered byte-level
          containment check. ``bytes.lower()`` is correct for the ASCII
          range.
        - **Tier 3** (case-insensitive, non-ASCII term): Decode raw bytes
          in memory and perform a ``str.casefold()`` containment check.
          The decoded content is reused for subsequent line-by-line
          matching, eliminating double I/O for this tier.

        The pre-filter may produce false positives (passing a file that
        ultimately yields no line-level matches) but must never produce
        false negatives (rejecting a file that contains a valid match).

        Args:
            file_path: Path to the file to search.
            search_value: The literal string or regex pattern.
            compiled_pattern: Pre-compiled regex, or ``None`` for literal
                mode.

        Returns:
            A ``FileResult`` if matches were found, otherwise ``None``.
        """
        content: str | None = None

        # -----------------------------------------------------------------
        # FAST PATH: tiered pre-filter for literal searches
        # -----------------------------------------------------------------
        if self._match_type == MatchType.MATCH_LITERAL:
            try:
                raw = file_path.read_bytes()
            except OSError:
                return None

            if self._case_sensitive:
                # Tier 1: Exact byte containment — always correct.
                search_bytes = search_value.encode(DEFAULT_ENCODING)
                if search_bytes not in raw:
                    return None

            elif search_value.isascii():
                # Tier 2: ASCII case-insensitive — bytes.lower() handles
                # the A-Z / a-z range correctly.
                search_bytes = search_value.encode(DEFAULT_ENCODING)
                if search_bytes.lower() not in raw.lower():
                    return None

            else:
                # Tier 3: Non-ASCII case-insensitive — bytes.lower() does
                # not fold multi-byte UTF-8 codepoints.  Decode in memory
                # and use str.casefold() which implements full Unicode case
                # folding (e.g. Ä→ä, Σ→σ, ß→ss).
                try:
                    content = raw.decode(DEFAULT_ENCODING)
                except (UnicodeDecodeError, ValueError):
                    return None

                if search_value.casefold() not in content.casefold():
                    return None
                # `content` is retained — reused below, no second read.

        # -----------------------------------------------------------------
        # Full decode (skipped when Tier 3 already decoded successfully)
        # -----------------------------------------------------------------
        if content is None:
            try:
                content = file_path.read_text(
                    encoding=DEFAULT_ENCODING, errors="strict"
                )
            except (UnicodeDecodeError, OSError):
                return None

        # -----------------------------------------------------------------
        # Line-by-line matching
        # -----------------------------------------------------------------
        lines = content.splitlines()
        line_matches: list[LineMatch] = []

        for line_num, line in enumerate(lines, start=1):
            matched, spans = self._match_line(
                line, search_value, compiled_pattern
            )
            if matched:
                line_matches.append(LineMatch(
                    line_number=line_num,
                    line_content=line,
                    match_spans=tuple(spans),
                ))

        if not line_matches:
            return None

        try:
            stat = file_path.stat()
        except OSError:
            return None

        return FileResult(
            file_path=file_path,
            relative_path=str(file_path.relative_to(self._root_path)),
            file_size_bytes=stat.st_size,
            last_modified=datetime.datetime.fromtimestamp(stat.st_mtime),
            encoding=DEFAULT_ENCODING,
            total_matches=len(line_matches),
            line_matches=tuple(line_matches),
        )

    # -------------------------------------------------------------------------
    # Private: Line Matching
    # -------------------------------------------------------------------------

    def _match_line(
        self: Self,
        line: str,
        search_value: str,
        compiled_pattern: re.Pattern[str] | None,
    ) -> tuple[bool, list[tuple[int, int]]]:
        """Match a single line against the search value.

        Args:
            line: The line of text to search within.
            search_value: The literal string to find.
            compiled_pattern: Pre-compiled regex pattern, or None.

        Returns:
            A tuple of (was_matched, list_of_spans) where each span is a
            (start, end) character offset pair.
        """
        spans: list[tuple[int, int]] = []

        if self._match_type == MatchType.MATCH_LITERAL:
            search_in = line if self._case_sensitive else line.lower()
            search_for = search_value if self._case_sensitive else search_value.lower()
            start = 0
            while True:
                idx = search_in.find(search_for, start)
                if idx == -1:
                    break
                spans.append((idx, idx + len(search_for)))
                start = idx + len(search_for)  # Non-overlapping matches
        else:
            if compiled_pattern is not None:
                for match_obj in compiled_pattern.finditer(line):
                    spans.append((match_obj.start(), match_obj.end()))

        return (len(spans) > 0, spans)


# =============================================================================
# SECTION 7: CONVENIENCE FUNCTION
# =============================================================================

def search_python_code(
    root_path: str | pathlib.Path,
    search_value: str,
    *,
    mode: SearchMode = SearchMode.SEARCH_TOP_LEVEL_PROJECT_CODE,
    file_mask: str = DEFAULT_FILE_MASK,
    case_sensitive: bool = True,
    match_type: MatchType = MatchType.MATCH_LITERAL,
    exclude_dirs: list[str] | None = None,
) -> SearchResult:
    """Perform a single search operation and return results.

    This is a convenience wrapper that constructs a SearchPythonCode instance,
    executes the search, and returns the result.

    Args:
        root_path: Root directory to search under.
        search_value: The literal string or regex pattern to search for.
        mode: Search scope mode.
        file_mask: Semicolon-delimited file glob patterns.
        case_sensitive: Whether searches are case-sensitive.
        match_type: Literal or regex matching.
        exclude_dirs: Custom exclusion list. None uses built-in defaults.

    Returns:
        A SearchResult containing all matches and search metadata.

    Raises:
        RootPathNotFoundError: If the root path does not exist.
        RootPathNotDirectoryError: If the root path is not a directory.
        InvalidRegexError: If match_type is MATCH_REGEX and the pattern
            is invalid.
        FileReadError: If a file cannot be read.
    """
    searcher = SearchPythonCode(
        root_path,
        mode=mode,
        file_mask=file_mask,
        case_sensitive=case_sensitive,
        match_type=match_type,
        exclude_dirs=exclude_dirs,
    )
    return searcher.search(search_value)


# =============================================================================
# SECTION 8: TUI WIDGETS
# =============================================================================

class SearchInput(Input):
    """A styled input widget for entering search terms.

    This widget is used as the search bar in the TUI, positioned at the
    top of the screen. It supports Enter key submission to trigger a search.
    """

    DEFAULT_CSS = """
    SearchInput {
        height: 3;
        margin: 0 1;
        padding: 0 1;
        width: 1fr;
    }
    """

    def __init__(
        self,
        placeholder: str = "Enter search term and press Enter...",
        **kwargs: Any,
    ) -> None:
        """Initialize the search input.

        Args:
            placeholder: Placeholder text shown when the input is empty.
            **kwargs: Additional keyword arguments passed to Input.
        """
        super().__init__(placeholder=placeholder, **kwargs)


class SearchBar(Horizontal):
    """Horizontal container holding the search input and option controls."""

    BORDER_STYLE = "solid"
    DEFAULT_CSS = f"""
    SearchBar {{
        height: auto;
        padding: 0 1;
        border: {BORDER_STYLE} $primary;
        align: left middle;
    }}

    SearchBar > SearchInput {{
        width: 1fr;
        margin: 1 1 0 0;
        border: {BORDER_STYLE} $primary;
    }}

    SearchBar > SearchInput:focus {{
        border: {BORDER_STYLE} $accent;
    }}

    SearchBar > Checkbox {{
        width: auto;
        margin: 1 1 0 0;
        border: {BORDER_STYLE} $primary;
    }}

    SearchBar > Checkbox:focus {{
        border: {BORDER_STYLE} $accent;
        margin: 1 1 0 0;
    }}

    SearchBar > Label {{
        width: auto;
        margin: 1 0 0 0;
        border: {BORDER_STYLE} $primary;
    }}

    SearchBar > Select {{
        width: 20;
        margin: 0 1;
        border: {BORDER_STYLE} $primary;
    }}

    SearchBar > Select:focus {{
        border: {BORDER_STYLE} $accent;
    }}

    SearchBar > Button  {{
        width: 20;
        margin: 1 1 0 0;
        border: {BORDER_STYLE} $primary;
    }}

    SearchBar > Button:focus  {{
        border: {BORDER_STYLE} $accent;
        margin: 1 1 0 0;
    }}
    """


class HitListItem(ListItem):
    """A ListItem carrying search hit metadata for self-contained data access.


    Attributes:
        file_result: The file result associated with this hit.
        line_match: The line match associated with this hit.
    """

    def __init__(
        self,
        *children: Any,
        file_result: FileResult,
        line_match: LineMatch,
        **kwargs: Any,
    ) -> None:
        """Initialize the hit list item.

        Args:
            *children: Child widgets (typically a Label).
            file_result: The file result associated with this hit.
            line_match: The line match associated with this hit.
            **kwargs: Additional keyword arguments passed to ListItem.
        """
        super().__init__(*children, **kwargs)
        self.file_result: FileResult = file_result
        self.line_match: LineMatch = line_match

    def on_click(self, event: Click) -> None:
        if event.chain == 2:
            self.app._open_in_editor(self.file_result, self.line_match)


class MetadataPanel(Static):
    """Panel displaying file metadata for the currently selected hit.

    Shows the file path, relative path, size, modification timestamp,
    encoding, and match count for the selected file.
    """

    DEFAULT_CSS = """
    MetadataPanel {
        height: 25%;
        border-bottom: solid $primary;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def update_metadata(self: Self, file_result: FileResult) -> None:
        """Update the panel with file metadata.

        Args:
            file_result: The file result whose metadata to display.
        """
        text = Text()
        text.append("File Metadata", style="bold underline")
        text.append("\n")
        text.append("Path:       ", style="bold cyan")
        text.append(str(file_result.file_path))
        text.append("\n")
        text.append("Relative:   ", style="bold cyan")
        text.append(file_result.relative_path)
        text.append("\n")
        text.append("Size:       ", style="bold cyan")
        text.append(_format_file_size(file_result.file_size_bytes))
        text.append("\n")
        text.append("Modified:   ", style="bold cyan")
        text.append(file_result.last_modified.strftime("%Y-%m-%d %H:%M:%S"))
        text.append("\n")
        text.append("Encoding:   ", style="bold cyan")
        text.append(file_result.encoding)
        text.append("\n")
        text.append("Matches:    ", style="bold cyan")
        text.append(str(file_result.total_matches), style="bold yellow")
        self.update(text)


class ContextPanel(Static):
    """Panel displaying matched lines in surrounding file context.

    Shows the selected matched line with a few lines above and below
    for context, with the matched portions highlighted.
    """

    DEFAULT_CSS = """
    ContextPanel {
        height: 75%;
        padding: 0 1;
        overflow-y: auto;
    }
    """

    def update_context(
        self: Self, file_result: FileResult, line_match: LineMatch
    ) -> None:
        """Update the panel with context around a matched line.

        Args:
            file_result: The file result containing the match.
            line_match: The specific line match to show in context.
        """
        text = _build_context_text(file_result, line_match)
        self.update(text)


# =============================================================================
# SECTION 9: TUI APPLICATION
# =============================================================================

class PyCodeSearchApp(App[None]):
    """Textual TUI application for interactive code search.

    Provides a split-panel interface with a hits list on the left,
    file metadata on the top-right, and matched-line context on the
    bottom-right. Double-clicking a hit or pressing Ctrl+O opens the
    file in Notepad++ at the matched line number.

    Attributes:
        search_result: Reactive search result state.
    """

    TITLE = "PyCodeSearch"

    BORDER_STYLE = "solid"
    CSS = f"""
        Screen {{
            layout: vertical;
        }}

        #main-area {{
            layout: horizontal;
            height: 1fr;
        }}

        #hits-panel {{
            width: 25%;
            height: 1fr;
            border-right: {BORDER_STYLE} $primary;
        }}

        #right-area {{
            width: 75%;
            layout: vertical;
            height: 1fr;
        }}

        #status-bar {{
            height: 1;
            background: $primary;
            color: $text;
            padding: 0 1;
        }}

        /* ── Select dropdown overlay ── */
        SelectOverlay {{
            border: {BORDER_STYLE} $primary;
        }}

        SelectOverlay > ListItem {{
            border: {BORDER_STYLE} $primary;
        }}

        SelectOverlay > ListItem:hover,
        SelectOverlay > ListItem.highlighted {{
            border: {BORDER_STYLE} $accent;
        }}
        """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("ctrl+o", "open_in_editor", "Open in Editor"),
    ]

    search_result: reactive[SearchResult | None] = reactive(None)

    def __init__(
        self: Self,
        root_path: pathlib.Path,
        *,
        mode: SearchMode = SearchMode.SEARCH_TOP_LEVEL_PROJECT_CODE,
        file_mask: str = DEFAULT_FILE_MASK,
        case_sensitive: bool = True,
        match_type: MatchType = MatchType.MATCH_LITERAL,
        exclude_dirs: list[str] | None = None,
        initial_search_value: str = "",
    ) -> None:
        """Initialize the TUI application.

        Args:
            root_path: Root directory to search under.
            mode: Search scope mode.
            file_mask: Semicolon-delimited file glob patterns.
            case_sensitive: Whether searches are case-sensitive.
            match_type: Literal or regex matching.
            exclude_dirs: Custom exclusion list.
            initial_search_value: Optional search value to pre-populate.
        """
        super().__init__()
        self._root_path = root_path
        self._initial_mode = mode
        self._file_mask = file_mask
        self._initial_case_sensitive = case_sensitive
        self._initial_match_type = match_type
        self._exclude_dirs = exclude_dirs
        self._initial_search_value = initial_search_value

    # -------------------------------------------------------------------------
    # Composition
    # -------------------------------------------------------------------------

    def compose(self: Self) -> ComposeResult:
        """Compose the TUI layout."""
        yield Header()
        with SearchBar():
            yield SearchInput(id="search-input")
            yield Checkbox("Ignore Case", id="ignore-case-checkbox")
            yield Checkbox("Regular Exp", id="regex-checkbox")
            yield Label("Mode:")
            yield Select(MODE_OPTIONS, id="mode-select")
            yield Button("Search", id="search-button", variant="primary")
        with Horizontal(id="main-area"):
            with Vertical(id="hits-panel"):
                yield ListView(id="hits-list")
            with Vertical(id="right-area"):
                yield MetadataPanel(id="metadata-panel")
                yield ContextPanel(id="context-panel")
        yield Static("Type a search term and press Enter", id="status-bar")
        yield Footer()


    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def on_mount(self: Self) -> None:
        """Handle application mount event."""
        search_input = self.query_one("#search-input", SearchInput)
        if self._initial_search_value:
            search_input.value = self._initial_search_value
        search_input.focus()

        ignore_case_checkbox = self.query_one("#ignore-case-checkbox", Checkbox)
        ignore_case_checkbox.value = not self._initial_case_sensitive  # Ignore Case = NOT case_sensitive

        regex_checkbox = self.query_one("#regex-checkbox", Checkbox)
        regex_checkbox.value = (self._initial_match_type == MatchType.MATCH_REGEX)

        mode_select = self.query_one("#mode-select", Select)
        # Map the initial SearchMode to the Select value
        mode_value_map = {
            SearchMode.SEARCH_TOP_LEVEL_PROJECT_CODE: MODE_TOP,
            SearchMode.SEARCH_ALL_USER_PROJECT_LEVEL_CODE: MODE_USER,
            SearchMode.SEARCH_ALL_CODE_INCLUDING_VENV_CODE: MODE_ALL,
        }
        mode_select.value = mode_value_map[self._initial_mode]
        # Show welcome message in context panel
        context_panel = self.query_one("#context-panel", ContextPanel)
        welcome = Text()
        welcome.append("PyCodeSearch v" + __version__, style="bold green")
        welcome.append("\n\n")
        welcome.append("Enter a search term in the bar above and press ")
        welcome.append("Enter", style="bold")
        welcome.append(" to begin searching.\n\n")
        welcome.append("Double-click a hit or press ")
        welcome.append("Ctrl+O", style="bold")
        welcome.append(" to open in Notepad++.\n\n")
        welcome.append(f"Root path: ", style="dim")
        welcome.append(str(self._root_path), style="cyan")
        welcome.append("\n")
        welcome.append(f"Mode: ", style="dim")
        welcome.append(self._initial_mode.name, style="cyan")
        welcome.append("\n")
        welcome.append(f"Mask: ", style="dim")
        welcome.append(self._file_mask, style="cyan")
        context_panel.update(welcome)

        # Clear metadata panel
        metadata_panel = self.query_one("#metadata-panel", MetadataPanel)
        metadata_panel.update(Text("No file selected", style="dim"))

    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------

    def on_input_submitted(self: Self, event: Input.Submitted) -> None:
        """Handle search input submission.

        Args:
            event: The input submitted event.
        """
        if event.input.id != "search-input":
            return
        search_value = event.value.strip()
        if not search_value:
            return
        self._execute_search(search_value)

    def on_list_view_highlighted(
        self: Self, event: ListView.Highlighted
    ) -> None:
        """Handle list view highlight change for navigation.

        Reads hit metadata directly from the ``HitListItem`` instead of
        relying on a parallel index-based data list.

        Args:
            event: The list view highlighted event.
        """
        if event.item is None or not isinstance(event.item, HitListItem):
            return
        metadata_panel = self.query_one("#metadata-panel", MetadataPanel)
        context_panel = self.query_one("#context-panel", ContextPanel)
        metadata_panel.update_metadata(event.item.file_result)
        context_panel.update_context(
            event.item.file_result, event.item.line_match
        )
        self._update_status_bar('')


    def on_list_view_selected(
        self: Self, event: ListView.Selected
    ) -> None:
        """Handle list view item selection.

        Reads hit metadata directly from the ``HitListItem`` instead of
        relying on a parallel index-based data list.

        Args:
            event: The list view selected event.
        """
        if not isinstance(event.item, HitListItem):
            return
        metadata_panel = self.query_one("#metadata-panel", MetadataPanel)
        context_panel = self.query_one("#context-panel", ContextPanel)
        metadata_panel.update_metadata(event.item.file_result)
        context_panel.update_context(
            event.item.file_result, event.item.line_match
        )
        self._update_status_bar('')

    def on_button_pressed(self: Self, event: Button.Pressed) -> None:
        """Handle search button press.

        Args:
            event: The button pressed event.
        """
        if event.button.id != "search-button":
            return
        search_input = self.query_one("#search-input", SearchInput)
        search_value = search_input.value.strip()
        if not search_value:
            return
        self._execute_search(search_value)

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def action_open_in_editor(self: Self) -> None:
        """Open the currently highlighted hit in Notepad++.

        Uses ``ListView.highlighted_child`` to retrieve the currently
        highlighted ``HitListItem`` directly, avoiding index-based
        lookups.
        """
        list_view = self.query_one("#hits-list", ListView)
        item = list_view.highlighted_child
        if item is None or not isinstance(item, HitListItem):
            self._update_status_bar("No hit selected to open.")
            return
        self._open_in_editor(item.file_result, item.line_match)

    # -------------------------------------------------------------------------
    # Reactive Watchers
    # -------------------------------------------------------------------------

    async def watch_search_result(
        self: Self, result: SearchResult | None
    ) -> None:
        """React to search result changes by populating the hits list.

        Populates the ``ListView`` with ``HitListItem`` instances, each
        carrying its own ``FileResult`` and ``LineMatch`` data. This
        eliminates the need for a parallel index-based data list.

        Args:
            result: The new search result, or None.
        """
        list_view = self.query_one("#hits-list", ListView)

        await list_view.remove_children()

        if result is None:
            self._update_status_bar("No search performed")
            return

        # Build HitListItem instances — each carries its own data
        items: list[HitListItem] = []
        for file_result in result.file_results:
            for line_match in file_result.line_matches:
                label_text = (
                    f"{file_result.relative_path}:{line_match.line_number}"
                )
                items.append(HitListItem(
                    Label(label_text),
                    file_result=file_result,
                    line_match=line_match,
                ))

        await list_view.mount(*items)

        # Update status bar
        self._update_status_bar(
            f"Scanned {result.total_files_scanned} files | "
            f"{result.total_hits} hits | "
            f"{result.duration_seconds:.2f}s"
        )

        # Auto-select first hit if available
        if items:
            list_view.index = 0

    # -------------------------------------------------------------------------
    # Private Methods
    # -------------------------------------------------------------------------

    def _execute_search(self: Self, search_value: str) -> None:
        """Execute a search in a background worker.

        Args:
            search_value: The search term to search for.
        """
        self._update_status_bar("Searching...")
        self.run_worker(
            self._search_worker(search_value),
            name="search",
            exclusive=True,
        )

    async def _search_worker(self: Self, search_value: str) -> None:
        """Background worker that performs the search.

        Args:
            search_value: The search term to search for.
        """
        try:
            # Read current widget values for live option changes
            ignore_case = self.query_one(
                "#ignore-case-checkbox", Checkbox
            ).value
            use_regex = self.query_one(
                "#regex-checkbox", Checkbox
            ).value
            mode_value = self.query_one(
                "#mode-select", Select
            ).value

            case_sensitive = not ignore_case
            match_type = (
                MatchType.MATCH_REGEX if use_regex
                else MatchType.MATCH_LITERAL
            )
            mode = _map_mode(mode_value)

            searcher = SearchPythonCode(
                self._root_path,
                mode=mode,
                file_mask=self._file_mask,
                case_sensitive=case_sensitive,
                match_type=match_type,
                exclude_dirs=self._exclude_dirs,
            )
            result = await asyncio.to_thread(searcher.search, search_value)
            self.search_result = result
        except PyCodeSearchError as exc:
            self.search_result = None
            context_panel = self.query_one("#context-panel", ContextPanel)
            error_text = Text()
            error_text.append("Search Error", style="bold underline red")
            error_text.append("\n\n")
            error_text.append(str(exc), style="red")
            context_panel.update(error_text)
            self._update_status_bar(f"Error: {exc}")

    def _open_in_editor(
        self: Self, file_result: FileResult, line_match: LineMatch
    ) -> None:
        """Open a file at the matched line in Notepad++.

        Launches Notepad++ as a fire-and-forget subprocess using the
        ``-n<line>`` CLI flag to navigate to the matched line number.
        The subprocess is non-blocking — the TUI remains responsive.

        Args:
            file_result: The file result containing the match.
            line_match: The specific line match to open at.
        """
        editor_path = _find_notepadpp()
        if editor_path is None:
            self._update_status_bar(
                "Notepad++ not found. Set NOTEPADPP_PATH env var."
            )
            return

        try:
            subprocess.Popen(
                [
                    str(editor_path),
                    f"-n{line_match.line_number}",
                    str(file_result.file_path),
                ],
                shell=False,
            )
            self._update_status_bar(
                f"Opened {file_result.relative_path}:{line_match.line_number}"
            )
        except OSError as exc:
            self._update_status_bar(f"Failed to open editor: {exc}")

    def _update_status_bar(self: Self, message: str) -> None:
        """Update the status bar with a message.

        Args:
            message: The status message to display.
        """
        status_bar = self.query_one("#status-bar", Static)
        status_bar.update(message)


# =============================================================================
# SECTION 10: CLI - ARGUMENT PARSER & CONSOLE OUTPUT
# =============================================================================

def build_argument_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser.

    Returns:
        A configured ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        prog="PyCodeSearch",
        description="Python source code search utility with optional TUI.",
        epilog="Copyright (c) 2026 Stephen Genusa. Licensed under the MIT License.",
    )

    parser.add_argument(
        "search_value",
        type=str,
        help="Search string or regex pattern to find in file contents.",
    )
    parser.add_argument(
        "--path", "-p",
        type=str,
        required=True,
        help="Root directory path to search under.",
    )
    parser.add_argument(
        "--mode", "-m",
        type=str,
        choices=[MODE_TOP, MODE_USER, MODE_ALL],
        default=MODE_TOP,
        help=f"Search mode: '{MODE_TOP}' (top-level subdirs), "
             f"'{MODE_USER}' (recursive, exclude noise), "
             f"'{MODE_ALL}' (recursive, no exclusions). Default: {MODE_TOP}.",
    )
    parser.add_argument(
        "--mask",
        type=str,
        default=DEFAULT_FILE_MASK,
        help=f"Semicolon-delimited file mask (e.g., '*.py;*.ts'). "
             f"Default: {DEFAULT_FILE_MASK}.",
    )
    parser.add_argument(
        "--regex", "-r",
        action="store_true",
        default=False,
        help="Use regex matching instead of literal string matching.",
    )
    parser.add_argument(
        "--ignore-case", "-i",
        action="store_true",
        default=False,
        help="Perform case-insensitive search.",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default=None,
        help="Comma-separated list of directory names to exclude. "
             "Overrides default exclusion list.",
    )
    parser.add_argument(
        "--tui", "-t",
        action="store_true",
        default=True,
        help="Launch the Textual TUI interface.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser


def _map_mode(mode_str: str) -> SearchMode:
    """Map a CLI mode string to a SearchMode enum value.

    Args:
        mode_str: One of 'top', 'user', or 'all'.

    Returns:
        The corresponding SearchMode enum value.
    """
    mapping: dict[str, SearchMode] = {
        MODE_TOP: SearchMode.SEARCH_TOP_LEVEL_PROJECT_CODE,
        MODE_USER: SearchMode.SEARCH_ALL_USER_PROJECT_LEVEL_CODE,
        MODE_ALL: SearchMode.SEARCH_ALL_CODE_INCLUDING_VENV_CODE,
    }
    return mapping[mode_str]


def print_console_output(result: SearchResult) -> None:
    """Print search results to the console in a readable format.

    Args:
        result: The search result to display.
    """
    console = Console()

    # Header
    console.print(f"[bold green]PyCodeSearch by Stephen Genusa[/bold green] v{__version__}")
    console.rule()

    # Search info
    match_type_str = "Regex" if result.match_type == MatchType.MATCH_REGEX else "Literal"
    case_str = "sensitive" if result.case_sensitive else "insensitive"
    console.print(
        f'Search: "{result.search_value}" | Mode: {result.mode.name} | '
        f"Mask: {result.file_mask} | Case: {case_str} | {match_type_str}"
    )
    console.print(f"Root: {result.root_path}")
    console.rule()

    # Summary
    console.print(
        f"Files scanned: {result.total_files_scanned} | "
        f"Hits: {result.total_hits} | "
        f"Duration: {result.duration_seconds:.2f}s"
    )
    console.rule()

    # Results
    if not result.file_results:
        console.print("\n[dim]No matches found.[/dim]")
        return

    for file_result in result.file_results:
        console.print()
        console.print(
            f"[bold]{file_result.relative_path}[/bold] "
            f"({_format_file_size(file_result.file_size_bytes)}, "
            f"modified {file_result.last_modified.strftime('%Y-%m-%d %H:%M:%S')})"
        )
        for line_match in file_result.line_matches:
            # Build highlighted line
            line_text = Text(f"  Line {line_match.line_number}: ")
            content = Text(line_match.line_content.rstrip())
            # Apply highlighting for match spans
            for start, end in line_match.match_spans:
                content.stylize("bold yellow on red", start, end)
            line_text.append(content)
            console.print(line_text)


# =============================================================================
# SECTION 11: HELPER FUNCTIONS
# =============================================================================

def _find_notepadpp() -> pathlib.Path | None:
    """Locate the Notepad++ executable.

    Checks the ``NOTEPADPP_PATH`` environment variable first, then
    falls back to the default installation path. The environment
    variable allows users on non-standard installations to override
    the path without modifying source code.

    Returns:
        Path to notepad++.exe, or None if not found.
    """
    # Environment variable override takes priority
    env_path = os.environ.get("NOTEPADPP_PATH")
    if env_path:
        p = pathlib.Path(env_path)
        if p.is_file():
            return p

    # Default installation path
    default = pathlib.Path(_NOTEPADPP_PATH)
    if default.is_file():
        return default

    return None


def _format_file_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable file size string.

    Args:
        size_bytes: File size in bytes.

    Returns:
        A formatted string such as '4.2 KB' or '1.1 MB'.
    """
    if size_bytes < 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _build_context_text(
    file_result: FileResult, line_match: LineMatch
) -> Text:
    """Build a Rich Text object showing a matched line with surrounding context.

    Args:
        file_result: The file result containing the match.
        line_match: The specific line match to show in context.

    Returns:
        A Rich Text object with line numbers and highlighted matches.
    """
    text = Text()
    try:
        content = file_result.file_path.read_text(
            encoding=DEFAULT_ENCODING, errors="replace"
        )
    except OSError:
        text.append("Error reading file for context display.", style="bold red")
        return text

    lines = content.splitlines()
    target_idx = line_match.line_number - 1  # 0-based index
    start_idx = max(0, target_idx - CONTEXT_LINES)
    end_idx = min(len(lines), target_idx + CONTEXT_LINES + 1)

    for i in range(start_idx, end_idx):
        line_num = i + 1
        line_content = lines[i] if i < len(lines) else ""

        # Line number gutter
        gutter = f"{line_num:>4} \u2502 "
        text.append(gutter, style="dim")

        if line_num == line_match.line_number:
            # Highlight the matched line
            highlighted = Text(line_content)
            for span_start, span_end in line_match.match_spans:
                highlighted.stylize(
                    "bold black on yellow", span_start, span_end
                )
            text.append(highlighted)
        else:
            text.append(line_content, style="dim")

        text.append("\n")

    return text


# =============================================================================
# SECTION 12: MAIN ENTRY POINT
# =============================================================================

def main() -> None:
    """Main entry point for the pycodesearch application.

    Parses command-line arguments and either launches the TUI or prints
    search results to the console. On error, prints a message to stderr
    and exits with code 1.
    """
    parser = build_argument_parser()
    args = parser.parse_args()

    # Map CLI arguments to internal types
    mode = _map_mode(args.mode)
    match_type = (
        MatchType.MATCH_REGEX if args.regex else MatchType.MATCH_LITERAL
    )
    exclude_dirs = (
        [d.strip() for d in args.exclude.split(",") if d.strip()]
        if args.exclude
        else None
    )

    if args.tui:
        # Launch TUI
        app = PyCodeSearchApp(
            root_path=pathlib.Path(args.path),
            mode=mode,
            file_mask=args.mask,
            case_sensitive=not args.ignore_case,
            match_type=match_type,
            exclude_dirs=exclude_dirs,
            initial_search_value=args.search_value,
        )
        try:
            app.run()
        except PyCodeSearchError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        # Console mode
        try:
            result = search_python_code(
                root_path=args.path,
                search_value=args.search_value,
                mode=mode,
                file_mask=args.mask,
                case_sensitive=not args.ignore_case,
                match_type=match_type,
                exclude_dirs=exclude_dirs,
            )
            print_console_output(result)
        except PyCodeSearchError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()