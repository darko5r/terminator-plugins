"""
Terminator complete-scrollback visual-selection prototype.

This version implements only the visual selection layer.

It does not:
- change VTE's native selection state;
- intercept keyboard shortcuts;
- copy text;
- modify the GTK widget hierarchy;
- load native C or assembly code.
"""

import math
import os
import re
import threading
import time
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")

from gi.repository import Gdk, GLib, Gtk, Vte

import terminatorlib.plugin as plugin
from terminatorlib.terminator import Terminator
from terminatorlib.translation import _


AVAILABLE = ["FullSelectVisualPrototype"]

LOG_PATH = Path("/tmp/terminator-fullselect-overlay.txt")

ROW_COORDINATE_PROBE_PATH = Path(
    "/tmp/terminator-fullselect-row-coordinate-probe.txt"
)

# Initial visual intensity.
#
# 0.0 = completely invisible
# 1.0 = completely opaque
#
# We deliberately begin conservatively so terminal text remains readable.
OVERLAY_ALPHA = 0.34

# ROW_BAND_RENDERER_V1
#
# Two vertical pixels are removed from every 18-pixel terminal row:
#
#     1 px upper gap
#     16 px highlighted band
#     1 px lower gap
#
# Integer splitting prevents fractional-pixel blur.
ROW_GAP_PX = 2

# Small rounding improves visual separation between adjacent rows.
ROW_CORNER_RADIUS_PX = 2.0

# Keep the horizontal cell bounds exact.
ROW_HORIZONTAL_INSET_PX = 0

# Used when terminal text contains a literal tab.
TAB_STOP_COLUMNS = 8

# SEMANTIC_SELECTION_RENDERER_V1
#
# The color probe showed that the shell currently renders executable
# command tokens with exactly this foreground color.
COMMAND_SOURCE_COLORS = {
    "#3E7B04",
}

# On this system VTE's HTML export repeatedly reported zero command
# rows, while the deterministic lexical detector found every tested
# command. Disable the unused HTML scan to reduce work during refreshes.
#
# The HTML implementation remains in this file for an easy rollback.
COMMAND_HTML_DETECTION_ENABLED = False
COMMAND_LEXICAL_FALLBACK_ENABLED = True

COMMON_SHELL_COMMANDS = {
    ".", "alias", "autoload", "bg", "bindkey", "break", "builtin",
    "cd", "command", "continue", "dirs", "disable", "disown", "echo",
    "emulate", "enable", "eval", "exec", "exit", "export", "false",
    "fc", "fg", "functions", "getopts", "hash", "history", "jobs",
    "kill", "let", "local", "logout", "popd", "print", "printf",
    "pushd", "pwd", "read", "readonly", "rehash", "return", "set",
    "shift", "source", "suspend", "test", "times", "trap", "true",
    "type", "typeset", "ulimit", "umask", "unalias", "unfunction",
    "unhash", "unset", "wait", "whence", "where", "which", "zcompile",
    "zformat", "zle", "zmodload", "zparseopts", "zregexparse", "zstyle",
}

COMMAND_WRAPPERS = {
    "builtin", "command", "doas", "env", "exec", "ionice", "nice",
    "noglob", "nohup", "setsid", "sudo", "time",
}

# A prompt marker is accepted only in a compact, whitespace-free prefix.
# This supports the user's current Zsh prompt (╰─⌗) and common forms such
# as user@host$ without mistaking # or $ characters inside command text.
PROMPT_PREFIX_PATTERN = re.compile(
    r"^(?:[^\s]{0,80}(?:⌗|❯|➜)|[^\s]{1,80}(?:\$|#|%))\s+"
)
COMMAND_TOKEN_PATTERN = re.compile(r"[^\s;&|()<>]+")
SHELL_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Simulated selection backgrounds.
COMMAND_SELECTION_COLOR = "#C026D3"
LINE_NUMBER_SELECTION_COLOR = "#22C55E"

COMMAND_SELECTION_ALPHA = 0.50
LINE_NUMBER_SELECTION_ALPHA = 0.64

RENDERER_BUILD_ID = "semantic-v28-copy-validated-visual-candidate"

# LIVE_SEMANTIC_REFRESH_V4
#
# Semantic HTML is sampled outside the VTE draw callback and cached.
# This avoids the one-draw delay where command colors appeared only
# after a mouse click forced another expose.
# BOUNDED_TRAILING_DEBOUNCE_V1
#
# Rebuild after a short quiet period, but impose a maximum latency so
# continuously changing terminal output still receives periodic updates.
LIVE_REFRESH_DELAY_MS = 50
LIVE_REFRESH_MAX_LATENCY_MS = 160

# BOTTOM_FOLLOW_SCROLL_SUPPRESSION_V1
#
# VTE emits adjustment changes while automatically following new output
# at the bottom. contents-changed already covers that output, so those
# adjustment refresh requests are redundant.
SCROLL_BOTTOM_EPSILON_ROWS = 0.5

# SINGLE_PASS_REFRESH_V1
#
# Lexical command detection is deterministic on the first cache pass,
# so the former three-pass refresh burst is no longer required.
LIVE_REFRESH_PASS_COUNT = 1
RESIZE_SETTLE_DELAY_MS = 110

# PROFILE_BASELINE_V1
#
# Instrumentation only. Rendering, refresh, detection, and copying
# behavior remain unchanged while we measure actual hotspots.
PROFILE_TIMING_ENABLED = True
NANOSECONDS_PER_MILLISECOND = 1_000_000.0

SEMANTIC_CORNER_RADIUS_PX = 2.0

# Detect line-number prefixes such as:
#
#     1:
#     25:
#       300:
#
# The colon is included in the green selection segment.
LINE_NUMBER_PATTERN = re.compile(
    # Supported prefixes:
    #
    #     1:
    #     123:
    #     1-
    #     1289-
    #
    # A hyphen followed by another digit is excluded, preventing
    # the beginning of dates such as 2026-07-15 from matching.
    r"^([ \\t]*)([0-9]+(?::|-(?![0-9])))"
)

# Used only when the GTK theme does not expose a selection color.
FALLBACK_SELECTION_COLOR = "#3584e4"

# Temporary prototype shortcut:
#
#     Ctrl + Shift + Alt + S
#
# Ctrl+Shift+Alt+A remains assigned to the existing complete-copy plugin.
TOGGLE_KEYVAL = Gdk.KEY_s

TOGGLE_MODIFIERS = (
    Gdk.ModifierType.CONTROL_MASK
    | Gdk.ModifierType.SHIFT_MASK
    | Gdk.ModifierType.MOD1_MASK
)

# NATIVE_COPY_MENU_INTEGRATION_V1
#
# Reuse Terminator's existing right-click Copy menu item while visual
# full-selection mode is active. The menu remains visually native, but
# the complete-buffer extraction must be performed by this plugin
# because VTE has no native full-scrollback selection.
NATIVE_COPY_MENU_INTEGRATION = True

# COPY_LEADING_EMPTY_ROW_TRIM_V1
# Remove artificial blank terminal rows surrounding retained content.
# Internal blank lines and indentation remain unchanged.
TRIM_LEADING_EMPTY_ROWS = True
TRIM_TRAILING_EMPTY_ROWS = True


class _VteHtmlColorParser(HTMLParser):
    """
    Convert VTE's HTML row representation into plain text plus exact
    character-index color spans.
    """

    def __init__(self):
        super().__init__(
            convert_charrefs=True,
        )

        self._parts = []
        self._length = 0
        self._font_color_stack = []
        self.color_spans = []

    @property
    def text(self):
        return "".join(self._parts)

    @staticmethod
    def _normalize_color(value):
        if not value:
            return None

        return str(value).strip().upper()

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "font":
            return

        attribute_map = {
            str(name).lower(): value
            for name, value in attrs
        }

        self._font_color_stack.append(
            self._normalize_color(
                attribute_map.get("color")
            )
        )

    def handle_endtag(self, tag):
        if (
            tag.lower() == "font"
            and self._font_color_stack
        ):
            self._font_color_stack.pop()

    def handle_data(self, data):
        if not data:
            return

        start = self._length
        end = start + len(data)

        self._parts.append(data)
        self._length = end

        active_color = None

        for color in reversed(
            self._font_color_stack
        ):
            if color:
                active_color = color
                break

        if active_color is None:
            return

        if (
            self.color_spans
            and self.color_spans[-1]["color"]
                == active_color
            and self.color_spans[-1]["end"]
                == start
        ):
            self.color_spans[-1]["end"] = end
            return

        self.color_spans.append(
            {
                "start": start,
                "end": end,
                "color": active_color,
            }
        )


class FullSelectVisualPrototype(plugin.MenuItem):
    """Draw a reversible selection-colored layer over a VTE widget."""

    capabilities = ["terminal_menu"]
    _command_registry = None
    _command_registry_build_count = 0
    _command_registry_build_ns = 0
    _command_registry_lock = threading.Lock()
    _command_registry_prewarm_started = False
    _command_registry_prewarm_finished = False

    def __init__(self):
        plugin.MenuItem.__init__(self)

        # State is isolated per VTE pane.
        #
        # {
        #     id(vte): {
        #         "vte": vte,
        #         "active": bool,
        #         "draw_handler_id": int,
        #         "destroy_handler_id": int,
        #     }
        # }
        self.states = {}

        # Build the executable-name registry in a daemon worker before
        # the user first activates full selection. This removes the
        # measured ~40 ms cold-start scan from the first selection path.
        self._start_command_registry_prewarm()

        # STARTUP_TERMINAL_ATTACHMENT_V1
        #
        # A terminal_menu callback does not run until the user opens the
        # context menu. Attach to every registered VTE immediately so the
        # keyboard shortcut works from the first moment after startup.
        self._attach_registered_terminals()

    def _attach_registered_terminals(self):
        """
        Install this plugin's handlers on every currently registered VTE.

        _ensure_state() is idempotent, so the later context-menu callback
        remains a harmless fallback and cannot duplicate signal handlers.
        """

        terminator = Terminator()

        for terminal in list(
            getattr(terminator, "terminals", [])
        ):
            try:
                vte = terminal.get_vte()
            except Exception:
                continue

            if vte is None:
                continue

            self._ensure_state(vte)

    def callback(self, menuitems, menu, terminal):
        """Add the context-menu action for the current terminal pane."""

        vte = terminal.get_vte()
        state = self._ensure_state(vte)

        self._wire_native_copy_menu_item(
            menu,
            terminal,
            state,
        )

        if state["active"]:
            label = "Full Select: clear visual selection"
        else:
            label = "Full Select: show visual selection"

        item = Gtk.MenuItem.new_with_label(label)

        item.connect(
            "activate",
            self.toggle_visual_selection,
            terminal,
        )

        menuitems.append(item)

        # ROW_COORDINATE_PROBE_V1
        #
        # This action only records VTE coordinate information. It does
        # not toggle selection, rebuild caches, draw, copy, or modify VTE.
        probe_item = Gtk.MenuItem.new_with_label(
            "Full Select: write row-coordinate report"
        )

        probe_item.connect(
            "activate",
            self.write_row_coordinate_report,
            terminal,
        )

        menuitems.append(probe_item)

    def write_row_coordinate_report(
        self,
        _menu_item,
        terminal,
    ):
        """
        Map GtkAdjustment-relative rows to VTE absolute text rows.

        VTE exposes:
        - coordinate-free visible text;
        - an absolute cursor row;
        - explicit absolute-row text extraction.

        We compare all possible visible windows containing the cursor
        against coordinate-free visible text. The best match reveals the
        absolute top row and therefore the adjustment-to-absolute offset.
        """

        import difflib

        vte = terminal.get_vte()
        geometry = self._calculate_geometry(vte)

        rows = max(
            int(geometry["rows"]),
            1,
        )

        columns = max(
            int(geometry["columns"]),
            1,
        )

        adjustment = vte.get_vadjustment()

        lower = float(
            adjustment.get_lower()
        )

        upper = float(
            adjustment.get_upper()
        )

        value = float(
            adjustment.get_value()
        )

        page_size = float(
            adjustment.get_page_size()
        )

        try:
            cursor_position = (
                vte.get_cursor_position()
            )

            if hasattr(cursor_position, "column"):
                cursor_column = int(
                    cursor_position.column
                )
                cursor_row = int(
                    cursor_position.row
                )
            else:
                cursor_column = int(
                    cursor_position[0]
                )
                cursor_row = int(
                    cursor_position[1]
                )

        except Exception:
            cursor_column = -1
            cursor_row = -1

        try:
            visible_result = vte.get_text_format(
                format=Vte.Format.TEXT
            )

            visible_text = (
                self._normalize_extracted_text(
                    visible_result
                )
            )

            raw_visible_result = visible_result

            if isinstance(raw_visible_result, (tuple, list)):
                raw_visible_result = raw_visible_result[0]

            if raw_visible_result is None:
                raw_visible_result = ""

            if isinstance(raw_visible_result, bytes):
                raw_visible_result = raw_visible_result.decode(
                    "utf-8",
                    errors="replace",
                )

            visible_position_text = (
                str(raw_visible_result)
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )

        except Exception as exc:
            visible_text = ""
            visible_position_text = ""
            visible_text_error = (
                f"{type(exc).__name__}: {exc}"
            )
        else:
            visible_text_error = ""

        def compact_lines(values):
            output = []

            for value_item in values:
                normalized = (
                    str(value_item)
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                )

                for line in normalized.split("\n"):
                    line = line.rstrip()

                    if line.strip():
                        output.append(line)

            return output

        visible_lines = compact_lines(
            [visible_text]
        )

        # POSITIONAL_ROW_PROBE_V1
        # Keep blank rows and display indexes for diagnostics only.
        visible_has_final_delimiter = (
            visible_position_text.endswith("\n")
        )
        visible_position_rows = visible_position_text.split("\n")

        if visible_has_final_delimiter:
            visible_position_rows = visible_position_rows[:-1]

        visible_position_rows = [
            line.rstrip(" \t")
            for line in visible_position_rows
        ]
        visible_nonempty_display_rows = [
            index
            for index, line in enumerate(visible_position_rows)
            if line.strip()
        ]

        # RAW_LOGICAL_WINDOW_DIFFERENCE_PROBE_V1
        # Diagnostic-only canonicalizations for multi-row VTE ranges.
        def normalize_range_result(result):
            if isinstance(result, (tuple, list)):
                result = result[0]

            if result is None:
                result = ""

            if isinstance(result, bytes):
                result = result.decode(
                    "utf-8",
                    errors="replace",
                )

            return (
                str(result)
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )

        def window_variants(value):
            raw = normalize_range_result(value)
            single_delimiter = (
                raw[:-1]
                if raw.endswith("\n")
                else raw
            )
            line_rstrip = "\n".join(
                line.rstrip(" \t")
                for line in raw.split("\n")
            )
            combined = (
                line_rstrip[:-1]
                if line_rstrip.endswith("\n")
                else line_rstrip
            )

            return {
                "raw": raw,
                "single_delimiter": single_delimiter,
                "line_rstrip": line_rstrip,
                "combined": combined,
            }

        def first_mismatch(left, right):
            for index, (left_char, right_char) in enumerate(
                zip(left, right)
            ):
                if left_char != right_char:
                    return index

            if len(left) != len(right):
                return min(len(left), len(right))

            return -1

        visible_window_variants = window_variants(
            visible_position_text
        )

        adjustment_top = math.floor(
            value
        )

        adjustment_bottom_top = math.floor(
            max(
                lower,
                upper - page_size,
            )
        )

        candidate_tops = {
            adjustment_top,
            math.floor(lower),
            adjustment_bottom_top,
        }

        # When the insertion cursor is visible, its display row must be
        # somewhere from 0 through rows - 1. These are therefore every
        # mathematically possible absolute top-row candidate.
        if cursor_row >= 0:
            for cursor_display_row in range(rows):
                candidate_tops.add(
                    cursor_row - cursor_display_row
                )

        def extract_window(top_row):
            row_values = []
            extraction_errors = 0

            for display_row in range(rows):
                absolute_row = (
                    int(top_row)
                    + display_row
                )

                try:
                    row_text = (
                        self._extract_terminal_row(
                            vte,
                            absolute_row,
                            columns,
                        )
                    )
                except Exception:
                    extraction_errors += 1
                    row_text = ""

                row_values.append(row_text)

            return (
                row_values,
                extraction_errors,
            )

        records = []

        for top_row in sorted(candidate_tops):
            (
                row_values,
                extraction_errors,
            ) = extract_window(top_row)

            candidate_lines = compact_lines(
                row_values
            )

            matcher = difflib.SequenceMatcher(
                None,
                candidate_lines,
                visible_lines,
                autojunk=False,
            )

            ratio = float(
                matcher.ratio()
            )

            matching_line_count = sum(
                block.size
                for block in matcher.get_matching_blocks()
            )

            try:
                range_result = vte.get_text_range_format(
                    format=Vte.Format.TEXT,
                    start_row=int(top_row),
                    start_col=0,
                    end_row=int(top_row) + rows - 1,
                    end_col=columns - 1,
                )
                candidate_window_variants = window_variants(
                    range_result
                )
                range_error = ""
            except Exception as exc:
                candidate_window_variants = window_variants("")
                range_error = f"{type(exc).__name__}: {exc}"

            variant_ratios = {
                name: float(
                    difflib.SequenceMatcher(
                        None,
                        candidate_window_variants[name],
                        visible_window_variants[name],
                        autojunk=False,
                    ).ratio()
                )
                for name in visible_window_variants
            }

            records.append(
                {
                    "top": int(top_row),
                    "ratio": ratio,
                    "matching_line_count":
                        matching_line_count,
                    "lines": candidate_lines,
                    "row_values": [
                        line.rstrip(" \t")
                        for line in row_values
                    ],
                    "errors": extraction_errors,
                    "window_variants": candidate_window_variants,
                    "variant_ratios": variant_ratios,
                    "range_error": range_error,
                }
            )

        logical_window_records = sorted(
            records,
            key=lambda record: (
                record["variant_ratios"]["combined"],
                record["variant_ratios"]["single_delimiter"],
                -abs(record["top"] - adjustment_top),
            ),
            reverse=True,
        )
        logical_window_best = (
            logical_window_records[0]
            if logical_window_records
            else None
        )
        variant_exact_records = {
            name: [
                record
                for record in records
                if (
                    record["window_variants"][name]
                    == visible_window_variants[name]
                )
            ]
            for name in visible_window_variants
        }

        compact_exact_records = [
            record
            for record in records
            if record["lines"] == visible_lines
        ]

        if records:
            best = max(
                records,
                key=lambda record: (
                    record["ratio"],
                    record["matching_line_count"],
                    -abs(
                        record["top"]
                        - adjustment_top
                    ),
                ),
            )
        else:
            best = {
                "top": adjustment_top,
                "ratio": 0.0,
                "matching_line_count": 0,
                "lines": [],
                "errors": 0,
            }

        absolute_top = int(
            best["top"]
        )

        row_coordinate_offset = (
            absolute_top
            - adjustment_top
        )

        mapped_start_row = (
            math.floor(lower)
            + row_coordinate_offset
        )

        mapped_end_row = (
            math.ceil(upper)
            - 1
            + row_coordinate_offset
        )

        try:
            mapped_result = (
                vte.get_text_range_format(
                    format=Vte.Format.TEXT,
                    start_row=int(
                        mapped_start_row
                    ),
                    start_col=0,
                    end_row=int(
                        mapped_end_row
                    ),
                    end_col=int(
                        columns - 1
                    ),
                )
            )

            mapped_text = (
                self._normalize_complete_buffer(
                    mapped_result
                )
            )

            mapped_error = ""

        except Exception as exc:
            mapped_text = ""
            mapped_error = (
                f"{type(exc).__name__}: {exc}"
            )

        mapped_lines = compact_lines(
            [mapped_text]
        )

        adjustment_rows, adjustment_errors = (
            extract_window(
                adjustment_top
            )
        )

        adjustment_lines = compact_lines(
            adjustment_rows
        )

        ranked_records = sorted(
            records,
            key=lambda record: (
                record["ratio"],
                record["matching_line_count"],
                -abs(
                    record["top"]
                    - adjustment_top
                ),
            ),
            reverse=True,
        )

        lines = [
            "TERMINATOR FULL-SELECT ROW-COORDINATE PROBE",
            "=" * 72,
            (
                "renderer_build_id="
                f"{RENDERER_BUILD_ID}"
            ),
            (
                "adjustment_lower="
                f"{lower:.6f}"
            ),
            (
                "adjustment_upper="
                f"{upper:.6f}"
            ),
            (
                "adjustment_value="
                f"{value:.6f}"
            ),
            (
                "adjustment_page_size="
                f"{page_size:.6f}"
            ),
            (
                "adjustment_top="
                f"{adjustment_top}"
            ),
            (
                "adjustment_bottom_top="
                f"{adjustment_bottom_top}"
            ),
            (
                "cursor_absolute_column="
                f"{cursor_column}"
            ),
            (
                "cursor_absolute_row="
                f"{cursor_row}"
            ),
            (
                "visible_rows="
                f"{rows}"
            ),
            (
                "columns="
                f"{columns}"
            ),
            (
                "visible_text_error="
                f"{visible_text_error}"
            ),
            (
                "visible_nonempty_line_count="
                f"{len(visible_lines)}"
            ),
            (
                "visible_has_final_delimiter="
                f"{visible_has_final_delimiter}"
            ),
            (
                "visible_position_row_count="
                f"{len(visible_position_rows)}"
            ),
            (
                "visible_nonempty_display_rows="
                + ",".join(
                    str(index)
                    for index in visible_nonempty_display_rows
                )
            ),
            (
                "compact_exact_candidate_count="
                f"{len(compact_exact_records)}"
            ),
            (
                "raw_window_visible_characters="
                f"{len(visible_window_variants['raw'])}"
            ),
            (
                "raw_window_visible_newlines="
                f"{visible_window_variants['raw'].count(chr(10))}"
            ),
            *[
                (
                    f"raw_window_{name}_exact_candidate_count="
                    f"{len(variant_exact_records[name])}"
                )
                for name in (
                    "raw",
                    "single_delimiter",
                    "line_rstrip",
                    "combined",
                )
            ],
            *[
                (
                    f"raw_window_{name}_exact_candidate_tops="
                    + ",".join(
                        str(record["top"])
                        for record in variant_exact_records[name]
                    )
                )
                for name in (
                    "raw",
                    "single_delimiter",
                    "line_rstrip",
                    "combined",
                )
            ],
            (
                "raw_window_best_absolute_top="
                + (
                    str(logical_window_best["top"])
                    if logical_window_best is not None
                    else "none"
                )
            ),
            (
                "raw_window_best_combined_ratio="
                + (
                    f"{logical_window_best['variant_ratios']['combined']:.9f}"
                    if logical_window_best is not None
                    else "0.000000000"
                )
            ),
            (
                "raw_window_best_first_mismatch="
                + (
                    str(
                        first_mismatch(
                            logical_window_best["window_variants"]["combined"],
                            visible_window_variants["combined"],
                        )
                    )
                    if logical_window_best is not None
                    else "-1"
                )
            ),
            (
                "adjustment_probe_nonempty_line_count="
                f"{len(adjustment_lines)}"
            ),
            (
                "adjustment_probe_error_count="
                f"{adjustment_errors}"
            ),
            (
                "best_absolute_top="
                f"{absolute_top}"
            ),
            (
                "best_match_ratio="
                f"{best['ratio']:.9f}"
            ),
            (
                "best_matching_line_count="
                f"{best['matching_line_count']}"
            ),
            (
                "best_probe_nonempty_line_count="
                f"{len(best['lines'])}"
            ),
            (
                "best_probe_error_count="
                f"{best['errors']}"
            ),
            (
                "resolved_row_coordinate_offset="
                f"{row_coordinate_offset}"
            ),
            (
                "mapped_start_row="
                f"{mapped_start_row}"
            ),
            (
                "mapped_end_row="
                f"{mapped_end_row}"
            ),
            (
                "mapped_text_error="
                f"{mapped_error}"
            ),
            (
                "mapped_text_characters="
                f"{len(mapped_text)}"
            ),
            (
                "mapped_nonempty_line_count="
                f"{len(mapped_lines)}"
            ),
            "",
            "TOP CANDIDATES",
            "-" * 72,
        ]

        for index, record in enumerate(
            ranked_records[:8],
            start=1,
        ):
            lines.append(
                (
                    f"candidate_{index}="
                    f"top:{record['top']},"
                    f"ratio:{record['ratio']:.9f},"
                    "matching_lines:"
                    f"{record['matching_line_count']},"
                    "nonempty_lines:"
                    f"{len(record['lines'])},"
                    f"errors:{record['errors']}"
                )
            )

        lines.extend(
            [
                "",
                "RAW LOGICAL-WINDOW CANDIDATES",
                "-" * 72,
            ]
        )

        for index, record in enumerate(
            logical_window_records[:8],
            start=1,
        ):
            lines.append(
                (
                    f"raw_window_candidate_{index}="
                    f"top:{record['top']},"
                    f"raw:{record['variant_ratios']['raw']:.9f},"
                    "single_delimiter:"
                    f"{record['variant_ratios']['single_delimiter']:.9f},"
                    f"line_rstrip:{record['variant_ratios']['line_rstrip']:.9f},"
                    f"combined:{record['variant_ratios']['combined']:.9f},"
                    "candidate_chars:"
                    f"{len(record['window_variants']['combined'])},"
                    "candidate_newlines:"
                    f"{record['window_variants']['combined'].count(chr(10))},"
                    f"error:{record['range_error']}"
                )
            )

        if logical_window_best is not None:
            best_combined = logical_window_best[
                "window_variants"
            ]["combined"]
            visible_combined = visible_window_variants[
                "combined"
            ]
            mismatch_index = first_mismatch(
                best_combined,
                visible_combined,
            )
            context_start = max(mismatch_index - 40, 0)
            context_end = (
                mismatch_index + 80
                if mismatch_index >= 0
                else 120
            )

            lines.extend(
                [
                    (
                        "raw_window_visible_context="
                        f"{visible_combined[context_start:context_end]!r}"
                    ),
                    (
                        "raw_window_candidate_context="
                        f"{best_combined[context_start:context_end]!r}"
                    ),
                    (
                        "raw_window_visible_suffix="
                        f"{visible_combined[-120:]!r}"
                    ),
                    (
                        "raw_window_candidate_suffix="
                        f"{best_combined[-120:]!r}"
                    ),
                ]
            )

        lines.extend(
            [
                "",
                "COMPACT-EXACT POSITIONAL CANDIDATES",
                "-" * 72,
            ]
        )

        for index, record in enumerate(
            compact_exact_records,
            start=1,
        ):
            candidate_nonempty_rows = [
                display_row
                for display_row, line in enumerate(record["row_values"])
                if line.strip()
            ]
            cursor_display_row = (
                cursor_row - record["top"]
                if cursor_row >= 0
                else -1
            )

            lines.append(
                (
                    f"exact_candidate_{index}="
                    f"top:{record['top']},"
                    f"offset:{record['top'] - adjustment_top},"
                    f"cursor_display_row:{cursor_display_row},"
                    "nonempty_display_rows:"
                    + ",".join(
                        str(display_row)
                        for display_row in candidate_nonempty_rows
                    )
                )
            )

        lines.extend(
            [
                "",
                "VISIBLE POSITIONAL ROWS",
                "-" * 72,
            ]
        )

        for display_row, line in enumerate(
            visible_position_rows[:rows]
        ):
            lines.append(
                f"visible_display_row_{display_row:02d}={line!r}"
            )

        lines.extend(
            [
                "",
                "VISIBLE TEXT SAMPLES",
                "-" * 72,
            ]
        )

        for index, line in enumerate(
            visible_lines[:12],
            start=1,
        ):
            lines.append(
                f"visible_sample_{index}={line!r}"
            )

        lines.extend(
            [
                "",
                "ADJUSTMENT-ORIGIN SAMPLES",
                "-" * 72,
            ]
        )

        for index, line in enumerate(
            adjustment_lines[:12],
            start=1,
        ):
            lines.append(
                f"adjustment_sample_{index}={line!r}"
            )

        lines.extend(
            [
                "",
                "BEST-ORIGIN SAMPLES",
                "-" * 72,
            ]
        )

        for index, line in enumerate(
            best["lines"][:12],
            start=1,
        ):
            lines.append(
                f"best_sample_{index}={line!r}"
            )

        lines.extend(
            [
                "",
                "MAPPED BUFFER FIRST LINES",
                "-" * 72,
            ]
        )

        for index, line in enumerate(
            mapped_lines[:12],
            start=1,
        ):
            lines.append(
                f"mapped_first_{index}={line!r}"
            )

        lines.extend(
            [
                "",
                "MAPPED BUFFER LAST LINES",
                "-" * 72,
            ]
        )

        for index, line in enumerate(
            mapped_lines[-12:],
            start=1,
        ):
            lines.append(
                f"mapped_last_{index}={line!r}"
            )

        ROW_COORDINATE_PROBE_PATH.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _normalized_menu_label(value):
        """
        Normalize a GTK mnemonic label for reliable comparison.

        Examples:
            "_Copy" -> "copy"
            "Copy"  -> "copy"
        """

        if value is None:
            return ""

        return (
            str(value)
            .replace("_", "")
            .strip()
            .casefold()
        )

    def _wire_native_copy_menu_item(
        self,
        menu,
        terminal,
        state,
    ):
        """
        Find Terminator's existing Copy item.

        When visual full-selection mode is active:
        - enable that existing item;
        - retain its native label, location and accelerator;
        - append our complete-scrollback copy handler after Terminator's
          original VTE copy handler.
        """

        state["last_native_copy_menu_found"] = False

        if (
            not NATIVE_COPY_MENU_INTEGRATION
            or not state["active"]
        ):
            return

        target_label = self._normalized_menu_label(
            _("_Copy")
        )

        native_copy_item = None

        for menu_item in menu.get_children():
            if not isinstance(menu_item, Gtk.MenuItem):
                continue

            try:
                item_label = menu_item.get_label()
            except Exception:
                continue

            if (
                self._normalized_menu_label(item_label)
                == target_label
            ):
                native_copy_item = menu_item
                break

        if native_copy_item is None:
            state["last_copy_error"] = (
                "Terminator native Copy menu item was not found."
            )
            return

        state["last_native_copy_menu_found"] = True
        state["native_copy_menu_wire_count"] += 1

        # Terminator disabled this item because VTE reports no native
        # selection. Our visual full-selection state makes it meaningful.
        native_copy_item.set_sensitive(True)

        # Terminator's original activate handler runs first. With no
        # native VTE selection it is effectively a no-op. This handler
        # then places the complete retained buffer on the clipboard.
        native_copy_item.connect_after(
            "activate",
            self._on_native_copy_menu_activate,
            terminal,
            id(terminal.get_vte()),
        )

    @staticmethod
    def _normalize_complete_buffer(result):
        """
        Convert VTE's text-range result to normalized Unicode text.
        """

        if isinstance(result, (tuple, list)):
            result = result[0]

        if result is None:
            return ""

        if isinstance(result, bytes):
            text = result.decode(
                "utf-8",
                errors="replace",
            )
        else:
            text = str(result)

        text = text.replace("\r\n", "\n")
        text = text.replace("\r", "\n")

        if (
            not TRIM_LEADING_EMPTY_ROWS
            and not TRIM_TRAILING_EMPTY_ROWS
        ):
            return text

        lines = text.split("\n")

        if TRIM_LEADING_EMPTY_ROWS:
            first_content_index = 0

            while (
                first_content_index < len(lines)
                and lines[first_content_index].strip(" \t") == ""
            ):
                first_content_index += 1

            lines = lines[first_content_index:]

        if TRIM_TRAILING_EMPTY_ROWS:
            while (
                lines
                and lines[-1].strip(" \t") == ""
            ):
                lines.pop()

        return "\n".join(lines)

    # COPY_ONLY_ROW_COORDINATE_REPAIR_V1
    # COPY_LOGICAL_WINDOW_MATCHING_V1
    # COPY_VALIDATED_VISUAL_OFFSET_CANDIDATE_V1
    @classmethod
    def _resolve_copy_row_coordinates(
        cls,
        vte,
        preferred_offset=None,
    ):
        """Resolve Copy rows, optionally testing a validated visual offset."""

        import difflib

        geometry = cls._calculate_geometry(vte)
        rows = max(int(geometry["rows"]), 1)
        columns = max(int(geometry["columns"]), 1)

        adjustment = vte.get_vadjustment()
        lower = float(adjustment.get_lower())
        upper = float(adjustment.get_upper())
        value = float(adjustment.get_value())
        page_size = float(adjustment.get_page_size())
        adjustment_top = math.floor(value)
        preferred_top = (
            adjustment_top + int(preferred_offset)
            if preferred_offset is not None
            else None
        )

        try:
            cursor_position = vte.get_cursor_position()
            cursor_row = int(
                cursor_position.row
                if hasattr(cursor_position, "row")
                else cursor_position[1]
            )
        except Exception:
            cursor_row = -1

        def compact(value):
            normalized = (
                str(value)
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            return [
                line.rstrip()
                for line in normalized.split("\n")
                if line.strip()
            ]

        visible_result = vte.get_text_format(
            format=Vte.Format.TEXT
        )
        visible_lines = compact(
            cls._normalize_extracted_text(
                visible_result
            )
        )

        def evaluate(top_row):
            try:
                candidate_result = vte.get_text_range_format(
                    format=Vte.Format.TEXT,
                    start_row=int(top_row),
                    start_col=0,
                    end_row=int(top_row) + rows - 1,
                    end_col=columns - 1,
                )
                candidate_text = (
                    cls._normalize_extracted_text(
                        candidate_result
                    )
                )
                error_count = 0
            except Exception:
                candidate_text = ""
                error_count = 1

            candidate_lines = compact(candidate_text)
            matcher = difflib.SequenceMatcher(
                None,
                candidate_lines,
                visible_lines,
                autojunk=False,
            )
            return {
                "top": int(top_row),
                "score": float(matcher.ratio()),
                "matched": sum(
                    block.size
                    for block in matcher.get_matching_blocks()
                ),
                "lines": candidate_lines,
                "errors": error_count,
            }

        # Fast path: ordinary pre-clear coordinates already match.
        baseline = evaluate(adjustment_top)

        if baseline["errors"] == 0 and baseline["lines"] == visible_lines:
            best = baseline
        else:
            candidate_tops = {
                adjustment_top,
                math.floor(lower),
                math.floor(max(lower, upper - page_size)),
            }

            if preferred_top is not None:
                candidate_tops.add(preferred_top)

            if cursor_row >= 0:
                candidate_tops.update(
                    cursor_row - display_row
                    for display_row in range(rows)
                )

            records = [
                baseline
                if top_row == adjustment_top
                else evaluate(top_row)
                for top_row in sorted(candidate_tops)
            ]

            best = max(
                records,
                key=lambda record: (
                    record["score"],
                    record["matched"],
                    -abs(record["top"] - adjustment_top),
                ),
            )

        exact = best["lines"] == visible_lines
        strong = (
            len(visible_lines) >= 2
            and best["score"] >= 0.95
            and best["matched"] >= max(
                2,
                math.ceil(len(visible_lines) * 0.90),
            )
        )
        validation = "exact" if exact else "strong" if strong else "failed"

        if best["errors"] or validation == "failed":
            raise RuntimeError(
                "Copy row-coordinate mapping was not strongly validated "
                f"(score={best['score']:.9f}, matched={best['matched']}, "
                f"visible={len(visible_lines)}, errors={best['errors']}, "
                f"preferred_top={preferred_top})."
            )

        absolute_top = int(best["top"])

        return {
            "row_coordinate_offset": absolute_top - adjustment_top,
            "coordinate_match_score": best["score"],
            "coordinate_matching_line_count": best["matched"],
            "coordinate_visible_line_count": len(visible_lines),
            "coordinate_validation": validation,
            "coordinate_window_mode": "logical_multi_row",
            "coordinate_preferred_offset": (
                int(preferred_offset)
                if preferred_offset is not None
                else 0
            ),
            "coordinate_preferred_top": (
                int(preferred_top)
                if preferred_top is not None
                else adjustment_top
            ),
            "coordinate_preferred_candidate_used": (
                preferred_top is not None
                and absolute_top == preferred_top
            ),
            "adjustment_top": adjustment_top,
            "absolute_top": absolute_top,
            "cursor_absolute_row": cursor_row,
        }

    # POSITION_ALIGNED_VISUAL_REPAIR_V1
    # VISUAL_LOGICAL_WINDOW_TRAILING_NEWLINE_REPAIR_V1
    @classmethod
    def _resolve_visual_row_coordinates(
        cls,
        vte,
        geometry,
        preferred_offset=0,
    ):
        """Resolve visual rows by canonical logical-window equality."""

        rows = max(int(geometry["rows"]), 1)
        columns = max(int(geometry["columns"]), 1)
        adjustment = vte.get_vadjustment()
        adjustment_top = math.floor(
            float(adjustment.get_value())
        )

        try:
            cursor_position = vte.get_cursor_position()
            cursor_row = int(
                cursor_position.row
                if hasattr(cursor_position, "row")
                else cursor_position[1]
            )
        except Exception:
            cursor_row = -1

        def normalize_window(result):
            if isinstance(result, (tuple, list)):
                result = result[0]

            if result is None:
                result = ""

            if isinstance(result, bytes):
                result = result.decode(
                    "utf-8",
                    errors="replace",
                )

            text = (
                str(result)
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )

            # The diagnostic probe proved that get_text_format() retains
            # one more trailing empty-row delimiter than an equivalent
            # get_text_range_format() window. Remove only the trailing
            # newline run. Leading and internal blank rows remain exact.
            return text.rstrip("\n")

        visible_window = normalize_window(
            vte.get_text_format(
                format=Vte.Format.TEXT
            )
        )
        visible_logical_lines = (
            visible_window.split("\n")
            if visible_window
            else []
        )

        def evaluate(top_row):
            try:
                candidate_result = vte.get_text_range_format(
                    format=Vte.Format.TEXT,
                    start_row=int(top_row),
                    start_col=0,
                    end_row=int(top_row) + rows - 1,
                    end_col=columns - 1,
                )
                candidate_window = normalize_window(
                    candidate_result
                )
                error_count = 0
            except Exception:
                candidate_window = ""
                error_count = 1

            return {
                "top": int(top_row),
                "errors": error_count,
                "exact": (
                    error_count == 0
                    and candidate_window == visible_window
                ),
            }

        preferred_top = (
            adjustment_top
            + int(preferred_offset)
        )
        preferred_record = evaluate(preferred_top)

        if preferred_record["exact"]:
            exact_records = [preferred_record]
        else:
            candidate_tops = {
                adjustment_top,
                preferred_top,
            }

            if cursor_row >= 0:
                candidate_tops.update(
                    cursor_row - display_row
                    for display_row in range(rows)
                )

            records = [
                preferred_record
                if top_row == preferred_top
                else evaluate(top_row)
                for top_row in sorted(candidate_tops)
            ]
            exact_records = [
                record
                for record in records
                if record["exact"]
            ]

        if not exact_records:
            raise RuntimeError(
                "No candidate VTE range exactly matched the "
                "canonical visible logical window."
            )

        best = min(
            exact_records,
            key=lambda record: (
                abs(record["top"] - preferred_top),
                abs(record["top"] - adjustment_top),
            ),
        )
        absolute_top = int(best["top"])

        return {
            "row_coordinate_offset": (
                absolute_top - adjustment_top
            ),
            "coordinate_validation": "logical_window_exact",
            "coordinate_window_mode": (
                "logical_multi_row_trailing_newline_trim"
            ),
            "coordinate_visible_row_count": rows,
            "coordinate_logical_line_count": len(
                visible_logical_lines
            ),
            "coordinate_nonempty_row_count": sum(
                bool(line.strip())
                for line in visible_logical_lines
            ),
            "coordinate_exact_candidate_count": len(exact_records),
            "adjustment_top": adjustment_top,
            "absolute_top": absolute_top,
            "cursor_absolute_row": cursor_row,
            "cursor_display_row": (
                cursor_row - absolute_top
                if cursor_row >= 0
                else -1
            ),
        }

    @classmethod
    def _extract_complete_scrollback(
        cls,
        vte,
        row_coordinate_offset=0,
    ):
        """
        Extract the complete retained normal-screen VTE buffer.

        The vertical adjustment represents a half-open interval:

            [lower, upper)

        Therefore the last retained row is ceil(upper) - 1.
        """

        adjustment = vte.get_vadjustment()

        start_row = (
            math.floor(float(adjustment.get_lower()))
            + int(row_coordinate_offset)
        )

        end_row = (
            math.ceil(
                float(adjustment.get_upper())
            )
            - 1
            + int(row_coordinate_offset)
        )

        columns = max(
            int(vte.get_column_count()),
            1,
        )

        if end_row < start_row:
            return "", start_row, end_row

        result = vte.get_text_range_format(
            format=Vte.Format.TEXT,
            start_row=int(start_row),
            start_col=0,
            end_row=int(end_row),
            end_col=int(columns - 1),
        )

        return (
            cls._normalize_complete_buffer(result),
            start_row,
            end_row,
        )

    def _copy_complete_visual_selection(
        self,
        terminal,
        state,
        action,
    ):
        """
        Copy the complete retained scrollback to the normal clipboard.
        """

        profile_started_ns = (
            time.perf_counter_ns()
            if PROFILE_TIMING_ENABLED
            else 0
        )

        state["last_copy_action"] = action
        state["last_copy_error"] = ""

        try:
            vte = terminal.get_vte()

            state.update(
                {
                    "copy_row_coordinate_offset": 0,
                    "copy_coordinate_match_score": 0.0,
                    "copy_coordinate_matching_line_count": 0,
                    "copy_coordinate_visible_line_count": 0,
                    "copy_coordinate_validation": "failed",
                    "copy_coordinate_window_mode": "logical_multi_row",
                    "copy_coordinate_preferred_offset": 0,
                    "copy_coordinate_preferred_top": 0,
                    "copy_coordinate_preferred_candidate_available": False,
                    "copy_coordinate_preferred_candidate_used": False,
                    "copy_adjustment_top": 0,
                    "copy_absolute_top": 0,
                    "copy_cursor_absolute_row": -1,
                    "copy_mapped_start_row": 0,
                    "copy_mapped_end_row": -1,
                }
            )

            preferred_visual_offset = None

            if state.get("visual_coordinate_validation") == (
                "logical_window_exact"
            ):
                preferred_visual_offset = int(
                    state.get(
                        "visual_row_coordinate_offset",
                        0,
                    )
                )

            state["copy_coordinate_preferred_offset"] = (
                preferred_visual_offset
                if preferred_visual_offset is not None
                else 0
            )
            state["copy_coordinate_preferred_candidate_available"] = (
                preferred_visual_offset is not None
            )

            try:
                coordinate_resolution = (
                    self._resolve_copy_row_coordinates(
                        vte,
                        preferred_offset=preferred_visual_offset,
                    )
                )
            except Exception:
                state["copy_coordinate_failure_count"] = (
                    state.get("copy_coordinate_failure_count", 0) + 1
                )
                raise

            state["copy_coordinate_resolution_count"] = (
                state.get("copy_coordinate_resolution_count", 0) + 1
            )
            state.update(
                {
                    f"copy_{name}": value
                    for name, value in coordinate_resolution.items()
                }
            )

            (
                text,
                start_row,
                end_row,
            ) = self._extract_complete_scrollback(
                vte,
                coordinate_resolution["row_coordinate_offset"],
            )

            state["copy_mapped_start_row"] = start_row
            state["copy_mapped_end_row"] = end_row

            # Use the same standard clipboard object Terminator uses.
            clipboard = terminal.clipboard
            clipboard.set_text(text, -1)
            clipboard.store()

            state["copy_count"] += 1
            state["last_copy_characters"] = len(text)
            state["last_copy_lines"] = (
                len(text.splitlines())
                if text
                else 0
            )
            state["last_copy_start_row"] = start_row
            state["last_copy_end_row"] = end_row

            return True

        except Exception as exc:
            state["last_copy_error"] = (
                f"{type(exc).__name__}: {exc}"
            )

            return False

        finally:
            if PROFILE_TIMING_ENABLED:
                self._record_profile_timing(
                    state,
                    "profile_copy",
                    time.perf_counter_ns()
                    - profile_started_ns,
                )

    def _on_native_copy_menu_activate(
        self,
        _menu_item,
        terminal,
        key,
    ):
        state = self.states.get(key)

        if (
            state is None
            or not state["active"]
        ):
            return

        self._copy_complete_visual_selection(
            terminal,
            state,
            "native_copy_menu",
        )

        # Refresh diagnostics after the Copy click.
        self._safe_write_diagnostic(
            terminal.get_vte(),
            True,
            "native_copy_menu",
        )

    def unload(self):
        """Remove every drawing handler when the plugin is disabled."""

        for key, state in list(self.states.items()):
            vte = state["vte"]

            state["active"] = False

            try:
                vte.queue_draw()
            except Exception:
                pass

            try:
                vte.disconnect(state["draw_handler_id"])
            except Exception:
                pass

            try:
                vte.disconnect(state["destroy_handler_id"])
            except Exception:
                pass

            for source_name in (
                "refresh_source_id",
                "resize_source_id",
            ):
                source_id = state.get(source_name)

                if source_id is None:
                    continue

                try:
                    GLib.source_remove(source_id)
                except Exception:
                    pass

                state[source_name] = None

            adjustment = state.get("adjustment")
            adjustment_handler_id = state.get(
                "adjustment_handler_id"
            )

            if (
                adjustment is not None
                and adjustment_handler_id is not None
            ):
                try:
                    adjustment.disconnect(
                        adjustment_handler_id
                    )
                except Exception:
                    pass

            try:
                vte.disconnect(
                    state["size_allocate_handler_id"]
                )
            except Exception:
                pass

            try:
                vte.disconnect(
                    state["commit_handler_id"]
                )
            except Exception:
                pass

            try:
                vte.disconnect(
                    state["cursor_handler_id"]
                )
            except Exception:
                pass

            try:
                vte.disconnect(
                    state["contents_handler_id"]
                )
            except Exception:
                pass

            try:
                vte.disconnect(state["key_handler_id"])
            except Exception:
                pass

            self.states.pop(key, None)

    def _ensure_state(self, vte):
        key = id(vte)
        existing = self.states.get(key)

        if existing is not None and existing["vte"] is vte:
            return existing

        # connect_after() is essential: VTE paints first, then our
        # translucent layer is composited over the completed terminal.
        draw_handler_id = vte.connect_after(
            "draw",
            self.on_vte_draw_after,
            key,
        )

        destroy_handler_id = vte.connect(
            "destroy",
            self.on_vte_destroy,
            key,
        )

        key_handler_id = vte.connect(
            "key-press-event",
            self.on_vte_keypress,
            key,
        )

        # LIVE_SEMANTIC_REFRESH_V1
        #
        # VTE may finish updating foreground-color attributes after the
        # current draw request. Schedule one additional, coalesced redraw
        # after its visible contents change.
        contents_handler_id = vte.connect_after(
            "contents-changed",
            self.on_vte_contents_changed,
            key,
        )

        cursor_handler_id = vte.connect_after(
            "cursor-moved",
            self.on_vte_cursor_moved,
            key,
        )

        commit_handler_id = vte.connect_after(
            "commit",
            self.on_vte_commit,
            key,
        )

        size_allocate_handler_id = vte.connect_after(
            "size-allocate",
            self.on_vte_size_allocate,
            key,
        )

        adjustment = vte.get_vadjustment()

        adjustment_handler_id = adjustment.connect(
            "value-changed",
            self.on_vte_adjustment_changed,
            key,
        )

        state = {
            "vte": vte,
            "active": False,
            "draw_handler_id": draw_handler_id,
            "destroy_handler_id": destroy_handler_id,
            "key_handler_id": key_handler_id,
            "contents_handler_id": contents_handler_id,
            "cursor_handler_id": cursor_handler_id,
            "commit_handler_id": commit_handler_id,
            "size_allocate_handler_id": size_allocate_handler_id,
            "adjustment": adjustment,
            "adjustment_handler_id": adjustment_handler_id,
            "refresh_source_id": None,
            "resize_source_id": None,
            "refresh_passes_remaining": 0,
            "refresh_request_count": 0,
            "refresh_reason_counts": {},
            "refresh_flush_count": 0,
            "refresh_burst_started_ns": 0,
            "refresh_last_request_ns": 0,
            "refresh_deferred_count": 0,
            "refresh_max_latency_flush_count": 0,
            "refresh_failure_count": 0,
            "last_refresh_failure_context": "",
            "last_refresh_error": "",
            "scroll_at_bottom_suppressed_count": 0,
            "scroll_manual_refresh_count": 0,
            "forced_redraw_count": 0,
            "resize_refresh_count": 0,
            "last_refresh_reason": "none",
            "render_cache": None,
            "cache_generation": 0,
            "cache_refresh_count": 0,
            "last_cache_reason": "none",
            "last_draw_cache_miss_count": 0,
            "draw_cache_hit_count": 0,
            "draw_cache_slow_count": 0,
            "last_draw_cache_path": "none",
            "last_command_segment_count": 0,
            "last_line_number_segment_count": 0,
            "last_html_command_row_count": 0,
            "last_lexical_command_row_count": 0,
            "native_copy_menu_wire_count": 0,
            "last_native_copy_menu_found": False,
            "copy_count": 0,
            "last_copy_action": "none",
            "last_copy_characters": 0,
            "last_copy_lines": 0,
            "last_copy_start_row": 0,
            "last_copy_end_row": -1,
            "last_copy_error": "",
            "diagnostic_attempt_count": 0,
            "diagnostic_failure_count": 0,
            "last_diagnostic_error": "",
            "visual_row_coordinate_offset": 0,
            "visual_coordinate_resolution_count": 0,
            "visual_coordinate_failure_count": 0,
            "visual_coordinate_validation": "none",
            "visual_coordinate_window_mode": "none",
            "visual_coordinate_visible_row_count": 0,
            "visual_coordinate_logical_line_count": 0,
            "visual_coordinate_nonempty_row_count": 0,
            "visual_coordinate_exact_candidate_count": 0,
            "visual_adjustment_top": 0,
            "visual_absolute_top": 0,
            "visual_cursor_absolute_row": -1,
            "visual_cursor_display_row": -1,
            "visual_coordinate_error": "",
            "profile_cache_rebuild_count": 0,
            "profile_cache_rebuild_total_ns": 0,
            "profile_cache_rebuild_last_ns": 0,
            "profile_cache_rebuild_max_ns": 0,
            "profile_geometry_count": 0,
            "profile_geometry_total_ns": 0,
            "profile_geometry_last_ns": 0,
            "profile_geometry_max_ns": 0,
            "profile_row_extract_count": 0,
            "profile_row_extract_total_ns": 0,
            "profile_row_extract_last_ns": 0,
            "profile_row_extract_max_ns": 0,
            "profile_semantic_count": 0,
            "profile_semantic_total_ns": 0,
            "profile_semantic_last_ns": 0,
            "profile_semantic_max_ns": 0,
            "profile_partition_count": 0,
            "profile_partition_total_ns": 0,
            "profile_partition_last_ns": 0,
            "profile_partition_max_ns": 0,
            "profile_draw_count": 0,
            "profile_draw_total_ns": 0,
            "profile_draw_last_ns": 0,
            "profile_draw_max_ns": 0,
            "profile_copy_count": 0,
            "profile_copy_total_ns": 0,
            "profile_copy_last_ns": 0,
            "profile_copy_max_ns": 0,
        }

        self.states[key] = state
        return state

    @staticmethod
    def _event_modifiers(event):
        """
        Keep only modifiers relevant to GTK accelerators.

        This excludes irrelevant state such as Caps Lock and Num Lock,
        preventing those states from breaking the shortcut.
        """

        relevant_mask = Gtk.accelerator_get_default_mod_mask()

        return int(
            event.state & relevant_mask
        )

    @classmethod
    def _is_toggle_shortcut(cls, event):
        keyval = Gdk.keyval_to_lower(
            event.keyval
        )

        modifiers = cls._event_modifiers(
            event
        )

        return (
            keyval == TOGGLE_KEYVAL
            and modifiers == int(TOGGLE_MODIFIERS)
        )

    def on_vte_keypress(self, vte, event, key):
        state = self.states.get(key)

        if state is None or state["vte"] is not vte:
            return False

        if self._is_toggle_shortcut(event):
            self._set_active(
                vte,
                not state["active"],
                action="keyboard_toggle",
            )

            # Prevent this shortcut from producing input in the shell.
            return True

        if (
            event.keyval == Gdk.KEY_Escape
            and state["active"]
        ):
            self._set_active(
                vte,
                False,
                action="escape_clear",
            )

            # Consume Escape only while our selection mode is active.
            return True

        # Schedule a delayed semantic refresh after VTE and the shell
        # have processed this ordinary keypress.
        self._schedule_live_refresh(
            key,
            "key_press",
        )

        # All unrelated keyboard input continues to VTE normally.
        return False

    def toggle_visual_selection(self, _menu_item, terminal):
        vte = terminal.get_vte()
        state = self._ensure_state(vte)

        self._set_active(
            vte,
            not state["active"],
            action="menu_toggle",
        )

    # REFRESH_FAILURE_CONTAINMENT_V1
    @staticmethod
    def _record_refresh_failure(state, context, exc):
        """Record a contained refresh failure without wedging timers."""

        state["refresh_failure_count"] += 1
        state["last_refresh_failure_context"] = str(context)
        state["last_refresh_error"] = (
            f"{type(exc).__name__}: {exc}"
        )

    def _set_active(self, vte, active, action):
        state = self._ensure_state(vte)
        state["active"] = bool(active)

        if state["active"]:
            # Lexical command detection is available immediately, so one
            # synchronous cache build produces the complete first frame.
            try:
                self._rebuild_render_cache(
                    vte,
                    state,
                    "selection_activated_sync",
                )
            except Exception as exc:
                self._record_refresh_failure(
                    state,
                    "selection_activated_sync",
                    exc,
                )
        else:
            state["render_cache"] = None

        self._safe_write_diagnostic(
            vte,
            state["active"],
            action,
        )

        self._force_overlay_redraw(vte, state)

        if state["active"]:
            self._schedule_live_refresh(
                id(vte),
                "selection_activated",
            )

    def on_vte_destroy(self, _vte, key):
        state = self.states.pop(key, None)

        if state is None:
            return

        for source_name in (
            "refresh_source_id",
            "resize_source_id",
        ):
            source_id = state.get(source_name)

            if source_id is None:
                continue

            try:
                GLib.source_remove(source_id)
            except Exception:
                pass

        adjustment = state.get("adjustment")
        adjustment_handler_id = state.get(
            "adjustment_handler_id"
        )

        if (
            adjustment is not None
            and adjustment_handler_id is not None
        ):
            try:
                adjustment.disconnect(
                    adjustment_handler_id
                )
            except Exception:
                pass

    def on_vte_contents_changed(self, _vte, key):
        self._schedule_live_refresh(
            key,
            "contents_changed",
        )

    def on_vte_cursor_moved(self, _vte, key):
        self._schedule_live_refresh(
            key,
            "cursor_moved",
        )

    def on_vte_commit(
        self,
        _vte,
        _text,
        _size,
        key,
    ):
        self._schedule_live_refresh(
            key,
            "commit",
        )

    def on_vte_size_allocate(
        self,
        vte,
        _allocation,
        key,
    ):
        state = self.states.get(key)

        if state is None or not state["active"]:
            return

        # Current geometry is applied on every draw using cached semantic
        # column ranges, so resizing remains aligned immediately. Delay
        # expensive HTML resampling until the drag has settled.
        source_id = state.get("resize_source_id")

        if source_id is not None:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass

        state["last_refresh_reason"] = "size_allocate"
        state["resize_source_id"] = GLib.timeout_add(
            RESIZE_SETTLE_DELAY_MS,
            self._flush_resize_refresh,
            key,
        )

        # GTK already redraws the VTE continuously during an interactive
        # resize. Avoid adding a second full repaint for every allocation.

    def _flush_resize_refresh(self, key):
        state = self.states.get(key)

        if state is None:
            return False

        state["resize_source_id"] = None

        if not state["active"]:
            return False

        state["resize_refresh_count"] += 1

        try:
            self._rebuild_render_cache(
                state["vte"],
                state,
                "size_allocate_settled",
            )
            self._force_overlay_redraw(
                state["vte"],
                state,
            )
        except Exception as exc:
            self._record_refresh_failure(
                state,
                "size_allocate_settled",
                exc,
            )

        return False

    def on_vte_adjustment_changed(
        self,
        adjustment,
        key,
    ):
        """
        Refresh for genuine manual scrolling, but suppress VTE's
        automatic bottom-follow adjustment traffic.

        When the user manually returns to the bottom, the draw callback's
        row-cache-miss fallback still schedules the required semantic
        refresh for newly visible rows.
        """

        state = self.states.get(key)

        if state is None or not state["active"]:
            return

        try:
            value = float(
                adjustment.get_value()
            )

            upper = float(
                adjustment.get_upper()
            )

            page_size = float(
                adjustment.get_page_size()
            )

            at_bottom = (
                value + page_size
                >= upper - SCROLL_BOTTOM_EPSILON_ROWS
            )

        except Exception:
            # Preserve the existing behavior if adjustment inspection
            # unexpectedly fails.
            at_bottom = False

        if at_bottom:
            state[
                "scroll_at_bottom_suppressed_count"
            ] += 1

            return

        state["scroll_manual_refresh_count"] += 1

        self._schedule_live_refresh(
            key,
            "scroll_adjustment",
        )

    def _schedule_live_refresh(self, key, reason):
        """
        Schedule a bounded trailing-edge semantic refresh.

        Repeated events postpone the cache rebuild until the terminal has
        been quiet for LIVE_REFRESH_DELAY_MS. Continuous activity cannot
        postpone rebuilding beyond LIVE_REFRESH_MAX_LATENCY_MS.
        """

        state = self.states.get(key)

        if state is None or not state["active"]:
            return

        now_ns = time.monotonic_ns()

        state["refresh_request_count"] += 1

        reason_counts = state["refresh_reason_counts"]
        reason_counts[reason] = (
            int(reason_counts.get(reason, 0)) + 1
        )

        state["last_refresh_reason"] = reason
        state["refresh_last_request_ns"] = now_ns
        state["refresh_passes_remaining"] = (
            LIVE_REFRESH_PASS_COUNT
        )

        if state["refresh_source_id"] is not None:
            return

        state["refresh_burst_started_ns"] = now_ns

        state["refresh_source_id"] = GLib.timeout_add(
            LIVE_REFRESH_DELAY_MS,
            self._flush_live_refresh,
            key,
        )

    def _flush_live_refresh(self, key):
        state = self.states.get(key)

        if state is None:
            return False

        if not state["active"]:
            state["refresh_source_id"] = None
            state["refresh_passes_remaining"] = 0
            state["refresh_burst_started_ns"] = 0
            state["refresh_last_request_ns"] = 0
            return False

        now_ns = time.monotonic_ns()

        burst_started_ns = int(
            state.get("refresh_burst_started_ns", now_ns)
            or now_ns
        )

        last_request_ns = int(
            state.get("refresh_last_request_ns", now_ns)
            or now_ns
        )

        quiet_elapsed_ms = (
            now_ns - last_request_ns
        ) / NANOSECONDS_PER_MILLISECOND

        burst_elapsed_ms = (
            now_ns - burst_started_ns
        ) / NANOSECONDS_PER_MILLISECOND

        should_defer = (
            quiet_elapsed_ms < LIVE_REFRESH_DELAY_MS
            and burst_elapsed_ms < LIVE_REFRESH_MAX_LATENCY_MS
        )

        if should_defer:
            quiet_remaining_ms = (
                LIVE_REFRESH_DELAY_MS
                - quiet_elapsed_ms
            )

            max_remaining_ms = (
                LIVE_REFRESH_MAX_LATENCY_MS
                - burst_elapsed_ms
            )

            next_delay_ms = max(
                1,
                int(
                    math.ceil(
                        min(
                            quiet_remaining_ms,
                            max_remaining_ms,
                        )
                    )
                ),
            )

            state["refresh_deferred_count"] += 1

            state["refresh_source_id"] = GLib.timeout_add(
                next_delay_ms,
                self._flush_live_refresh,
                key,
            )

            # Remove the timer that invoked this callback.
            return False

        if (
            quiet_elapsed_ms < LIVE_REFRESH_DELAY_MS
            and burst_elapsed_ms >= LIVE_REFRESH_MAX_LATENCY_MS
        ):
            state["refresh_max_latency_flush_count"] += 1

        state["refresh_flush_count"] += 1

        try:
            self._rebuild_render_cache(
                state["vte"],
                state,
                state["last_refresh_reason"],
            )

            self._force_overlay_redraw(
                state["vte"],
                state,
            )
        except Exception as exc:
            self._record_refresh_failure(
                state,
                "live_refresh",
                exc,
            )
        finally:
            state["refresh_passes_remaining"] = max(
                int(state["refresh_passes_remaining"]) - 1,
                0,
            )

            # A GLib timeout source is removed after this callback returns
            # False. Always clear our bookkeeping too, including failures,
            # so a later terminal event can schedule a fresh source.
            state["refresh_source_id"] = None
            state["refresh_burst_started_ns"] = 0
            state["refresh_last_request_ns"] = 0

        return False

    def _force_overlay_redraw(self, vte, state=None):
        """Force a complete overlay repaint for a windowless VTE widget."""

        width = max(int(vte.get_allocated_width()), 1)
        height = max(int(vte.get_allocated_height()), 1)

        try:
            vte.queue_draw_area(0, 0, width, height)
        except Exception:
            vte.queue_draw()

        parent = vte.get_parent()

        if parent is not None:
            try:
                parent.queue_draw_area(
                    0,
                    0,
                    max(int(parent.get_allocated_width()), 1),
                    max(int(parent.get_allocated_height()), 1),
                )
            except Exception:
                try:
                    parent.queue_draw()
                except Exception:
                    pass

        if state is not None:
            state["forced_redraw_count"] += 1

    @staticmethod
    def _record_profile_timing(
        state,
        prefix,
        elapsed_ns,
    ):
        """Record one monotonic nanosecond timing sample."""

        elapsed_ns = max(int(elapsed_ns), 0)

        count_key = f"{prefix}_count"
        total_key = f"{prefix}_total_ns"
        last_key = f"{prefix}_last_ns"
        max_key = f"{prefix}_max_ns"

        state[count_key] = int(
            state.get(count_key, 0)
        ) + 1

        state[total_key] = int(
            state.get(total_key, 0)
        ) + elapsed_ns

        state[last_key] = elapsed_ns
        state[max_key] = max(
            int(state.get(max_key, 0)),
            elapsed_ns,
        )

    @staticmethod
    def _profile_ns_to_ms(value_ns):
        return (
            float(value_ns)
            / NANOSECONDS_PER_MILLISECOND
        )

    @classmethod
    def _profile_diagnostic_lines(
        cls,
        state,
    ):
        lines = [
            "",
            "PERFORMANCE PROFILE",
            "-" * 72,
            (
                "profile_timing_enabled="
                f"{PROFILE_TIMING_ENABLED}"
            ),
            (
                "command_registry_prewarm_started="
                f"{cls._command_registry_prewarm_started}"
            ),
            (
                "command_registry_prewarm_finished="
                f"{cls._command_registry_prewarm_finished}"
            ),
            (
                "command_registry_build_count="
                f"{cls._command_registry_build_count}"
            ),
            (
                "command_registry_build_ms="
                f"{cls._profile_ns_to_ms(cls._command_registry_build_ns):.6f}"
            ),
        ]

        metrics = (
            (
                "cache_rebuild",
                "profile_cache_rebuild",
            ),
            (
                "geometry",
                "profile_geometry",
            ),
            (
                "row_extract",
                "profile_row_extract",
            ),
            (
                "semantic_detection",
                "profile_semantic",
            ),
            (
                "partition",
                "profile_partition",
            ),
            (
                "draw",
                "profile_draw",
            ),
            (
                "copy",
                "profile_copy",
            ),
        )

        for label, prefix in metrics:
            count = int(
                state.get(f"{prefix}_count", 0)
            )

            total_ns = int(
                state.get(f"{prefix}_total_ns", 0)
            )

            last_ns = int(
                state.get(f"{prefix}_last_ns", 0)
            )

            max_ns = int(
                state.get(f"{prefix}_max_ns", 0)
            )

            average_ns = (
                total_ns / count
                if count
                else 0.0
            )

            lines.extend(
                [
                    f"profile_{label}_count={count}",
                    (
                        f"profile_{label}_last_ms="
                        f"{cls._profile_ns_to_ms(last_ns):.6f}"
                    ),
                    (
                        f"profile_{label}_average_ms="
                        f"{cls._profile_ns_to_ms(average_ns):.6f}"
                    ),
                    (
                        f"profile_{label}_max_ms="
                        f"{cls._profile_ns_to_ms(max_ns):.6f}"
                    ),
                    (
                        f"profile_{label}_total_ms="
                        f"{cls._profile_ns_to_ms(total_ns):.6f}"
                    ),
                ]
            )

        return lines

    @staticmethod
    def _render_signature(vte, geometry):
        adjustment = vte.get_vadjustment()

        return (
            geometry["x"],
            geometry["y"],
            geometry["width"],
            geometry["height"],
            geometry["columns"],
            geometry["rows"],
            geometry["character_width"],
            geometry["character_height"],
            round(float(adjustment.get_value()), 6),
        )

    def _rebuild_render_cache(
        self,
        vte,
        state,
        reason,
    ):
        rebuild_started_ns = (
            time.perf_counter_ns()
            if PROFILE_TIMING_ENABLED
            else 0
        )

        geometry_started_ns = (
            time.perf_counter_ns()
            if PROFILE_TIMING_ENABLED
            else 0
        )

        geometry = self._calculate_geometry(vte)

        if PROFILE_TIMING_ENABLED:
            self._record_profile_timing(
                state,
                "profile_geometry",
                time.perf_counter_ns()
                - geometry_started_ns,
            )

        rows_started_ns = (
            time.perf_counter_ns()
            if PROFILE_TIMING_ENABLED
            else 0
        )

        previous_visual_offset = int(
            state.get(
                "visual_row_coordinate_offset",
                0,
            )
        )

        try:
            visual_resolution = (
                self._resolve_visual_row_coordinates(
                    vte,
                    geometry,
                    preferred_offset=previous_visual_offset,
                )
            )
        except Exception as exc:
            state["visual_coordinate_failure_count"] += 1
            state["visual_coordinate_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
        else:
            state["visual_coordinate_resolution_count"] += 1
            state["visual_coordinate_error"] = ""
            state.update(
                {
                    f"visual_{name}": value
                    for name, value in visual_resolution.items()
                }
            )

        visual_offset = int(
            state.get(
                "visual_row_coordinate_offset",
                previous_visual_offset,
            )
        )

        row_segments = self._calculate_visible_row_segments(
            vte,
            geometry,
            visual_offset,
        )

        if PROFILE_TIMING_ENABLED:
            self._record_profile_timing(
                state,
                "profile_row_extract",
                time.perf_counter_ns()
                - rows_started_ns,
            )

        semantic_started_ns = (
            time.perf_counter_ns()
            if PROFILE_TIMING_ENABLED
            else 0
        )

        semantic_rows = self._calculate_semantic_rows(
            vte,
            geometry,
            row_segments,
        )

        if PROFILE_TIMING_ENABLED:
            self._record_profile_timing(
                state,
                "profile_semantic",
                time.perf_counter_ns()
                - semantic_started_ns,
            )

        state["last_html_command_row_count"] = sum(
            1
            for row in semantic_rows.values()
            if row.get("command_detection_source") == "html"
            and row.get("command_ranges")
        )
        state["last_lexical_command_row_count"] = sum(
            1
            for row in semantic_rows.values()
            if row.get("command_detection_source") == "lexical"
            and row.get("command_ranges")
        )

        partition_started_ns = (
            time.perf_counter_ns()
            if PROFILE_TIMING_ENABLED
            else 0
        )

        (
            ordinary_segments,
            command_segments,
            line_number_segments,
            cache_miss_count,
        ) = self._partition_from_semantic_rows(
            geometry,
            row_segments,
            semantic_rows,
        )

        if PROFILE_TIMING_ENABLED:
            self._record_profile_timing(
                state,
                "profile_partition",
                time.perf_counter_ns()
                - partition_started_ns,
            )

        state["cache_generation"] += 1
        state["cache_refresh_count"] += 1
        state["last_cache_reason"] = reason
        state["last_command_segment_count"] = len(
            command_segments
        )
        state["last_line_number_segment_count"] = len(
            line_number_segments
        )

        state["render_cache"] = {
            "signature": self._render_signature(
                vte,
                geometry,
            ),
            "geometry": geometry,
            "row_segments": row_segments,
            "semantic_rows": semantic_rows,
            "ordinary_segments": ordinary_segments,
            "command_segments": command_segments,
            "line_number_segments": line_number_segments,
            "cache_miss_count": cache_miss_count,
            "generation": state["cache_generation"],
        }

        if PROFILE_TIMING_ENABLED:
            self._record_profile_timing(
                state,
                "profile_cache_rebuild",
                time.perf_counter_ns()
                - rebuild_started_ns,
            )

        return state["render_cache"]

    def on_vte_draw_after(self, vte, cairo_context, key):
        state = self.states.get(key)

        if state is None or not state["active"]:
            return False

        draw_started_ns = (
            time.perf_counter_ns()
            if PROFILE_TIMING_ENABLED
            else 0
        )

        # DRAW_CACHE_BASELINE_V2
        #
        # The v13 exact-signature fast path achieved too few hits to
        # justify its branching and stale-cache restrictions. Restore
        # the proven v12 draw calculation while refresh sources are
        # measured independently.
        geometry = self._calculate_geometry(vte)

        row_segments = self._calculate_visible_row_segments(
            vte,
            geometry,
            state.get(
                "visual_row_coordinate_offset",
                0,
            ),
        )

        cache = state.get("render_cache") or {}

        semantic_rows = cache.get(
            "semantic_rows",
            {},
        )

        (
            ordinary_segments,
            command_segments,
            line_number_segments,
            cache_miss_count,
        ) = self._partition_from_semantic_rows(
            geometry,
            row_segments,
            semantic_rows,
        )

        state["draw_cache_slow_count"] += 1
        state["last_draw_cache_path"] = (
            "baseline_recalculate"
        )

        state["last_draw_cache_miss_count"] = (
            cache_miss_count
        )

        if (
            cache_miss_count
            and state.get("resize_source_id") is None
        ):
            self._schedule_live_refresh(
                key,
                "row_cache_miss",
            )

        selection_rgb = self._resolve_selection_rgb(
            vte
        )

        command_rgb = self._parse_rgb(
            COMMAND_SELECTION_COLOR
        )

        line_number_rgb = self._parse_rgb(
            LINE_NUMBER_SELECTION_COLOR
        )

        cairo_context.save()

        try:
            # Everything remains clipped to the exact VTE cell grid.
            cairo_context.rectangle(
                geometry["x"],
                geometry["y"],
                geometry["width"],
                geometry["height"],
            )

            cairo_context.clip()

            # The blue and semantic segments are mutually exclusive.
            # No blue rectangle exists beneath cyan or green segments.
            self._fill_segments(
                cairo_context,
                ordinary_segments,
                selection_rgb,
                OVERLAY_ALPHA,
                ROW_CORNER_RADIUS_PX,
            )

            self._fill_segments(
                cairo_context,
                line_number_segments,
                line_number_rgb,
                LINE_NUMBER_SELECTION_ALPHA,
                SEMANTIC_CORNER_RADIUS_PX,
            )

            self._fill_segments(
                cairo_context,
                command_segments,
                command_rgb,
                COMMAND_SELECTION_ALPHA,
                SEMANTIC_CORNER_RADIUS_PX,
            )

        finally:
            cairo_context.restore()

        if PROFILE_TIMING_ENABLED:
            self._record_profile_timing(
                state,
                "profile_draw",
                time.perf_counter_ns()
                - draw_started_ns,
            )

        return False

    @classmethod
    def _fill_segments(
        cls,
        cairo_context,
        segments,
        rgb,
        alpha,
        radius,
    ):
        if not segments:
            return

        red, green, blue = rgb

        cairo_context.set_source_rgba(
            red,
            green,
            blue,
            float(alpha),
        )

        for segment in segments:
            cls._append_rounded_rectangle(
                cairo_context,
                segment["x"],
                segment["y"],
                segment["width"],
                segment["height"],
                radius,
            )

        cairo_context.fill()

    @staticmethod
    def _parse_rgb(value):
        rgba = Gdk.RGBA()

        if not rgba.parse(value):
            raise ValueError(
                f"Invalid RGB color: {value}"
            )

        return (
            float(rgba.red),
            float(rgba.green),
            float(rgba.blue),
        )

    @classmethod
    def _extract_terminal_row_html(
        cls,
        vte,
        absolute_row,
        columns,
    ):
        if columns <= 0:
            return ""

        result = vte.get_text_range_format(
            format=Vte.Format.HTML,
            start_row=int(absolute_row),
            start_col=0,
            end_row=int(absolute_row),
            end_col=int(columns - 1),
        )

        return cls._normalize_extracted_text(
            result
        )

    @staticmethod
    def _merge_column_ranges(ranges):
        """
        Merge overlapping or adjacent half-open column ranges.

            [start_column, end_column)
        """

        normalized = sorted(
            (
                int(start),
                int(end),
            )
            for start, end in ranges
            if int(end) > int(start)
        )

        if not normalized:
            return []

        merged = [
            [
                normalized[0][0],
                normalized[0][1],
            ]
        ]

        for start, end in normalized[1:]:
            previous = merged[-1]

            if start <= previous[1]:
                previous[1] = max(
                    previous[1],
                    end,
                )
            else:
                merged.append(
                    [start, end]
                )

        return [
            (start, end)
            for start, end in merged
        ]

    @classmethod
    def _character_range_to_columns(
        cls,
        text,
        start_index,
        end_index,
        maximum_columns,
    ):
        start_column = cls._cell_column_after(
            text[:start_index]
        )

        end_column = cls._cell_column_after(
            text[:end_index]
        )

        maximum_columns = max(
            int(maximum_columns),
            0,
        )

        start_column = min(
            max(start_column, 0),
            maximum_columns,
        )

        end_column = min(
            max(end_column, start_column),
            maximum_columns,
        )

        if end_column <= start_column:
            return None

        return (
            start_column,
            end_column,
        )

    @staticmethod
    def _clamp_column_range(
        column_range,
        minimum_column,
        maximum_column,
    ):
        if column_range is None:
            return None

        start_column, end_column = (
            column_range
        )

        start_column = max(
            int(start_column),
            int(minimum_column),
        )

        end_column = min(
            int(end_column),
            int(maximum_column),
        )

        if end_column <= start_column:
            return None

        return (
            start_column,
            end_column,
        )

    @staticmethod
    def _pixel_segment_from_columns(
        geometry,
        row_segment,
        start_column,
        end_column,
        semantic_type,
    ):
        character_width = geometry[
            "character_width"
        ]

        return {
            "semantic_type": semantic_type,
            "absolute_row": row_segment[
                "absolute_row"
            ],
            "display_row": row_segment[
                "display_row"
            ],
            "start_column": start_column,
            "end_column": end_column,
            "x": (
                geometry["x"]
                + start_column
                * character_width
            ),
            "y": row_segment["y"],
            "width": (
                end_column - start_column
            ) * character_width,
            "height": row_segment["height"],
        }

    @classmethod
    def _find_line_number_ranges(
        cls,
        row_text,
        maximum_columns,
    ):
        match = LINE_NUMBER_PATTERN.match(
            row_text
        )

        if match is None:
            return []

        column_range = (
            cls._character_range_to_columns(
                row_text,
                match.start(2),
                match.end(2),
                maximum_columns,
            )
        )

        if column_range is None:
            return []

        return [column_range]

    @classmethod
    def _build_command_registry(cls):
        """Scan PATH and return executable names plus shell builtins."""

        names = set(COMMON_SHELL_COMMANDS)

        for directory in os.get_exec_path():
            if not directory:
                continue

            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_file(follow_symlinks=True):
                                continue

                            if os.access(entry.path, os.X_OK):
                                names.add(entry.name)
                        except OSError:
                            continue
            except OSError:
                continue

        return frozenset(names)

    @classmethod
    def _command_registry_prewarm_worker(cls):
        try:
            cls._get_command_registry()
        finally:
            cls._command_registry_prewarm_finished = True

    @classmethod
    def _start_command_registry_prewarm(cls):
        """Start one daemon worker for the process-wide command registry."""

        if cls._command_registry is not None:
            cls._command_registry_prewarm_finished = True
            return

        with cls._command_registry_lock:
            if cls._command_registry_prewarm_started:
                return

            cls._command_registry_prewarm_started = True

        worker = threading.Thread(
            target=cls._command_registry_prewarm_worker,
            name="terminator-fullselect-command-registry",
            daemon=True,
        )
        worker.start()

    @classmethod
    def _get_command_registry(cls):
        """Return a thread-safe cached set of commands and executables."""

        if cls._command_registry is not None:
            return cls._command_registry

        with cls._command_registry_lock:
            if cls._command_registry is not None:
                return cls._command_registry

            registry_started_ns = (
                time.perf_counter_ns()
                if PROFILE_TIMING_ENABLED
                else 0
            )

            cls._command_registry = cls._build_command_registry()

            if PROFILE_TIMING_ENABLED:
                cls._command_registry_build_count += 1
                cls._command_registry_build_ns = (
                    time.perf_counter_ns()
                    - registry_started_ns
                )

        return cls._command_registry

    @staticmethod
    def _command_body_start(row_text):
        """Return the character index where a shell command may begin."""

        prompt_match = PROMPT_PREFIX_PATTERN.match(row_text)

        if prompt_match is not None:
            cursor = prompt_match.end()
        else:
            cursor = len(row_text) - len(row_text.lstrip())

        while cursor < len(row_text) and row_text[cursor].isspace():
            cursor += 1

        return cursor

    @classmethod
    def _is_registered_command(cls, token):
        """Check a shell token without executing the user's shell."""

        cleaned = token.strip()

        if not cleaned:
            return False

        if "/" in cleaned:
            expanded = os.path.expanduser(cleaned)
            return os.path.isfile(expanded) and os.access(expanded, os.X_OK)

        return cleaned in cls._get_command_registry()

    @classmethod
    def _find_lexical_command_ranges(
        cls,
        row_text,
        maximum_columns,
    ):
        """
        Detect the first executable shell token deterministically.

        This is a fallback for rows where VTE's HTML export does not
        expose the shell's foreground-color span. It recognises commands
        after the current prompt, optional variable assignments, and a
        short chain of wrappers such as sudo/env/command.
        """

        if not COMMAND_LEXICAL_FALLBACK_ENABLED:
            return []

        cursor = cls._command_body_start(row_text)
        character_ranges = []
        wrapper_budget = 4

        while cursor < len(row_text):
            while cursor < len(row_text) and row_text[cursor].isspace():
                cursor += 1

            match = COMMAND_TOKEN_PATTERN.match(row_text, cursor)

            if match is None:
                break

            token = match.group(0)
            cursor = match.end()

            # Environment assignments precede the actual command.
            if SHELL_ASSIGNMENT_PATTERN.match(token):
                continue

            # Skip redirection-only or option-like tokens while looking
            # for the command following a wrapper.
            if token.startswith(("-", ">", "<")):
                continue

            normalized = token.rstrip(":")

            if not cls._is_registered_command(normalized):
                break

            column_range = cls._character_range_to_columns(
                row_text,
                match.start(),
                match.start() + len(normalized),
                maximum_columns,
            )

            if column_range is not None:
                character_ranges.append(column_range)

            if normalized not in COMMAND_WRAPPERS:
                break

            wrapper_budget -= 1

            if wrapper_budget <= 0:
                break

        return cls._merge_column_ranges(character_ranges)

    @classmethod
    def _find_command_ranges(
        cls,
        html_text,
        maximum_columns,
    ):
        parser = _VteHtmlColorParser()

        try:
            parser.feed(html_text)
            parser.close()
        except Exception:
            return []

        ranges = []

        for span in parser.color_spans:
            if (
                span["color"]
                not in COMMAND_SOURCE_COLORS
            ):
                continue

            column_range = (
                cls._character_range_to_columns(
                    parser.text,
                    span["start"],
                    span["end"],
                    maximum_columns,
                )
            )

            if column_range is not None:
                ranges.append(column_range)

        return cls._merge_column_ranges(
            ranges
        )

    @classmethod
    def _subtract_column_ranges(
        cls,
        base_start,
        base_end,
        excluded_ranges,
    ):
        """
        Remove semantic ranges from one blue row range.

        The returned ranges never overlap semantic ranges, ensuring
        that cyan and green backgrounds have no blue underneath.
        """

        excluded_ranges = (
            cls._merge_column_ranges(
                excluded_ranges
            )
        )

        output = []
        cursor = int(base_start)
        base_end = int(base_end)

        for start_column, end_column in (
            excluded_ranges
        ):
            start_column = max(
                start_column,
                int(base_start),
            )

            end_column = min(
                end_column,
                base_end,
            )

            if end_column <= start_column:
                continue

            if start_column > cursor:
                output.append(
                    (
                        cursor,
                        start_column,
                    )
                )

            cursor = max(
                cursor,
                end_column,
            )

        if cursor < base_end:
            output.append(
                (
                    cursor,
                    base_end,
                )
            )

        return output

    @classmethod
    def _calculate_semantic_rows(
        cls,
        vte,
        geometry,
        row_segments,
    ):
        maximum_columns = geometry["columns"]
        semantic_rows = {}

        for row_segment in row_segments:
            row_text = row_segment["row_text"]
            base_start = row_segment["start_column"]
            base_end = row_segment["end_column"]

            line_ranges = cls._find_line_number_ranges(
                row_text,
                maximum_columns,
            )

            command_ranges = []
            command_detection_source = "none"

            if COMMAND_HTML_DETECTION_ENABLED:
                try:
                    html_text = cls._extract_terminal_row_html(
                        vte,
                        row_segment["absolute_row"],
                        maximum_columns,
                    )

                    command_ranges = cls._find_command_ranges(
                        html_text,
                        maximum_columns,
                    )

                except Exception:
                    command_ranges = []

                if command_ranges:
                    command_detection_source = "html"

            if (
                not command_ranges
                and COMMAND_LEXICAL_FALLBACK_ENABLED
            ):
                command_ranges = cls._find_lexical_command_ranges(
                    row_text,
                    maximum_columns,
                )

                command_detection_source = (
                    "lexical" if command_ranges else "none"
                )

            line_ranges = [
                clamped
                for value in line_ranges
                if (
                    clamped := cls._clamp_column_range(
                        value,
                        base_start,
                        base_end,
                    )
                ) is not None
            ]

            command_ranges = [
                clamped
                for value in command_ranges
                if (
                    clamped := cls._clamp_column_range(
                        value,
                        base_start,
                        base_end,
                    )
                ) is not None
            ]

            line_without_commands = []

            for line_start, line_end in line_ranges:
                line_without_commands.extend(
                    cls._subtract_column_ranges(
                        line_start,
                        line_end,
                        command_ranges,
                    )
                )

            semantic_rows[row_segment["absolute_row"]] = {
                "row_text": row_text,
                "command_ranges": cls._merge_column_ranges(
                    command_ranges
                ),
                "command_detection_source": command_detection_source,
                "line_ranges": cls._merge_column_ranges(
                    line_without_commands
                ),
            }

        return semantic_rows

    @classmethod
    def _partition_from_semantic_rows(
        cls,
        geometry,
        row_segments,
        semantic_rows,
    ):
        ordinary_segments = []
        command_segments = []
        line_number_segments = []
        cache_miss_count = 0
        maximum_columns = geometry["columns"]

        for row_segment in row_segments:
            row_text = row_segment["row_text"]
            base_start = row_segment["start_column"]
            base_end = row_segment["end_column"]
            cached = semantic_rows.get(
                row_segment["absolute_row"]
            )

            if (
                cached is None
                or cached.get("row_text") != row_text
            ):
                cache_miss_count += 1
                command_ranges = []
                line_ranges = cls._find_line_number_ranges(
                    row_text,
                    maximum_columns,
                )
            else:
                command_ranges = list(
                    cached.get("command_ranges", [])
                )
                line_ranges = list(
                    cached.get("line_ranges", [])
                )

            command_ranges = [
                clamped
                for value in command_ranges
                if (
                    clamped := cls._clamp_column_range(
                        value,
                        base_start,
                        base_end,
                    )
                ) is not None
            ]

            line_ranges = [
                clamped
                for value in line_ranges
                if (
                    clamped := cls._clamp_column_range(
                        value,
                        base_start,
                        base_end,
                    )
                ) is not None
            ]

            ordinary_ranges = cls._subtract_column_ranges(
                base_start,
                base_end,
                command_ranges + line_ranges,
            )

            for start_column, end_column in ordinary_ranges:
                ordinary_segments.append(
                    cls._pixel_segment_from_columns(
                        geometry,
                        row_segment,
                        start_column,
                        end_column,
                        "ordinary",
                    )
                )

            for start_column, end_column in line_ranges:
                line_number_segments.append(
                    cls._pixel_segment_from_columns(
                        geometry,
                        row_segment,
                        start_column,
                        end_column,
                        "line_number",
                    )
                )

            for start_column, end_column in command_ranges:
                command_segments.append(
                    cls._pixel_segment_from_columns(
                        geometry,
                        row_segment,
                        start_column,
                        end_column,
                        "command",
                    )
                )

        return (
            ordinary_segments,
            command_segments,
            line_number_segments,
            cache_miss_count,
        )

    @classmethod
    def _calculate_semantic_partition(
        cls,
        vte,
        geometry,
        row_segments,
    ):
        semantic_rows = cls._calculate_semantic_rows(
            vte,
            geometry,
            row_segments,
        )

        (
            ordinary_segments,
            command_segments,
            line_number_segments,
            _cache_miss_count,
        ) = cls._partition_from_semantic_rows(
            geometry,
            row_segments,
            semantic_rows,
        )

        return (
            ordinary_segments,
            command_segments,
            line_number_segments,
        )

    @staticmethod
    def _append_rounded_rectangle(
        cairo_context,
        x,
        y,
        width,
        height,
        requested_radius,
    ):
        """
        Append one independently closed rounded rectangle.

        Radius is bounded by half the rectangle's width and height,
        preventing invalid or self-intersecting geometry.
        """

        width = float(width)
        height = float(height)

        if width <= 0.0 or height <= 0.0:
            return

        radius = min(
            max(float(requested_radius), 0.0),
            width / 2.0,
            height / 2.0,
        )

        if radius <= 0.0:
            cairo_context.rectangle(
                float(x),
                float(y),
                width,
                height,
            )
            return

        x = float(x)
        y = float(y)

        right = x + width
        bottom = y + height

        cairo_context.new_sub_path()

        cairo_context.arc(
            right - radius,
            y + radius,
            radius,
            -math.pi / 2.0,
            0.0,
        )

        cairo_context.arc(
            right - radius,
            bottom - radius,
            radius,
            0.0,
            math.pi / 2.0,
        )

        cairo_context.arc(
            x + radius,
            bottom - radius,
            radius,
            math.pi / 2.0,
            math.pi,
        )

        cairo_context.arc(
            x + radius,
            y + radius,
            radius,
            math.pi,
            3.0 * math.pi / 2.0,
        )

        cairo_context.close_path()

    @staticmethod
    def _normalize_extracted_text(result):
        if isinstance(result, (tuple, list)):
            result = result[0]

        if result is None:
            return ""

        if isinstance(result, bytes):
            result = result.decode(
                "utf-8",
                errors="replace",
            )

        return str(result).rstrip("\r\n")

    @classmethod
    def _extract_terminal_row(
        cls,
        vte,
        absolute_row,
        columns,
    ):
        """
        Read exactly one logical terminal row.

        Modern VTE returns the requested range as UTF-8 text.
        """

        if columns <= 0:
            return ""

        result = vte.get_text_range_format(
            format=Vte.Format.TEXT,
            start_row=int(absolute_row),
            start_col=0,
            end_row=int(absolute_row),
            end_col=int(columns - 1),
        )

        return cls._normalize_extracted_text(result)

    @staticmethod
    def _is_meaningful_character(character):
        """
        Return True only for a character that produces visible content.

        Ordinary spaces, tabs, line separators, NUL characters,
        combining-only code points, and formatting controls do not begin
        or end a highlight band.
        """

        if (
            character == "\x00"
            or character.isspace()
        ):
            return False

        category = unicodedata.category(character)

        return category not in {
            "Mn",
            "Me",
            "Cf",
        }

    @staticmethod
    def _character_cell_width(character):
        """
        Deterministic terminal-cell width approximation.

        - combining and formatting characters: 0 cells
        - East Asian wide/full-width characters: 2 cells
        - ordinary printable characters: 1 cell
        """

        if character == "\x00":
            return 0

        category = unicodedata.category(character)

        if (
            unicodedata.combining(character)
            or category in {"Mn", "Me", "Cf"}
            or category.startswith("C")
        ):
            return 0

        if unicodedata.east_asian_width(character) in {
            "W",
            "F",
        }:
            return 2

        return 1

    @classmethod
    def _cell_column_after(
        cls,
        value,
        initial_column=0,
    ):
        column = max(int(initial_column), 0)

        for character in value:
            if character == "\t":
                remainder = column % TAB_STOP_COLUMNS

                if remainder == 0:
                    column += TAB_STOP_COLUMNS
                else:
                    column += (
                        TAB_STOP_COLUMNS - remainder
                    )

                continue

            column += cls._character_cell_width(
                character
            )

        return column

    @classmethod
    def _row_content_columns(
        cls,
        row_text,
        maximum_columns,
    ):
        """
        Return a half-open terminal-cell range:

            [start_column, end_column)

        Empty rows return None.
        """

        meaningful_indexes = [
            index
            for index, character in enumerate(row_text)
            if cls._is_meaningful_character(character)
        ]

        if not meaningful_indexes:
            return None

        first_index = meaningful_indexes[0]
        last_index = meaningful_indexes[-1]

        start_column = cls._cell_column_after(
            row_text[:first_index]
        )

        end_column = cls._cell_column_after(
            row_text[:last_index + 1]
        )

        maximum_columns = max(
            int(maximum_columns),
            0,
        )

        start_column = min(
            max(start_column, 0),
            maximum_columns,
        )

        end_column = min(
            max(end_column, start_column + 1),
            maximum_columns,
        )

        if (
            start_column >= maximum_columns
            or end_column <= start_column
        ):
            return None

        return (
            start_column,
            end_column,
        )

    @classmethod
    def _calculate_visible_row_segments(
        cls,
        vte,
        geometry,
        row_coordinate_offset=0,
    ):
        """
        Convert visible logical terminal rows into exact pixel bands.

        Smooth-scroll fractions are converted into one integer pixel
        offset. This avoids blurred half-pixel rectangle edges.
        """

        rows = geometry["rows"]
        columns = geometry["columns"]

        character_width = geometry["character_width"]
        character_height = geometry["character_height"]

        if (
            rows <= 0
            or columns <= 0
            or character_width <= 0
            or character_height <= 0
        ):
            return []

        adjustment = vte.get_vadjustment()
        scroll_value = float(adjustment.get_value())

        adjustment_top_row = math.floor(
            scroll_value
        )
        top_absolute_row = (
            adjustment_top_row
            + int(row_coordinate_offset)
        )

        fractional_row = (
            scroll_value - adjustment_top_row
        )

        pixel_scroll_offset = int(
            round(
                fractional_row
                * character_height
            )
        )

        # Rounding a value extremely close to the next row can produce
        # exactly one complete character height.
        if pixel_scroll_offset >= character_height:
            top_absolute_row += 1
            pixel_scroll_offset = 0

        row_gap = min(
            max(int(ROW_GAP_PX), 0),
            character_height - 1,
        )

        upper_gap = row_gap // 2

        band_height = max(
            character_height - row_gap,
            1,
        )

        # When the view is fractionally scrolled, one extra row can be
        # partially visible at the lower edge.
        iteration_count = rows

        if pixel_scroll_offset:
            iteration_count += 1

        segments = []

        for display_row in range(iteration_count):
            absolute_row = (
                top_absolute_row + display_row
            )

            try:
                row_text = cls._extract_terminal_row(
                    vte,
                    absolute_row,
                    columns,
                )
            except Exception:
                # A row can disappear between text extraction and
                # repaint while the terminal is producing output.
                continue

            content_columns = (
                cls._row_content_columns(
                    row_text,
                    columns,
                )
            )

            if content_columns is None:
                continue

            start_column, end_column = (
                content_columns
            )

            raw_width = (
                end_column - start_column
            ) * character_width

            horizontal_inset = min(
                max(
                    int(ROW_HORIZONTAL_INSET_PX),
                    0,
                ),
                max(
                    (raw_width - 1) // 2,
                    0,
                ),
            )

            x = (
                geometry["x"]
                + start_column * character_width
                + horizontal_inset
            )

            y = (
                geometry["y"]
                + display_row * character_height
                - pixel_scroll_offset
                + upper_gap
            )

            width = (
                raw_width
                - 2 * horizontal_inset
            )

            if width <= 0:
                continue

            # Completely off-screen rows do not need a Cairo path.
            grid_top = geometry["y"]

            grid_bottom = (
                geometry["y"]
                + geometry["height"]
            )

            if (
                y + band_height <= grid_top
                or y >= grid_bottom
            ):
                continue

            segments.append(
                {
                    "absolute_row": absolute_row,
                    "display_row": display_row,
                    "start_column": start_column,
                    "end_column": end_column,
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": band_height,
                    "row_text": row_text,
                }
            )

        return segments

    @staticmethod
    def _calculate_geometry(vte):
        """
        Calculate the VTE cell-grid rectangle from GTK's CSS box model.

        VTE anchors its grid at the widget's left/top border plus padding.
        Any extra pixels caused by integer rows/columns remain on the
        right and bottom. Centering that remainder was why the overlay
        drifted when the window was maximized.
        """

        allocated_width = max(
            int(vte.get_allocated_width()),
            0,
        )

        allocated_height = max(
            int(vte.get_allocated_height()),
            0,
        )

        columns = max(
            int(vte.get_column_count()),
            0,
        )

        rows = max(
            int(vte.get_row_count()),
            0,
        )

        character_width = max(
            int(vte.get_char_width()),
            0,
        )

        character_height = max(
            int(vte.get_char_height()),
            0,
        )

        context = vte.get_style_context()
        state_flags = context.get_state()

        try:
            padding = context.get_padding(
                state_flags
            )
        except Exception:
            padding = Gtk.Border()

        try:
            border = context.get_border(
                state_flags
            )
        except Exception:
            border = Gtk.Border()

        padding_left = max(
            int(getattr(padding, "left", 0)),
            0,
        )
        padding_right = max(
            int(getattr(padding, "right", 0)),
            0,
        )
        padding_top = max(
            int(getattr(padding, "top", 0)),
            0,
        )
        padding_bottom = max(
            int(getattr(padding, "bottom", 0)),
            0,
        )

        border_left = max(
            int(getattr(border, "left", 0)),
            0,
        )
        border_right = max(
            int(getattr(border, "right", 0)),
            0,
        )
        border_top = max(
            int(getattr(border, "top", 0)),
            0,
        )
        border_bottom = max(
            int(getattr(border, "bottom", 0)),
            0,
        )

        style_left = border_left + padding_left
        style_right = border_right + padding_right
        style_top = border_top + padding_top
        style_bottom = border_bottom + padding_bottom

        mathematical_grid_width = (
            columns * character_width
        )

        mathematical_grid_height = (
            rows * character_height
        )

        available_width = max(
            allocated_width
            - style_left
            - style_right,
            0,
        )

        available_height = max(
            allocated_height
            - style_top
            - style_bottom,
            0,
        )

        grid_width = min(
            mathematical_grid_width,
            available_width,
        )

        grid_height = min(
            mathematical_grid_height,
            available_height,
        )

        x = min(
            style_left,
            max(allocated_width - grid_width, 0),
        )

        y = min(
            style_top,
            max(allocated_height - grid_height, 0),
        )

        remaining_width = max(
            allocated_width - grid_width,
            0,
        )

        remaining_height = max(
            allocated_height - grid_height,
            0,
        )

        right_remainder = max(
            allocated_width - x - grid_width,
            0,
        )

        bottom_remainder = max(
            allocated_height - y - grid_height,
            0,
        )

        return {
            "x": x,
            "y": y,
            "width": grid_width,
            "height": grid_height,
            "allocated_width": allocated_width,
            "allocated_height": allocated_height,
            "columns": columns,
            "rows": rows,
            "character_width": character_width,
            "character_height": character_height,
            "mathematical_grid_width": mathematical_grid_width,
            "mathematical_grid_height": mathematical_grid_height,
            "remaining_width": remaining_width,
            "remaining_height": remaining_height,
            "right_remainder": right_remainder,
            "bottom_remainder": bottom_remainder,
            "scale_factor": int(vte.get_scale_factor()),
            "padding_left": padding_left,
            "padding_right": padding_right,
            "padding_top": padding_top,
            "padding_bottom": padding_bottom,
            "border_left": border_left,
            "border_right": border_right,
            "border_top": border_top,
            "border_bottom": border_bottom,
            "style_left": style_left,
            "style_right": style_right,
            "style_top": style_top,
            "style_bottom": style_bottom,
            "origin_model": "gtk_css_border_plus_padding",
        }

    @staticmethod
    def _resolve_selection_rgb(vte):
        """
        Prefer the active GTK theme's selected-background color.

        Return RGB only. The plugin controls opacity independently.
        """

        context = vte.get_style_context()

        color_names = (
            "theme_selected_bg_color",
            "selected_bg_color",
            "accent_bg_color",
        )

        for color_name in color_names:
            try:
                found, rgba = context.lookup_color(color_name)

                if found:
                    return (
                        float(rgba.red),
                        float(rgba.green),
                        float(rgba.blue),
                    )

            except Exception:
                continue

        fallback = Gdk.RGBA()

        if fallback.parse(FALLBACK_SELECTION_COLOR):
            return (
                float(fallback.red),
                float(fallback.green),
                float(fallback.blue),
            )

        # Last-resort mathematically normalized RGB blue.
        return (
            53.0 / 255.0,
            132.0 / 255.0,
            228.0 / 255.0,
        )

    # DIAGNOSTIC_FAILURE_ISOLATION_V1
    def _safe_write_diagnostic(self, vte, active, action):
        """Keep optional diagnostics outside functional callbacks."""

        state = self.states.get(id(vte))

        if state is not None:
            state["diagnostic_attempt_count"] += 1

        try:
            self._write_diagnostic(
                vte,
                active,
                action,
            )
        except Exception as exc:
            if state is not None:
                state["diagnostic_failure_count"] += 1
                state["last_diagnostic_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

            return False

        return True

    def _write_diagnostic(self, vte, active, action):
        state = self.states.get(
            id(vte),
            {},
        )

        geometry = self._calculate_geometry(vte)

        cache = state.get("render_cache")

        if cache is not None:
            segments = cache["row_segments"]
            ordinary_segments = cache[
                "ordinary_segments"
            ]
            command_segments = cache[
                "command_segments"
            ]
            line_number_segments = cache[
                "line_number_segments"
            ]
        else:
            segments = self._calculate_visible_row_segments(
                vte,
                geometry,
                state.get(
                    "visual_row_coordinate_offset",
                    0,
                ),
            )

            (
                ordinary_segments,
                command_segments,
                line_number_segments,
            ) = self._calculate_semantic_partition(
                vte,
                geometry,
                segments,
            )

        red, green, blue = self._resolve_selection_rgb(vte)

        lines = [
            "TERMINATOR FULL-SELECTION VISUAL PROTOTYPE",
            "=" * 72,
            f"last_action={action}",
            f"active={active}",
            (
                "diagnostic_attempt_count="
                f"{state.get('diagnostic_attempt_count', 0)}"
            ),
            (
                "diagnostic_failure_count="
                f"{state.get('diagnostic_failure_count', 0)}"
            ),
            (
                "last_diagnostic_error="
                f"{state.get('last_diagnostic_error', '')}"
            ),
            (
                "live_refresh_signals="
                "key-press,commit,cursor-moved,"
                "contents-changed,adjustment-value-changed"
            ),
            (
                "live_refresh_delay_ms="
                f"{LIVE_REFRESH_DELAY_MS}"
            ),
            (
                "live_refresh_max_latency_ms="
                f"{LIVE_REFRESH_MAX_LATENCY_MS}"
            ),
            (
                "live_refresh_pass_count="
                f"{LIVE_REFRESH_PASS_COUNT}"
            ),
            (
                "resize_settle_delay_ms="
                f"{RESIZE_SETTLE_DELAY_MS}"
            ),
            (
                "refresh_passes_remaining="
                f"{state.get('refresh_passes_remaining', 0)}"
            ),
            (
                "auto_refresh_requests="
                f"{state.get('refresh_request_count', 0)}"
            ),
            (
                "refresh_deferred_count="
                f"{state.get('refresh_deferred_count', 0)}"
            ),
            (
                "refresh_max_latency_flush_count="
                f"{state.get('refresh_max_latency_flush_count', 0)}"
            ),
            (
                "refresh_failure_count="
                f"{state.get('refresh_failure_count', 0)}"
            ),
            (
                "last_refresh_failure_context="
                f"{state.get('last_refresh_failure_context', '')}"
            ),
            (
                "last_refresh_error="
                f"{state.get('last_refresh_error', '')}"
            ),
            (
                "refresh_reason_key_press="
                f"{state.get('refresh_reason_counts', {}).get('key_press', 0)}"
            ),
            (
                "refresh_reason_commit="
                f"{state.get('refresh_reason_counts', {}).get('commit', 0)}"
            ),
            (
                "refresh_reason_cursor_moved="
                f"{state.get('refresh_reason_counts', {}).get('cursor_moved', 0)}"
            ),
            (
                "refresh_reason_contents_changed="
                f"{state.get('refresh_reason_counts', {}).get('contents_changed', 0)}"
            ),
            (
                "refresh_reason_scroll_adjustment="
                f"{state.get('refresh_reason_counts', {}).get('scroll_adjustment', 0)}"
            ),
            (
                "scroll_at_bottom_suppressed_count="
                f"{state.get('scroll_at_bottom_suppressed_count', 0)}"
            ),
            (
                "scroll_manual_refresh_count="
                f"{state.get('scroll_manual_refresh_count', 0)}"
            ),
            (
                "scroll_bottom_epsilon_rows="
                f"{SCROLL_BOTTOM_EPSILON_ROWS}"
            ),
            (
                "refresh_reason_row_cache_miss="
                f"{state.get('refresh_reason_counts', {}).get('row_cache_miss', 0)}"
            ),
            (
                "refresh_reason_selection_activated="
                f"{state.get('refresh_reason_counts', {}).get('selection_activated', 0)}"
            ),
            (
                "auto_refresh_flushes="
                f"{state.get('refresh_flush_count', 0)}"
            ),
            (
                "last_auto_refresh_reason="
                f"{state.get('last_refresh_reason', 'none')}"
            ),
            (
                "cache_generation="
                f"{state.get('cache_generation', 0)}"
            ),
            (
                "cache_refresh_count="
                f"{state.get('cache_refresh_count', 0)}"
            ),
            (
                "last_cache_reason="
                f"{state.get('last_cache_reason', 'none')}"
            ),
            (
                "forced_redraw_count="
                f"{state.get('forced_redraw_count', 0)}"
            ),
            (
                "resize_refresh_count="
                f"{state.get('resize_refresh_count', 0)}"
            ),
            (
                "last_draw_cache_miss_count="
                f"{state.get('last_draw_cache_miss_count', 0)}"
            ),
            (
                "draw_cache_hit_count="
                f"{state.get('draw_cache_hit_count', 0)}"
            ),
            (
                "draw_cache_slow_count="
                f"{state.get('draw_cache_slow_count', 0)}"
            ),
            (
                "last_draw_cache_path="
                f"{state.get('last_draw_cache_path', 'none')}"
            ),
            "draw_cache_mode=baseline_no_fast_path",
            (
                "last_command_segment_count="
                f"{state.get('last_command_segment_count', 0)}"
            ),
            (
                "last_line_number_segment_count="
                f"{state.get('last_line_number_segment_count', 0)}"
            ),
            (
                "native_copy_menu_integration="
                f"{NATIVE_COPY_MENU_INTEGRATION}"
            ),
            (
                "last_native_copy_menu_found="
                f"{state.get('last_native_copy_menu_found', False)}"
            ),
            (
                "native_copy_menu_wire_count="
                f"{state.get('native_copy_menu_wire_count', 0)}"
            ),
            (
                "trim_leading_empty_rows="
                f"{TRIM_LEADING_EMPTY_ROWS}"
            ),
            (
                "trim_trailing_empty_rows="
                f"{TRIM_TRAILING_EMPTY_ROWS}"
            ),
            (
                "copy_count="
                f"{state.get('copy_count', 0)}"
            ),
            (
                "last_copy_action="
                f"{state.get('last_copy_action', 'none')}"
            ),
            (
                "last_copy_characters="
                f"{state.get('last_copy_characters', 0)}"
            ),
            (
                "last_copy_lines="
                f"{state.get('last_copy_lines', 0)}"
            ),
            (
                "last_copy_start_row="
                f"{state.get('last_copy_start_row', 0)}"
            ),
            (
                "last_copy_end_row="
                f"{state.get('last_copy_end_row', -1)}"
            ),
            (
                "last_copy_error="
                f"{state.get('last_copy_error', '')}"
            ),
            *[
                f"copy_row_coordinate_offset={state.get('copy_row_coordinate_offset', 0)}",
                f"copy_coordinate_resolution_count={state.get('copy_coordinate_resolution_count', 0)}",
                f"copy_coordinate_failure_count={state.get('copy_coordinate_failure_count', 0)}",
                f"copy_coordinate_match_score={state.get('copy_coordinate_match_score', 0.0):.9f}",
                f"copy_coordinate_matching_line_count={state.get('copy_coordinate_matching_line_count', 0)}",
                f"copy_coordinate_visible_line_count={state.get('copy_coordinate_visible_line_count', 0)}",
                f"copy_coordinate_validation={state.get('copy_coordinate_validation', 'none')}",
                f"copy_coordinate_window_mode={state.get('copy_coordinate_window_mode', 'none')}",
                f"copy_coordinate_preferred_offset={state.get('copy_coordinate_preferred_offset', 0)}",
                f"copy_coordinate_preferred_top={state.get('copy_coordinate_preferred_top', 0)}",
                f"copy_coordinate_preferred_candidate_available={state.get('copy_coordinate_preferred_candidate_available', False)}",
                f"copy_coordinate_preferred_candidate_used={state.get('copy_coordinate_preferred_candidate_used', False)}",
                f"copy_adjustment_top={state.get('copy_adjustment_top', 0)}",
                f"copy_absolute_top={state.get('copy_absolute_top', 0)}",
                f"copy_cursor_absolute_row={state.get('copy_cursor_absolute_row', -1)}",
                f"copy_mapped_start_row={state.get('copy_mapped_start_row', 0)}",
                f"copy_mapped_end_row={state.get('copy_mapped_end_row', -1)}",
            ],
            *[
                f"visual_row_coordinate_offset={state.get('visual_row_coordinate_offset', 0)}",
                f"visual_coordinate_resolution_count={state.get('visual_coordinate_resolution_count', 0)}",
                f"visual_coordinate_failure_count={state.get('visual_coordinate_failure_count', 0)}",
                f"visual_coordinate_validation={state.get('visual_coordinate_validation', 'none')}",
                f"visual_coordinate_window_mode={state.get('visual_coordinate_window_mode', 'none')}",
                f"visual_coordinate_visible_row_count={state.get('visual_coordinate_visible_row_count', 0)}",
                f"visual_coordinate_logical_line_count={state.get('visual_coordinate_logical_line_count', 0)}",
                f"visual_coordinate_nonempty_row_count={state.get('visual_coordinate_nonempty_row_count', 0)}",
                f"visual_coordinate_exact_candidate_count={state.get('visual_coordinate_exact_candidate_count', 0)}",
                f"visual_adjustment_top={state.get('visual_adjustment_top', 0)}",
                f"visual_absolute_top={state.get('visual_absolute_top', 0)}",
                f"visual_cursor_absolute_row={state.get('visual_cursor_absolute_row', -1)}",
                f"visual_cursor_display_row={state.get('visual_cursor_display_row', -1)}",
                f"visual_coordinate_error={state.get('visual_coordinate_error', '')}",
            ],
            *self._profile_diagnostic_lines(
                state
            ),
            "toggle_shortcut=Ctrl+Shift+Alt+S",
            "escape_clears_only_while_active=True",
            f"overlay_alpha={OVERLAY_ALPHA}",
            "render_mode=content_fitted_row_bands",
            f"row_gap_px={ROW_GAP_PX}",
            f"row_corner_radius_px={ROW_CORNER_RADIUS_PX}",
            f"row_horizontal_inset_px={ROW_HORIZONTAL_INSET_PX}",
            f"renderer_build_id={RENDERER_BUILD_ID}",
            "semantic_partition=exclusive",
            "semantic_renderer=enabled",
            (
                "command_source_colors="
                f"{sorted(COMMAND_SOURCE_COLORS)}"
            ),
            (
                "command_detection_mode="
                + (
                    "html_then_lexical_fallback"
                    if COMMAND_HTML_DETECTION_ENABLED
                    else "lexical_only"
                )
            ),
            (
                "command_html_detection_enabled="
                f"{COMMAND_HTML_DETECTION_ENABLED}"
            ),
            (
                "command_registry_size="
                f"{len(self._get_command_registry())}"
            ),
            (
                "last_html_command_row_count="
                f"{state.get('last_html_command_row_count', 0)}"
            ),
            (
                "last_lexical_command_row_count="
                f"{state.get('last_lexical_command_row_count', 0)}"
            ),
            (
                "command_selection_color="
                f"{COMMAND_SELECTION_COLOR}"
            ),
            (
                "line_number_selection_color="
                f"{LINE_NUMBER_SELECTION_COLOR}"
            ),
            (
                "command_selection_alpha="
                f"{COMMAND_SELECTION_ALPHA}"
            ),
            (
                "line_number_selection_alpha="
                f"{LINE_NUMBER_SELECTION_ALPHA}"
            ),
            "",
            "COLOR",
            "-" * 72,
            f"red={red:.9f}",
            f"green={green:.9f}",
            f"blue={blue:.9f}",
            "",
            "ALLOCATION",
            "-" * 72,
            f"allocated_width={geometry['allocated_width']}",
            f"allocated_height={geometry['allocated_height']}",
            f"scale_factor={geometry['scale_factor']}",
            (
                "grid_origin_model="
                f"{geometry['origin_model']}"
            ),
            (
                "gtk_padding="
                f"{geometry['padding_left']},"
                f"{geometry['padding_top']},"
                f"{geometry['padding_right']},"
                f"{geometry['padding_bottom']}"
            ),
            (
                "gtk_border="
                f"{geometry['border_left']},"
                f"{geometry['border_top']},"
                f"{geometry['border_right']},"
                f"{geometry['border_bottom']}"
            ),
            "",
            "CELL GRID",
            "-" * 72,
            f"columns={geometry['columns']}",
            f"rows={geometry['rows']}",
            f"character_width={geometry['character_width']}",
            f"character_height={geometry['character_height']}",
            (
                "mathematical_grid_width="
                f"{geometry['mathematical_grid_width']}"
            ),
            (
                "mathematical_grid_height="
                f"{geometry['mathematical_grid_height']}"
            ),
            "",
            "ROW BAND MODEL",
            "-" * 72,
            f"visible_band_count={len(segments)}",
            (
                "ordinary_blue_segment_count="
                f"{len(ordinary_segments)}"
            ),
            (
                "violet_command_segment_count="
                f"{len(command_segments)}"
            ),
            (
                "green_line_number_segment_count="
                f"{len(line_number_segments)}"
            ),
            f"visible_terminal_rows={geometry['rows']}",
            (
                "blank_or_empty_rows_skipped="
                f"{max(geometry['rows'] - len(segments), 0)}"
            ),
            "",
            "OVERLAY CLIP RECTANGLE",
            "-" * 72,
            f"x={geometry['x']}",
            f"y={geometry['y']}",
            f"width={geometry['width']}",
            f"height={geometry['height']}",
            f"remaining_width={geometry['remaining_width']}",
            f"remaining_height={geometry['remaining_height']}",
            f"right_remainder={geometry['right_remainder']}",
            f"bottom_remainder={geometry['bottom_remainder']}",
        ]

        LOG_PATH.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
