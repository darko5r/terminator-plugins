"""
Terminator complete-scrollback visual selection and transactional Copy plugin.

The plugin draws an overlay without changing VTE's native selection, extracts
and classifies terminal text in Python, and extends Terminator's existing
right-click Copy action. An authenticated C engine may supply guarded numeric
drawing geometry; it never owns text extraction, semantic classification, or
clipboard content.
"""

import ctypes
import hashlib
import math
import os
import re
import stat
import threading
import time
import unicodedata
from collections import deque
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

WRAP_EDGE_PROBE_PATH = Path(
    "/tmp/terminator-fullselect-wrap-edge-probe.txt"
)

RESIZE_EVENT_TRACE_PATH = Path(
    "/tmp/terminator-fullselect-resize-event-trace.txt"
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

RENDERER_BUILD_ID = "semantic-v61-transactional-copy-default"

# PRODUCTION_HOTPATH_CLEANUP_V1
#
# Detailed timing, per-event resize tracing, and full native parity checks are
# valuable during validation but duplicate work on GTK's main thread. Keep the
# complete diagnostic implementation available behind one explicit process
# setting while making ordinary Terminator launches use the production path.
# Functional drawing, cache rebuilding, Copy, damage recovery, bounded
# overscan, clip expansion, and resize continuity do not depend on this flag.
VALIDATION_MODE_ENVIRONMENT = "TFSE_VALIDATION"
VALIDATION_MODE_ENABLED = (
    os.environ.get(
        VALIDATION_MODE_ENVIRONMENT,
        "",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)
RESIZE_EVENT_TRACE_ENABLED = VALIDATION_MODE_ENABLED
NATIVE_SHADOW_VALIDATION_ENABLED = VALIDATION_MODE_ENABLED

# NATIVE_PRODUCTION_FAST_PATH_DEFAULT_V1
#
# V58 proved the sparse-validation direct row-content path in a clean
# production process. V59 makes that authenticated path the normal production
# policy. Python remains authoritative for VTE text extraction and semantic
# classification; C supplies only guarded numeric draw geometry. Structural
# checks still cover every native draw, while the first calibration window and
# sparse sentinels retain exact same-frame Python comparison. Any failure uses
# the same captured row snapshot for immediate Python fallback and latches only
# this fast path off for the affected pane. An explicit process-local kill
# switch restores the prior Python numeric draw path without changing files.
NATIVE_PRODUCTION_FAST_PATH_DEFAULT_ENABLED = True
NATIVE_PRODUCTION_FAST_PATH_DISABLE_ENVIRONMENT = (
    "TFSE_DISABLE_NATIVE_FAST_PATH"
)
NATIVE_PRODUCTION_FAST_PATH_DISABLE_REQUESTED = (
    os.environ.get(
        NATIVE_PRODUCTION_FAST_PATH_DISABLE_ENVIRONMENT,
        "",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)
NATIVE_PRODUCTION_FAST_PATH_ENABLED = (
    NATIVE_PRODUCTION_FAST_PATH_DEFAULT_ENABLED
    and not VALIDATION_MODE_ENABLED
    and not NATIVE_PRODUCTION_FAST_PATH_DISABLE_REQUESTED
)

# NATIVE_FRAME_AUTHORITY_CANARY_V1
#
# Native frame output can become authoritative only behind two independent
# process gates. TFSE_VALIDATION keeps the exact Python comparison active;
# TFSE_NATIVE_FRAME_AUTHORITY opts into using the already-matched native
# numeric geometry and row-band fields. A mismatch, unavailable engine, or
# conversion error falls back to Python for that same frame and latches native
# authority off for the affected pane. Ordinary Terminator launches retain the
# v51 production path and make no per-frame native call.
NATIVE_FRAME_AUTHORITY_ENVIRONMENT = (
    "TFSE_NATIVE_FRAME_AUTHORITY"
)
NATIVE_FRAME_AUTHORITY_REQUESTED = (
    os.environ.get(
        NATIVE_FRAME_AUTHORITY_ENVIRONMENT,
        "",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)
NATIVE_FRAME_AUTHORITY_ENABLED = (
    NATIVE_SHADOW_VALIDATION_ENABLED
    and NATIVE_FRAME_AUTHORITY_REQUESTED
)

# NATIVE_DRAW_FRAME_AUTHORITY_CANARY_V1
#
# Cache-rebuild parity does not cover the actual GTK draw callback, where
# rapid resize and reflow expose the difficult event ordering. In validation
# mode only, run the same authenticated frame-v2 calculation against every
# draw's freshly extracted Python segments. A third independent process gate
# may then select only the exactly matched native numeric output. Any mismatch,
# unavailable result, or conversion error falls back to the same-frame Python
# objects and permanently latches draw authority off for the affected pane.
NATIVE_DRAW_FRAME_SHADOW_ENABLED = (
    NATIVE_SHADOW_VALIDATION_ENABLED
)
NATIVE_DRAW_FRAME_AUTHORITY_ENVIRONMENT = (
    "TFSE_NATIVE_DRAW_FRAME_AUTHORITY"
)
NATIVE_DRAW_FRAME_AUTHORITY_REQUESTED = (
    os.environ.get(
        NATIVE_DRAW_FRAME_AUTHORITY_ENVIRONMENT,
        "",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)
NATIVE_DRAW_FRAME_AUTHORITY_ENABLED = (
    NATIVE_DRAW_FRAME_SHADOW_ENABLED
    and NATIVE_FRAME_AUTHORITY_ENABLED
    and NATIVE_DRAW_FRAME_AUTHORITY_REQUESTED
)

# NATIVE_DIRECT_ROW_CONTENT_FRAME_CANARY_V1
#
# V55 proved that authenticated C numeric output can drive every real GTK
# draw while Python retains text and semantic authority. The next boundary is
# the input side of that call: extract row text and terminal-cell bounds once,
# then pass those raw bounds directly to the existing frame-v2 ABI. During
# this canary Python still projects the same raw snapshot independently and
# every numeric field must match before native output is selected. This keeps
# a same-frame Python fallback while proving that segment reconstruction is no
# longer part of the native draw input.
NATIVE_ROW_CONTENT_FRAME_ENVIRONMENT = (
    "TFSE_NATIVE_ROW_CONTENT_FRAME"
)
NATIVE_ROW_CONTENT_FRAME_REQUESTED = (
    os.environ.get(
        NATIVE_ROW_CONTENT_FRAME_ENVIRONMENT,
        "",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)
NATIVE_ROW_CONTENT_FRAME_ENABLED = (
    NATIVE_DRAW_FRAME_AUTHORITY_ENABLED
    and NATIVE_ROW_CONTENT_FRAME_REQUESTED
)

# NATIVE_DIRECT_ROW_CONTENT_FAST_PATH_CANARY_V1
#
# V56 established exact parity for direct row-content input on every real
# draw. V57 removes the duplicate Python numeric segment construction from
# successful frames while retaining bounded validation: the first calibration
# window and sparse sentinel frames still receive an exact independent Python
# projection. Intervening frames use authenticated C numeric output after
# structural validation and graft only Python-owned row text. Any native,
# structural, or sentinel failure builds the Python projection from the same
# captured row snapshot, uses it immediately, and latches this fast path off
# for the affected pane. The proven v56 path remains available after that
# latch, and ordinary Terminator launches remain unchanged.
NATIVE_ROW_CONTENT_FAST_PATH_ENVIRONMENT = (
    "TFSE_NATIVE_ROW_CONTENT_FAST_PATH"
)
NATIVE_ROW_CONTENT_FAST_PATH_REQUESTED = (
    os.environ.get(
        NATIVE_ROW_CONTENT_FAST_PATH_ENVIRONMENT,
        "",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)
NATIVE_ROW_CONTENT_FAST_PATH_ENABLED = (
    (
        NATIVE_ROW_CONTENT_FRAME_ENABLED
        and NATIVE_ROW_CONTENT_FAST_PATH_REQUESTED
    )
    or NATIVE_PRODUCTION_FAST_PATH_ENABLED
)
NATIVE_ROW_CONTENT_FAST_PATH_CALIBRATION_FRAMES = 32
NATIVE_ROW_CONTENT_FAST_PATH_SENTINEL_INTERVAL = 64

# NATIVE_ENGINE_FRAME_BATCH_SHADOW_V2
#
# The loader performs authenticated v1 and frame-v2 ABI handshakes. In
# TFSE_VALIDATION mode one v2 call recalculates geometry and row segments for
# comparison with the already-computed Python result. Ordinary production
# launches make no per-frame native call. The double-gated canary may use an
# exact native match for numeric rendering fields with same-frame fallback.
NATIVE_SHADOW_LOAD_ENABLED = True
NATIVE_SHADOW_AUTHORITATIVE = (
    NATIVE_FRAME_AUTHORITY_ENABLED
    or NATIVE_PRODUCTION_FAST_PATH_ENABLED
)
NATIVE_ENGINE_ABI_VERSION = 1
NATIVE_ENGINE_FRAME_ABI_VERSION = 2
NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS = 15
NATIVE_ENGINE_EXPECTED_BUILD_ID = "tfse-native-c-v2-frame-batch"
NATIVE_ENGINE_EXPECTED_SHA256 = (
    "a3e4b77cf9c23a9501abdb7673de7f46"
    "7ab63ea326a84ab8883a067b14a76529"
)
NATIVE_ENGINE_LIBRARY_PATH = (
    Path(__file__).resolve().parent
    / "full_select_native"
    / "libterminator_full_select_engine.so"
)

NATIVE_ENGINE_EXPECTED_ABI_SIZES = {
    "abi_info": 64,
    "geometry_input": 128,
    "geometry_output": 240,
    "viewport_input": 104,
    "row_content": 24,
    "segment": 64,
    "frame_abi_info": 64,
    "frame_input": 208,
    "frame_output": 304,
}

# NATIVE_GEOMETRY_SHADOW_COMPARE_V1
#
# These names define the complete integer contract shared by the proven
# Python geometry function and tfse_calculate_geometry_v1(). Native output
# is observed only; the Python dictionary remains the functional result.
NATIVE_GEOMETRY_INPUT_FIELDS = (
    "allocated_width",
    "allocated_height",
    "columns",
    "rows",
    "character_width",
    "character_height",
    "scale_factor",
    "padding_left",
    "padding_right",
    "padding_top",
    "padding_bottom",
    "border_left",
    "border_right",
    "border_top",
    "border_bottom",
)

NATIVE_GEOMETRY_OUTPUT_FIELDS = (
    "x",
    "y",
    "width",
    "height",
    "allocated_width",
    "allocated_height",
    "columns",
    "rows",
    "character_width",
    "character_height",
    "mathematical_grid_width",
    "mathematical_grid_height",
    "remaining_width",
    "remaining_height",
    "right_remainder",
    "bottom_remainder",
    "scale_factor",
    "padding_left",
    "padding_right",
    "padding_top",
    "padding_bottom",
    "border_left",
    "border_right",
    "border_top",
    "border_bottom",
    "style_left",
    "style_right",
    "style_top",
    "style_bottom",
)

# NATIVE_SEGMENT_SHADOW_COMPARE_V1
#
# The C engine receives row-content bounds reconstructed from Python's
# authoritative emitted segments. It recalculates only their integer pixel
# geometry and clipping, then its output is compared field-for-field with the
# Python result. These legacy separate-call helpers remain non-authoritative;
# the v53 authority canary uses only the authenticated batched frame result.
NATIVE_SEGMENT_FIELDS = (
    "absolute_row",
    "display_row",
    "start_column",
    "end_column",
    "x",
    "y",
    "width",
    "height",
)

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

# BOTTOM_EDGE_TOGGLE_REPAINT_V1
#
# GTK can retain the final composited VTE row when an 80x24 cell grid leaves
# only a one-pixel bottom remainder. Queue one full backing-window repaint
# immediately and one after two nominal 60 Hz frames. The second request runs
# after the shortcut key event and GTK's first damage pass have completed.
# The v34 deactivation-only path proved that this removes the stale bottom
# band. Apply the same bounded pair while turning the overlay on so its final
# row cannot wait for an unrelated cursor, keyboard, or mouse damage event.
TOGGLE_REPAINT_FOLLOW_UP_DELAY_MS = 32

# RESIZE_DAMAGE_CLIP_PROBE_V1
#
# Record whether GTK's current Cairo damage clip contains three points across
# the lowest visible overlay band while an interactive resize is pending. Also
# count semantic-cache-miss frames during draw. The classifier never changes a
# clip, rebuilds a cache, or alters any segment used by rendering. In v39 its
# returned point count triggers the separately coalesced recovery repaint.

# BOTTOM_DAMAGE_TRIGGERED_RECOVERY_V1
#
# The v38 runtime capture proved that every lowest-row segment existed and
# matched native geometry, but a draw occurring after the final allocation-
# driven repaint could still exclude that segment from its Cairo damage clip.
# When the diagnostic probe observes such an incomplete clip, schedule one
# coalesced full backing-window repaint after the current draw. A nominal-frame
# delay bounds recovery traffic and prevents a recursive redraw loop.
BOTTOM_DAMAGE_RECOVERY_DELAY_MS = 16

# VISIBLE_BOTTOM_BAND_DAMAGE_GRID_V1
#
# The original probe sampled three horizontal points at only the vertical
# midpoint. A clip covering the upper half of the lowest band could therefore
# look complete even while its lower pixels remained undamaged. Probe a 3x3
# grid across the portion of the band that is actually inside the VTE cell
# grid. Clamping to the visible intersection prevents a fractionally exposed
# final row from triggering an endless recovery loop.
BOTTOM_DAMAGE_PROBE_POINT_COUNT = 9

# LIVE_RESIZE_BOTTOM_DAMAGE_QUEUE_V1
#
# A fast interactive allocation can advance again before the delayed damage
# recovery executes. Include the current bottom cell-height rectangle in
# GTK's next damage region directly from size-allocate. This is a targeted
# redraw request only; the v41 3x3 classifier remains the fallback.

# FRAME_CLOCK_RESIZE_FULL_OVERLAY_REPAINT_V1
#
# v42 proved that every size allocation queued the intended bottom-cell
# rectangle, but a request made from inside size-allocate can still be merged
# against the previous backing-window state. Keep one bounded GTK tick
# callback active while allocations arrive. It queues the complete bounded
# overlay rectangle before each rendered frame, preventing VTE's partial
# live-resize damage clips from briefly exposing or recoloring already-covered
# text. The callback then stays alive for a quiet trailing interval so the
# final allocation cannot wait for mouse, keyboard, or cursor damage. The
# trailing tick performs one semantic-coordinate retry and one full backing-
# window invalidation. No synchronous update processing is used.
RESIZE_FRAME_REPAINT_QUIET_MS = 160
RESIZE_FRAME_REPAINT_MIN_TICKS = 2
RESIZE_FRAME_REPAINT_RETRY_DELAY_MS = 80
RESIZE_FRAME_REPAINT_MAX_SETTLED_ATTEMPTS = 3

# BOUNDED_VISIBLE_ROW_OVERSCAN_V1
#
# During an interactive resize, GTK can commit a taller VTE allocation one
# frame before VTE updates get_row_count().  The newly exposed bottom row is
# therefore real and drawable even though it is temporarily outside the
# mathematical rows * character_height grid.  Extract at most two look-ahead
# rows, and clip them to the widget's actual CSS content height.  This closes
# the one-frame segment gap without drawing into a parent widget or changing
# the authoritative VTE geometry/native geometry comparison.
VISIBLE_ROW_OVERSCAN_COUNT = 2

# RESIZE_EVENT_ORDER_PROBE_V1
#
# Record a bounded, in-memory timeline of allocation, frame-clock, draw, and
# recovery events. The trace is diagnostic only: it never schedules drawing,
# changes clipping, rebuilds semantic state, or alters geometry. A context-menu
# action writes the retained tail after a visible resize flash is reproduced.
RESIZE_EVENT_TRACE_LIMIT = 4096

# BOUNDED_RESIZE_DRAW_CLIP_EXPANSION_V1
#
# The v46 event-order trace proved that GTK can invoke the VTE draw handler
# with a dirty clip ending almost exactly one terminal row above the newly
# allocated overlay bottom.  The frame-clock damage request already precedes
# that draw, but GTK commits the clipped frame and honors the full request on
# the following frame 8-27 ms later.  During an active resize only, reset that
# incomplete Cairo clip inside a saved graphics state and immediately bound it
# again to the plugin's allocated-content overlay rectangle.  The surrounding
# save/restore prevents the controlled expansion from escaping this handler.
RESIZE_DRAW_CLIP_EXPANSION_EPSILON = 0.001

# BOUNDED_RESIZE_REFLOW_CONTINUITY_V1
#
# The v47 runtime trace proved that VTE can paint text at a newly committed
# resize geometry one frame before its row extraction reflects the same
# reflow.  In 13 exact-geometry transitions the first draw reused the prior
# row summary and corrected 12-29 ms later; cache misses recovered on another
# 18 exact geometries.  Retain only the previous tail partition while resize
# is active.  When a changed grid reports the identical row summary, project
# that tail by one plausible reflow row and fill only column ranges not already
# covered by the current authoritative partition.  Current semantic segments
# always win, the bridge never schedules a repaint, and all projected output
# remains clipped to the allocated-content overlay.
RESIZE_REFLOW_CONTINUITY_MAX_AGE_MS = 160
RESIZE_REFLOW_CONTINUITY_SHIFT_ROWS = 1
RESIZE_REFLOW_CONTINUITY_TAIL_ROWS = 10

# CURRENT_PARTITION_ONLY_RESIZE_V1
#
# Production evidence from v50 recorded 525 continuity applications and 2,255
# previous-frame ordinary segments without eliminating the underlying VTE
# reflow flash. Those projected segments are the visible duplicate/ghost
# overlay. Keep the bounded implementation available for audit, but do not
# snapshot, calculate, or draw it. Every production frame now renders only the
# current authoritative semantic partition; overscan, damage recovery, clip
# expansion, and frame-clock repainting remain unchanged.
RESIZE_REFLOW_CONTINUITY_ENABLED = False

# PROFILE_BASELINE_V1
#
# Instrumentation only. Production mode avoids thousands of monotonic-clock
# reads and counter updates; launch Terminator with TFSE_VALIDATION=1 when an
# exact before/after performance capture is required.
PROFILE_TIMING_ENABLED = VALIDATION_MODE_ENABLED
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

# TRANSACTIONAL_COPY_DEFAULT_PRODUCTION_V1
#
# V59's Copy path is exact, but it synchronously repeats visible-coordinate
# validation, complete-buffer extraction, clipboard publication, and
# clipboard-manager persistence for every click. V60 kept the same exact
# extraction and normalization contract while validating this production
# pipeline behind a separate canary gate:
#
# - after the visual selection settles, prepare one immutable full-scrollback
#   snapshot at low priority;
# - reuse it only while a generation/signature certificate still matches;
# - otherwise build the payload synchronously with the exact V59 resolver;
# - publish the completed string atomically before scheduling persistence;
# - show one short confirmation badge in the existing Cairo pass.
#
# Every content, cursor, scroll, or allocation event invalidates the prepared
# snapshot. A changed transaction is discarded and retried; the previous
# clipboard is never replaced by partial output. V60 runtime evidence passed
# without misses, retries, persistence failures, or visual regressions, so V61
# enables the accepted path for ordinary production launches. Validation mode
# remains mutually exclusive. One explicit kill switch restores the frozen
# synchronous V59 Copy implementation without changing any other subsystem.
TRANSACTIONAL_COPY_DEFAULT_ENABLED = True
TRANSACTIONAL_COPY_DISABLE_ENVIRONMENT = (
    "TFSE_DISABLE_TRANSACTIONAL_COPY"
)
TRANSACTIONAL_COPY_DISABLE_REQUESTED = (
    os.environ.get(
        TRANSACTIONAL_COPY_DISABLE_ENVIRONMENT,
        "",
    ).strip().lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)
TRANSACTIONAL_COPY_ENABLED = (
    TRANSACTIONAL_COPY_DEFAULT_ENABLED
    and not TRANSACTIONAL_COPY_DISABLE_REQUESTED
    and not VALIDATION_MODE_ENABLED
)
COPY_SNAPSHOT_QUIET_MS = 120
COPY_SNAPSHOT_MAX_CHARACTERS = 16 * 1024 * 1024
COPY_TRANSACTION_MAX_ATTEMPTS = 2
COPY_CONFIRMATION_DURATION_MS = 1100


class _NativeAbiInfoV1(ctypes.Structure):
    """Exact ctypes mirror of tfse_abi_info_v1."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("feature_flags", ctypes.c_uint64),
        ("geometry_input_size", ctypes.c_uint32),
        ("geometry_output_size", ctypes.c_uint32),
        ("viewport_input_size", ctypes.c_uint32),
        ("row_content_size", ctypes.c_uint32),
        ("segment_size", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


class _NativeGeometryInputV1(ctypes.Structure):
    """Exact ctypes mirror of tfse_geometry_input_v1."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        *[
            (name, ctypes.c_int64)
            for name in NATIVE_GEOMETRY_INPUT_FIELDS
        ],
    ]


class _NativeGeometryOutputV1(ctypes.Structure):
    """Exact ctypes mirror of tfse_geometry_output_v1."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        *[
            (name, ctypes.c_int64)
            for name in NATIVE_GEOMETRY_OUTPUT_FIELDS
        ],
    ]


class _NativeViewportInputV1(ctypes.Structure):
    """Exact ctypes mirror of tfse_viewport_input_v1."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("grid_x", ctypes.c_int64),
        ("grid_y", ctypes.c_int64),
        ("grid_width", ctypes.c_int64),
        ("grid_height", ctypes.c_int64),
        ("rows", ctypes.c_int64),
        ("columns", ctypes.c_int64),
        ("character_width", ctypes.c_int64),
        ("character_height", ctypes.c_int64),
        ("row_coordinate_offset", ctypes.c_int64),
        ("row_gap_px", ctypes.c_int64),
        ("horizontal_inset_px", ctypes.c_int64),
        ("scroll_value", ctypes.c_double),
    ]


class _NativeRowContentV1(ctypes.Structure):
    """Exact ctypes mirror of tfse_row_content_v1."""

    _fields_ = [
        ("has_content", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("start_column", ctypes.c_int64),
        ("end_column", ctypes.c_int64),
    ]


class _NativeSegmentV1(ctypes.Structure):
    """Exact ctypes mirror of tfse_segment_v1."""

    _fields_ = [
        (name, ctypes.c_int64)
        for name in NATIVE_SEGMENT_FIELDS
    ]


class _NativeFrameAbiInfoV2(ctypes.Structure):
    """Exact ctypes mirror of tfse_frame_abi_info_v2."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("feature_flags", ctypes.c_uint64),
        ("frame_input_size", ctypes.c_uint32),
        ("frame_output_size", ctypes.c_uint32),
        ("geometry_input_size", ctypes.c_uint32),
        ("geometry_output_size", ctypes.c_uint32),
        ("viewport_input_size", ctypes.c_uint32),
        ("row_content_size", ctypes.c_uint32),
        ("segment_size", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 5),
    ]


class _NativeFrameInputV2(ctypes.Structure):
    """Exact ctypes mirror of tfse_frame_input_v2."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("geometry", _NativeGeometryInputV1),
        ("row_coordinate_offset", ctypes.c_int64),
        ("row_gap_px", ctypes.c_int64),
        ("horizontal_inset_px", ctypes.c_int64),
        ("overscan_rows", ctypes.c_int64),
        ("scroll_value", ctypes.c_double),
        ("reserved", ctypes.c_uint64 * 4),
    ]


class _NativeFrameOutputV2(ctypes.Structure):
    """Exact ctypes mirror of tfse_frame_output_v2."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("geometry", _NativeGeometryOutputV1),
        ("segment_count", ctypes.c_uint64),
        ("required_segment_capacity", ctypes.c_uint64),
        ("reserved", ctypes.c_uint64 * 5),
    ]


class _NativeShadowEngineV2:
    """Fail closed to Python after an authenticated ABI handshake."""

    _required_symbols = (
        "tfse_engine_abi_version",
        "tfse_engine_feature_flags",
        "tfse_engine_build_id",
        "tfse_status_name",
        "tfse_query_abi_v1",
        "tfse_calculate_geometry_v1",
        "tfse_calculate_segments_v1",
        "tfse_query_frame_abi_v2",
        "tfse_calculate_frame_v2",
    )

    def __init__(self):
        self.library = None
        self.enabled = bool(
            NATIVE_SHADOW_LOAD_ENABLED
        )
        self.authoritative = bool(
            NATIVE_SHADOW_AUTHORITATIVE
        )
        self.load_attempt_count = 0
        self.load_success_count = 0
        self.load_failure_count = 0
        self.loaded = False
        self.validated = False
        self.available = False
        self.last_error = ""
        self.load_elapsed_ns = 0
        self.binary_sha256 = ""
        self.binary_size = 0
        self.binary_device = 0
        self.binary_inode = 0
        self.abi_version = 0
        self.feature_flags = 0
        self.build_id = ""
        self.query_status = -1
        self.frame_query_status = -1
        self.abi_sizes = {
            name: 0
            for name in NATIVE_ENGINE_EXPECTED_ABI_SIZES
        }
        self.geometry_call_count = 0
        self.segment_call_count = 0
        self.frame_call_count = 0

        if self.enabled:
            self._load_and_validate()

    @staticmethod
    def _hash_open_file(file_descriptor):
        digest = hashlib.sha256()

        os.lseek(
            file_descriptor,
            0,
            os.SEEK_SET,
        )

        while True:
            block = os.read(
                file_descriptor,
                1024 * 1024,
            )

            if not block:
                break

            digest.update(block)

        return digest.hexdigest()

    @staticmethod
    def _validate_file_metadata(metadata):
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(
                "Native engine is not a regular file."
            )

        allowed_owners = {
            0,
            os.geteuid(),
        }

        if metadata.st_uid not in allowed_owners:
            raise RuntimeError(
                "Native engine has an unexpected owner."
            )

        unsafe_write_bits = (
            stat.S_IWGRP
            | stat.S_IWOTH
        )

        if metadata.st_mode & unsafe_write_bits:
            raise RuntimeError(
                "Native engine is group- or world-writable."
            )

    @staticmethod
    def _configure_abi_functions(library):
        for symbol_name in (
            _NativeShadowEngineV2._required_symbols
        ):
            getattr(library, symbol_name)

        library.tfse_engine_abi_version.argtypes = []
        library.tfse_engine_abi_version.restype = (
            ctypes.c_uint32
        )
        library.tfse_engine_feature_flags.argtypes = []
        library.tfse_engine_feature_flags.restype = (
            ctypes.c_uint64
        )
        library.tfse_engine_build_id.argtypes = []
        library.tfse_engine_build_id.restype = (
            ctypes.c_char_p
        )
        library.tfse_query_abi_v1.argtypes = [
            ctypes.POINTER(_NativeAbiInfoV1)
        ]
        library.tfse_query_abi_v1.restype = (
            ctypes.c_int32
        )
        library.tfse_status_name.argtypes = [
            ctypes.c_int32
        ]
        library.tfse_status_name.restype = (
            ctypes.c_char_p
        )
        library.tfse_calculate_geometry_v1.argtypes = [
            ctypes.POINTER(_NativeGeometryInputV1),
            ctypes.POINTER(_NativeGeometryOutputV1),
        ]
        library.tfse_calculate_geometry_v1.restype = (
            ctypes.c_int32
        )
        library.tfse_calculate_segments_v1.argtypes = [
            ctypes.POINTER(_NativeViewportInputV1),
            ctypes.POINTER(_NativeRowContentV1),
            ctypes.c_uint64,
            ctypes.POINTER(_NativeSegmentV1),
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        library.tfse_calculate_segments_v1.restype = (
            ctypes.c_int32
        )
        library.tfse_query_frame_abi_v2.argtypes = [
            ctypes.POINTER(_NativeFrameAbiInfoV2)
        ]
        library.tfse_query_frame_abi_v2.restype = (
            ctypes.c_int32
        )
        library.tfse_calculate_frame_v2.argtypes = [
            ctypes.POINTER(_NativeFrameInputV2),
            ctypes.POINTER(_NativeRowContentV1),
            ctypes.c_uint64,
            ctypes.POINTER(_NativeSegmentV1),
            ctypes.c_uint64,
            ctypes.POINTER(_NativeFrameOutputV2),
        ]
        library.tfse_calculate_frame_v2.restype = (
            ctypes.c_int32
        )

    def _load_and_validate(self):
        started_ns = time.perf_counter_ns()
        file_descriptor = None

        self.load_attempt_count += 1

        try:
            open_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )

            file_descriptor = os.open(
                NATIVE_ENGINE_LIBRARY_PATH,
                open_flags,
            )
            metadata = os.fstat(file_descriptor)

            self._validate_file_metadata(metadata)

            self.binary_size = int(metadata.st_size)
            self.binary_device = int(metadata.st_dev)
            self.binary_inode = int(metadata.st_ino)
            self.binary_sha256 = self._hash_open_file(
                file_descriptor
            )

            if (
                self.binary_sha256
                != NATIVE_ENGINE_EXPECTED_SHA256
            ):
                raise RuntimeError(
                    "Native engine SHA-256 mismatch: "
                    f"expected {NATIVE_ENGINE_EXPECTED_SHA256}, "
                    f"found {self.binary_sha256}."
                )

            if (
                ctypes.sizeof(_NativeAbiInfoV1)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "abi_info"
                ]
            ):
                raise RuntimeError(
                    "Python ABI-info layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeGeometryInputV1)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "geometry_input"
                ]
            ):
                raise RuntimeError(
                    "Python geometry-input layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeGeometryOutputV1)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "geometry_output"
                ]
            ):
                raise RuntimeError(
                    "Python geometry-output layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeViewportInputV1)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "viewport_input"
                ]
            ):
                raise RuntimeError(
                    "Python viewport-input layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeRowContentV1)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "row_content"
                ]
            ):
                raise RuntimeError(
                    "Python row-content layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeSegmentV1)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "segment"
                ]
            ):
                raise RuntimeError(
                    "Python segment layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeFrameAbiInfoV2)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "frame_abi_info"
                ]
            ):
                raise RuntimeError(
                    "Python frame-ABI-info layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeFrameInputV2)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "frame_input"
                ]
            ):
                raise RuntimeError(
                    "Python frame-input layout mismatch."
                )

            if (
                ctypes.sizeof(_NativeFrameOutputV2)
                != NATIVE_ENGINE_EXPECTED_ABI_SIZES[
                    "frame_output"
                ]
            ):
                raise RuntimeError(
                    "Python frame-output layout mismatch."
                )

            loader_path = (
                f"/proc/self/fd/{file_descriptor}"
            )
            loader_mode = (
                getattr(os, "RTLD_LOCAL", 0)
                | getattr(os, "RTLD_NOW", 2)
            )
            library = ctypes.CDLL(
                loader_path,
                mode=loader_mode,
            )
            self.loaded = True

            self._configure_abi_functions(library)

            direct_abi_version = int(
                library.tfse_engine_abi_version()
            )
            direct_feature_flags = int(
                library.tfse_engine_feature_flags()
            )
            raw_build_id = (
                library.tfse_engine_build_id()
            )

            if raw_build_id is None:
                raise RuntimeError(
                    "Native engine returned a null build ID."
                )

            build_id = raw_build_id.decode(
                "ascii",
                errors="strict",
            )

            abi_info = _NativeAbiInfoV1()
            abi_info.struct_size = ctypes.sizeof(
                _NativeAbiInfoV1
            )
            abi_info.abi_version = (
                NATIVE_ENGINE_ABI_VERSION
            )

            query_status = int(
                library.tfse_query_abi_v1(
                    ctypes.byref(abi_info)
                )
            )

            frame_abi_info = _NativeFrameAbiInfoV2()
            frame_abi_info.struct_size = ctypes.sizeof(
                _NativeFrameAbiInfoV2
            )
            frame_abi_info.abi_version = (
                NATIVE_ENGINE_FRAME_ABI_VERSION
            )

            frame_query_status = int(
                library.tfse_query_frame_abi_v2(
                    ctypes.byref(frame_abi_info)
                )
            )

            abi_sizes = {
                "abi_info": ctypes.sizeof(
                    _NativeAbiInfoV1
                ),
                "geometry_input": int(
                    abi_info.geometry_input_size
                ),
                "geometry_output": int(
                    abi_info.geometry_output_size
                ),
                "viewport_input": int(
                    abi_info.viewport_input_size
                ),
                "row_content": int(
                    abi_info.row_content_size
                ),
                "segment": int(
                    abi_info.segment_size
                ),
                "frame_abi_info": ctypes.sizeof(
                    _NativeFrameAbiInfoV2
                ),
                "frame_input": int(
                    frame_abi_info.frame_input_size
                ),
                "frame_output": int(
                    frame_abi_info.frame_output_size
                ),
            }

            if query_status != 0:
                raise RuntimeError(
                    "Native ABI query failed with status "
                    f"{query_status}."
                )

            if frame_query_status != 0:
                raise RuntimeError(
                    "Native frame ABI query failed with status "
                    f"{frame_query_status}."
                )

            if (
                direct_abi_version
                != NATIVE_ENGINE_ABI_VERSION
                or int(abi_info.abi_version)
                != NATIVE_ENGINE_ABI_VERSION
            ):
                raise RuntimeError(
                    "Native engine ABI version mismatch."
                )

            if (
                int(frame_abi_info.abi_version)
                != NATIVE_ENGINE_FRAME_ABI_VERSION
            ):
                raise RuntimeError(
                    "Native frame ABI version mismatch."
                )

            if (
                direct_feature_flags
                != NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS
                or int(abi_info.feature_flags)
                != NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS
                or int(frame_abi_info.feature_flags)
                != NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS
            ):
                raise RuntimeError(
                    "Native engine feature-flag mismatch."
                )

            if build_id != NATIVE_ENGINE_EXPECTED_BUILD_ID:
                raise RuntimeError(
                    "Native engine build-ID mismatch."
                )

            if abi_sizes != NATIVE_ENGINE_EXPECTED_ABI_SIZES:
                raise RuntimeError(
                    "Native engine structure-size mismatch: "
                    f"{abi_sizes!r}."
                )

            if any(abi_info.reserved):
                raise RuntimeError(
                    "Native ABI reserved fields are nonzero."
                )

            if any(frame_abi_info.reserved):
                raise RuntimeError(
                    "Native frame ABI reserved fields are nonzero."
                )

            frame_v1_sizes = {
                "geometry_input": int(
                    frame_abi_info.geometry_input_size
                ),
                "geometry_output": int(
                    frame_abi_info.geometry_output_size
                ),
                "viewport_input": int(
                    frame_abi_info.viewport_input_size
                ),
                "row_content": int(
                    frame_abi_info.row_content_size
                ),
                "segment": int(
                    frame_abi_info.segment_size
                ),
            }
            expected_v1_sizes = {
                name: NATIVE_ENGINE_EXPECTED_ABI_SIZES[name]
                for name in frame_v1_sizes
            }

            if frame_v1_sizes != expected_v1_sizes:
                raise RuntimeError(
                    "Native frame ABI v1-layout mismatch: "
                    f"{frame_v1_sizes!r}."
                )

            self.library = library
            self.abi_version = direct_abi_version
            self.feature_flags = direct_feature_flags
            self.build_id = build_id
            self.query_status = query_status
            self.frame_query_status = frame_query_status
            self.abi_sizes = abi_sizes
            self.validated = True
            self.available = True
            self.load_success_count += 1

        except Exception as exc:
            self.library = None
            self.available = False
            self.validated = False
            self.load_failure_count += 1
            self.last_error = (
                f"{type(exc).__name__}: {exc}"
            )

        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except Exception:
                    pass

            self.load_elapsed_ns = (
                time.perf_counter_ns()
                - started_ns
            )

    def calculate_geometry_shadow(self, python_geometry):
        """Return native geometry without changing Python authority."""

        if not self.available or self.library is None:
            raise RuntimeError(
                "Native engine is unavailable."
            )

        native_input = _NativeGeometryInputV1()
        native_input.struct_size = ctypes.sizeof(
            _NativeGeometryInputV1
        )
        native_input.abi_version = (
            NATIVE_ENGINE_ABI_VERSION
        )

        for field_name in NATIVE_GEOMETRY_INPUT_FIELDS:
            setattr(
                native_input,
                field_name,
                int(python_geometry[field_name]),
            )

        native_output = _NativeGeometryOutputV1()
        native_output.struct_size = ctypes.sizeof(
            _NativeGeometryOutputV1
        )
        native_output.abi_version = (
            NATIVE_ENGINE_ABI_VERSION
        )

        self.geometry_call_count += 1

        status = int(
            self.library.tfse_calculate_geometry_v1(
                ctypes.byref(native_input),
                ctypes.byref(native_output),
            )
        )

        if status != 0:
            raw_status_name = (
                self.library.tfse_status_name(status)
            )
            status_name = (
                raw_status_name.decode(
                    "ascii",
                    errors="replace",
                )
                if raw_status_name
                else "unknown"
            )
            raise RuntimeError(
                "Native geometry failed with status "
                f"{status} ({status_name})."
            )

        return {
            field_name: int(
                getattr(native_output, field_name)
            )
            for field_name in NATIVE_GEOMETRY_OUTPUT_FIELDS
        }

    def calculate_segments_shadow(
        self,
        python_geometry,
        scroll_value,
        row_coordinate_offset,
        python_segments,
        overlay_height=None,
        overscan_rows=0,
    ):
        """Return native emitted segments without changing authority."""

        if not self.available or self.library is None:
            raise RuntimeError(
                "Native engine is unavailable."
            )

        rows = max(
            int(python_geometry["rows"]),
            0,
        )

        native_rows = rows + max(
            int(overscan_rows),
            0,
        )

        # The native routine can inspect one extra physical row while
        # scrolling by a fractional cell. Supplying native_rows + 1 is valid
        # for both the integer and fractional cases and bounds every
        # allocation, including Python's comparison-only overscan viewport.
        row_content_count = native_rows + 1

        native_input = _NativeViewportInputV1()
        native_input.struct_size = ctypes.sizeof(
            _NativeViewportInputV1
        )
        native_input.abi_version = (
            NATIVE_ENGINE_ABI_VERSION
        )
        native_input.grid_x = int(
            python_geometry["x"]
        )
        native_input.grid_y = int(
            python_geometry["y"]
        )
        native_input.grid_width = int(
            python_geometry["width"]
        )
        native_input.grid_height = int(
            python_geometry["height"]
            if overlay_height is None
            else overlay_height
        )
        native_input.rows = native_rows
        native_input.columns = int(
            python_geometry["columns"]
        )
        native_input.character_width = int(
            python_geometry["character_width"]
        )
        native_input.character_height = int(
            python_geometry["character_height"]
        )
        native_input.row_coordinate_offset = int(
            row_coordinate_offset
        )
        native_input.row_gap_px = int(
            ROW_GAP_PX
        )
        native_input.horizontal_inset_px = int(
            ROW_HORIZONTAL_INSET_PX
        )
        native_input.scroll_value = float(
            scroll_value
        )

        row_content_type = (
            _NativeRowContentV1 * row_content_count
        )
        native_row_content = row_content_type()

        for python_segment in python_segments:
            display_row = int(
                python_segment["display_row"]
            )

            if not 0 <= display_row < row_content_count:
                raise RuntimeError(
                    "Python segment display row is outside "
                    f"the native content array: {display_row}."
                )

            content = native_row_content[display_row]

            if content.has_content:
                raise RuntimeError(
                    "Python emitted more than one base segment "
                    f"for display row {display_row}."
                )

            content.has_content = 1
            content.reserved = 0
            content.start_column = int(
                python_segment["start_column"]
            )
            content.end_column = int(
                python_segment["end_column"]
            )

        native_segment_type = (
            _NativeSegmentV1 * row_content_count
        )
        native_segments = native_segment_type()
        native_segment_count = ctypes.c_uint64()

        self.segment_call_count += 1

        status = int(
            self.library.tfse_calculate_segments_v1(
                ctypes.byref(native_input),
                native_row_content,
                row_content_count,
                native_segments,
                row_content_count,
                ctypes.byref(native_segment_count),
            )
        )

        if status != 0:
            raw_status_name = (
                self.library.tfse_status_name(status)
            )
            status_name = (
                raw_status_name.decode(
                    "ascii",
                    errors="replace",
                )
                if raw_status_name
                else "unknown"
            )
            raise RuntimeError(
                "Native segment calculation failed with status "
                f"{status} ({status_name})."
            )

        output_count = int(
            native_segment_count.value
        )

        if output_count > row_content_count:
            raise RuntimeError(
                "Native segment count exceeded the supplied "
                f"capacity: {output_count}>{row_content_count}."
            )

        return [
            {
                field_name: int(
                    getattr(
                        native_segments[index],
                        field_name,
                    )
                )
                for field_name in NATIVE_SEGMENT_FIELDS
            }
            for index in range(output_count)
        ]

    def calculate_frame_shadow(
        self,
        python_geometry,
        scroll_value,
        row_coordinate_offset,
        python_segments,
        overscan_rows=0,
    ):
        """Preserve the v55 segment-reconstruction entry point."""

        return self.calculate_frame_from_row_contents(
            python_geometry,
            scroll_value,
            row_coordinate_offset,
            python_segments,
            overscan_rows=overscan_rows,
        )

    def calculate_frame_from_row_contents(
        self,
        python_geometry,
        scroll_value,
        row_coordinate_offset,
        row_contents,
        overscan_rows=0,
    ):
        """Batch geometry and raw row-content projection in one v2 call.

        Each row-content dictionary supplies only the display-row index and
        half-open terminal-cell bounds. Extra Python-only fields, including
        row text and absolute row, are deliberately ignored by the ABI.
        """

        if not self.available or self.library is None:
            raise RuntimeError(
                "Native engine is unavailable."
            )

        rows = max(
            int(python_geometry["rows"]),
            0,
        )
        bounded_overscan_rows = max(
            int(overscan_rows),
            0,
        )
        row_content_count = (
            rows + bounded_overscan_rows + 1
        )

        native_geometry_input = _NativeGeometryInputV1()
        native_geometry_input.struct_size = ctypes.sizeof(
            _NativeGeometryInputV1
        )
        native_geometry_input.abi_version = (
            NATIVE_ENGINE_ABI_VERSION
        )

        for field_name in NATIVE_GEOMETRY_INPUT_FIELDS:
            setattr(
                native_geometry_input,
                field_name,
                int(python_geometry[field_name]),
            )

        native_input = _NativeFrameInputV2()
        native_input.struct_size = ctypes.sizeof(
            _NativeFrameInputV2
        )
        native_input.abi_version = (
            NATIVE_ENGINE_FRAME_ABI_VERSION
        )
        native_input.geometry = native_geometry_input
        native_input.row_coordinate_offset = int(
            row_coordinate_offset
        )
        native_input.row_gap_px = int(
            ROW_GAP_PX
        )
        native_input.horizontal_inset_px = int(
            ROW_HORIZONTAL_INSET_PX
        )
        native_input.overscan_rows = bounded_overscan_rows
        native_input.scroll_value = float(
            scroll_value
        )

        row_content_type = (
            _NativeRowContentV1 * row_content_count
        )
        native_row_content = row_content_type()

        for row_content in row_contents:
            display_row = int(
                row_content["display_row"]
            )

            if not 0 <= display_row < row_content_count:
                raise RuntimeError(
                    "Python row-content display row is outside "
                    f"the native frame array: {display_row}."
                )

            content = native_row_content[display_row]

            if content.has_content:
                raise RuntimeError(
                    "Python emitted more than one content range "
                    f"for display row {display_row}."
                )

            content.has_content = 1
            content.reserved = 0
            content.start_column = int(
                row_content["start_column"]
            )
            content.end_column = int(
                row_content["end_column"]
            )

        native_segment_type = (
            _NativeSegmentV1 * row_content_count
        )
        native_segments = native_segment_type()
        native_output = _NativeFrameOutputV2()
        native_output.struct_size = ctypes.sizeof(
            _NativeFrameOutputV2
        )
        native_output.abi_version = (
            NATIVE_ENGINE_FRAME_ABI_VERSION
        )

        self.frame_call_count += 1

        status = int(
            self.library.tfse_calculate_frame_v2(
                ctypes.byref(native_input),
                native_row_content,
                row_content_count,
                native_segments,
                row_content_count,
                ctypes.byref(native_output),
            )
        )

        if status != 0:
            raw_status_name = (
                self.library.tfse_status_name(status)
            )
            status_name = (
                raw_status_name.decode(
                    "ascii",
                    errors="replace",
                )
                if raw_status_name
                else "unknown"
            )
            raise RuntimeError(
                "Native frame calculation failed with status "
                f"{status} ({status_name})."
            )

        if (
            int(native_output.struct_size)
            != ctypes.sizeof(_NativeFrameOutputV2)
            or int(native_output.abi_version)
            != NATIVE_ENGINE_FRAME_ABI_VERSION
        ):
            raise RuntimeError(
                "Native frame output header changed unexpectedly."
            )

        if any(native_output.reserved):
            raise RuntimeError(
                "Native frame output reserved fields are nonzero."
            )

        output_count = int(
            native_output.segment_count
        )
        required_capacity = int(
            native_output.required_segment_capacity
        )

        if output_count > row_content_count:
            raise RuntimeError(
                "Native frame segment count exceeded capacity: "
                f"{output_count}>{row_content_count}."
            )

        if required_capacity != row_content_count:
            raise RuntimeError(
                "Native frame capacity contract changed: "
                f"{required_capacity}!={row_content_count}."
            )

        native_geometry = {
            field_name: int(
                getattr(
                    native_output.geometry,
                    field_name,
                )
            )
            for field_name in NATIVE_GEOMETRY_OUTPUT_FIELDS
        }
        native_segment_output = [
            {
                field_name: int(
                    getattr(
                        native_segments[index],
                        field_name,
                    )
                )
                for field_name in NATIVE_SEGMENT_FIELDS
            }
            for index in range(output_count)
        ]

        return {
            "geometry": native_geometry,
            "segments": native_segment_output,
            "row_content_count": row_content_count,
            "required_segment_capacity": required_capacity,
        }

    def diagnostic_snapshot(self):
        return {
            "enabled": self.enabled,
            "authoritative": self.authoritative,
            "load_attempt_count": self.load_attempt_count,
            "load_success_count": self.load_success_count,
            "load_failure_count": self.load_failure_count,
            "loaded": self.loaded,
            "validated": self.validated,
            "available": self.available,
            "last_error": self.last_error,
            "load_elapsed_ns": self.load_elapsed_ns,
            "binary_path": str(
                NATIVE_ENGINE_LIBRARY_PATH
            ),
            "binary_sha256": self.binary_sha256,
            "binary_size": self.binary_size,
            "binary_device": self.binary_device,
            "binary_inode": self.binary_inode,
            "abi_version": self.abi_version,
            "feature_flags": self.feature_flags,
            "build_id": self.build_id,
            "query_status": self.query_status,
            "frame_query_status": self.frame_query_status,
            "abi_sizes": dict(self.abi_sizes),
            "geometry_call_count": (
                self.geometry_call_count
            ),
            "segment_call_count": (
                self.segment_call_count
            ),
            "frame_call_count": (
                self.frame_call_count
            ),
        }


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

        # Load and validate the optional native engine exactly once for this
        # plugin instance. Production remains Python-only. The double-gated
        # canary accepts only exact native frame matches and retains a
        # same-frame Python fallback plus a per-pane circuit breaker.
        self._native_shadow_engine = (
            _NativeShadowEngineV2()
        )

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

        # WRAP_EDGE_END_COLUMN_PROBE_V1
        #
        # This diagnostic compares VTE's range endpoint behavior around
        # the final physical column. It does not alter extraction used by
        # rendering, rebuild caches, draw, copy, or update plugin state.
        wrap_probe_item = Gtk.MenuItem.new_with_label(
            "Full Select: write wrap-edge report"
        )

        wrap_probe_item.connect(
            "activate",
            self.write_wrap_edge_report,
            terminal,
        )

        menuitems.append(wrap_probe_item)

        if RESIZE_EVENT_TRACE_ENABLED:
            # RESIZE_EVENT_ORDER_PROBE_V1
            #
            # Persist only the already-recorded bounded event tail. The menu
            # action is intentionally absent from production launches because
            # no event timeline is collected there.
            resize_trace_item = Gtk.MenuItem.new_with_label(
                "Full Select: write resize-event trace"
            )

            resize_trace_item.connect(
                "activate",
                self.write_resize_event_trace,
                terminal,
            )

            menuitems.append(resize_trace_item)

    def write_wrap_edge_report(
        self,
        _menu_item,
        terminal,
    ):
        """Record VTE end-column behavior without changing rendering."""

        vte = terminal.get_vte()
        state = self._ensure_state(vte)
        geometry = self._calculate_geometry(vte)

        rows = max(int(geometry["rows"]), 1)
        columns = max(int(geometry["columns"]), 1)

        adjustment = vte.get_vadjustment()
        adjustment_top = math.floor(
            float(adjustment.get_value())
        )
        row_coordinate_offset = int(
            state.get(
                "visual_row_coordinate_offset",
                0,
            )
        )
        absolute_top = (
            adjustment_top
            + row_coordinate_offset
        )

        try:
            cursor_position = vte.get_cursor_position()
            cursor_column = int(
                cursor_position.column
                if hasattr(cursor_position, "column")
                else cursor_position[0]
            )
            cursor_row = int(
                cursor_position.row
                if hasattr(cursor_position, "row")
                else cursor_position[1]
            )
        except Exception:
            cursor_column = -1
            cursor_row = -1

        def normalize_result(result):
            if isinstance(result, (tuple, list)):
                result = result[0]

            if result is None:
                return ""

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

        def extract_range(
            absolute_row,
            start_column,
            end_column,
        ):
            try:
                result = vte.get_text_range_format(
                    format=Vte.Format.TEXT,
                    start_row=int(absolute_row),
                    start_col=int(start_column),
                    end_row=int(absolute_row),
                    end_col=int(end_column),
                )
                return normalize_result(result), ""
            except Exception as exc:
                return "", (
                    f"{type(exc).__name__}: {exc}"
                )

        def without_delimiters(value):
            return value.rstrip("\n")

        def first_mismatch(left, right):
            for index, (left_value, right_value) in enumerate(
                zip(left, right)
            ):
                if left_value != right_value:
                    return index

            if len(left) != len(right):
                return min(len(left), len(right))

            return -1

        cache = state.get("render_cache") or {}
        cached_segments = cache.get("row_segments") or []
        cached_by_absolute_row = {
            int(segment["absolute_row"]): segment
            for segment in cached_segments
        }

        try:
            visible_result = vte.get_text_format(
                format=Vte.Format.TEXT
            )
            visible_text = normalize_result(
                visible_result
            )
            visible_text_error = ""
        except Exception as exc:
            visible_text = ""
            visible_text_error = (
                f"{type(exc).__name__}: {exc}"
            )

        endpoint_values = (
            (
                "columns_minus_2",
                max(columns - 2, 0),
            ),
            (
                "columns_minus_1",
                max(columns - 1, 0),
            ),
            ("columns", columns),
            ("columns_plus_1", columns + 1),
        )

        lines = [
            "TERMINATOR FULL-SELECT WRAP-EDGE END-COLUMN PROBE",
            "=" * 72,
            f"renderer_build_id={RENDERER_BUILD_ID}",
            "probe_functional_changes=False",
            f"active={state.get('active', False)}",
            f"columns={columns}",
            f"rows={rows}",
            f"character_width={geometry['character_width']}",
            f"character_height={geometry['character_height']}",
            f"adjustment_top={adjustment_top}",
            f"visual_row_coordinate_offset={row_coordinate_offset}",
            f"absolute_top={absolute_top}",
            f"cursor_absolute_column={cursor_column}",
            f"cursor_absolute_row={cursor_row}",
            f"cached_row_segment_count={len(cached_segments)}",
            f"visible_text_characters={len(visible_text)}",
            f"visible_text_error={visible_text_error}",
            f"visible_text_suffix={visible_text[-240:]!r}",
            "",
            "VISIBLE PHYSICAL ROW ENDPOINT COMPARISON",
            "-" * 72,
        ]

        differing_row_count = 0
        full_width_current_segment_count = 0

        for display_row in range(rows):
            absolute_row = absolute_top + display_row
            cached_segment = cached_by_absolute_row.get(
                absolute_row
            )
            variants = {}

            for label, end_column in endpoint_values:
                raw_text, error = extract_range(
                    absolute_row,
                    0,
                    end_column,
                )
                content_text = without_delimiters(
                    raw_text
                )
                variants[label] = {
                    "end_column": end_column,
                    "raw": raw_text,
                    "content": content_text,
                    "error": error,
                }

            minus_one_content = variants[
                "columns_minus_1"
            ]["content"]
            columns_content = variants[
                "columns"
            ]["content"]

            differs = (
                minus_one_content
                != columns_content
            )

            if differs:
                differing_row_count += 1

            current_start = (
                int(cached_segment["start_column"])
                if cached_segment is not None
                else -1
            )
            current_end = (
                int(cached_segment["end_column"])
                if cached_segment is not None
                else -1
            )
            current_text = (
                str(cached_segment["row_text"])
                if cached_segment is not None
                else ""
            )

            if current_end == columns:
                full_width_current_segment_count += 1

            lines.extend(
                [
                    f"row_{display_row:02d}_absolute={absolute_row}",
                    f"row_{display_row:02d}_current_segment_present={cached_segment is not None}",
                    f"row_{display_row:02d}_current_start_column={current_start}",
                    f"row_{display_row:02d}_current_end_column={current_end}",
                    f"row_{display_row:02d}_current_text_cells={self._cell_column_after(current_text)}",
                    f"row_{display_row:02d}_current_text={current_text!r}",
                    f"row_{display_row:02d}_minus_1_vs_columns_differ={differs}",
                    f"row_{display_row:02d}_minus_1_vs_columns_first_mismatch={first_mismatch(minus_one_content, columns_content)}",
                ]
            )

            if columns_content.startswith(
                minus_one_content
            ):
                extension = columns_content[
                    len(minus_one_content):
                ]
            else:
                extension = "<non-prefix>"

            lines.append(
                f"row_{display_row:02d}_minus_1_to_columns_extension={extension!r}"
            )

            for label, _end_column in endpoint_values:
                variant = variants[label]
                content_text = variant["content"]

                lines.extend(
                    [
                        f"row_{display_row:02d}_{label}_end_column={variant['end_column']}",
                        f"row_{display_row:02d}_{label}_characters={len(content_text)}",
                        f"row_{display_row:02d}_{label}_cells={self._cell_column_after(content_text)}",
                        f"row_{display_row:02d}_{label}_last_character={content_text[-1:]!r}",
                        f"row_{display_row:02d}_{label}_error={variant['error']}",
                        f"row_{display_row:02d}_{label}_text={content_text!r}",
                    ]
                )

            last_cell_text, last_cell_error = extract_range(
                absolute_row,
                max(columns - 1, 0),
                max(columns - 1, 0),
            )
            boundary_text, boundary_error = extract_range(
                absolute_row,
                max(columns - 1, 0),
                columns,
            )
            beyond_text, beyond_error = extract_range(
                absolute_row,
                columns,
                columns,
            )

            lines.extend(
                [
                    f"row_{display_row:02d}_last_cell_closed_text={without_delimiters(last_cell_text)!r}",
                    f"row_{display_row:02d}_last_cell_closed_error={last_cell_error}",
                    f"row_{display_row:02d}_last_cell_boundary_text={without_delimiters(boundary_text)!r}",
                    f"row_{display_row:02d}_last_cell_boundary_error={boundary_error}",
                    f"row_{display_row:02d}_beyond_cell_closed_text={without_delimiters(beyond_text)!r}",
                    f"row_{display_row:02d}_beyond_cell_closed_error={beyond_error}",
                    "",
                ]
            )

        lines.extend(
            [
                "SUMMARY",
                "-" * 72,
                f"differing_row_count={differing_row_count}",
                f"full_width_current_segment_count={full_width_current_segment_count}",
                "drawing_changed=False",
                "copy_changed=False",
                "geometry_changed=False",
                "native_calls_added=False",
            ]
        )

        WRAP_EDGE_PROBE_PATH.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

    def write_resize_event_trace(
        self,
        _menu_item,
        terminal,
    ):
        """Persist the retained resize timeline without changing behavior."""

        if not RESIZE_EVENT_TRACE_ENABLED:
            return

        vte = terminal.get_vte()
        state = self._ensure_state(vte)
        state["resize_event_trace_write_count"] += 1

        try:
            trace = list(
                state.get("resize_event_trace", ())
            )
            origin_ns = int(
                state.get("resize_event_trace_origin_ns", 0)
            )
            first_sequence = (
                int(trace[0]["sequence"])
                if trace
                else -1
            )
            last_sequence = (
                int(trace[-1]["sequence"])
                if trace
                else -1
            )
            event_names = (
                "allocation",
                "frame_tick",
                "draw",
                "bottom_recovery",
                "settled_full",
            )

            lines = [
                "TERMINATOR FULL SELECT RESIZE EVENT TRACE",
                "=" * 72,
                f"renderer_build_id={RENDERER_BUILD_ID}",
                f"active={state.get('active', False)}",
                "trace_diagnostic_only=True",
                f"trace_limit={RESIZE_EVENT_TRACE_LIMIT}",
                f"trace_total_record_count={state.get('resize_event_trace_record_count', 0)}",
                f"trace_retained_count={len(trace)}",
                f"trace_dropped_count={state.get('resize_event_trace_dropped_count', 0)}",
                f"trace_failure_count={state.get('resize_event_trace_failure_count', 0)}",
                f"last_resize_event_trace_error={state.get('last_resize_event_trace_error', '')}",
                f"trace_write_count={state.get('resize_event_trace_write_count', 0)}",
                f"trace_first_sequence={first_sequence}",
                f"trace_last_sequence={last_sequence}",
            ]

            for event_name in event_names:
                lines.append(
                    f"trace_{event_name}_count="
                    + str(
                        sum(
                            1
                            for entry in trace
                            if entry["event"] == event_name
                        )
                    )
                )

            lines.extend(
                [
                    "",
                    "FORMAT",
                    "-" * 72,
                    (
                        "sequence|elapsed_ms|event|active|resize_pending|"
                        "frame_pending|allocation_count|frame_request_count|"
                        "frame_tick_count|full_overlay_queue_count|"
                        "bottom_recovery_request_count|"
                        "bottom_recovery_full_count|allocated_width|"
                        "allocated_height|columns|rows|overlay_x|overlay_y|"
                        "overlay_width|overlay_height|extension_height|"
                        "damage_x|damage_y|damage_width|damage_height|"
                        "clip_x1|clip_y1|clip_x2|clip_y2|bottom_points|"
                        "bottom_class|segment_count|lowest_display_row|"
                        "cache_miss_count"
                    ),
                    "",
                    "EVENTS",
                    "-" * 72,
                ]
            )

            for entry in trace:
                elapsed_ms = (
                    max(
                        int(entry["timestamp_ns"])
                        - origin_ns,
                        0,
                    )
                    / NANOSECONDS_PER_MILLISECOND
                )
                lines.append(
                    "|".join(
                        (
                            str(entry["sequence"]),
                            f"{elapsed_ms:.3f}",
                            str(entry["event"]),
                            str(entry["active"]),
                            str(entry["resize_pending"]),
                            str(entry["frame_pending"]),
                            str(entry["allocation_count"]),
                            str(entry["frame_request_count"]),
                            str(entry["frame_tick_count"]),
                            str(entry["full_overlay_queue_count"]),
                            str(entry["bottom_recovery_request_count"]),
                            str(entry["bottom_recovery_full_count"]),
                            str(entry["allocated_width"]),
                            str(entry["allocated_height"]),
                            str(entry["columns"]),
                            str(entry["rows"]),
                            str(entry["overlay_x"]),
                            str(entry["overlay_y"]),
                            str(entry["overlay_width"]),
                            str(entry["overlay_height"]),
                            str(entry["extension_height"]),
                            str(entry["damage_x"]),
                            str(entry["damage_y"]),
                            str(entry["damage_width"]),
                            str(entry["damage_height"]),
                            f"{entry['clip_x1']:.3f}",
                            f"{entry['clip_y1']:.3f}",
                            f"{entry['clip_x2']:.3f}",
                            f"{entry['clip_y2']:.3f}",
                            str(entry["bottom_points"]),
                            str(entry["bottom_class"]),
                            str(entry["segment_count"]),
                            str(entry["lowest_display_row"]),
                            str(entry["cache_miss_count"]),
                        )
                    )
                )

            lines.extend(
                [
                    "",
                    "SAFETY",
                    "-" * 72,
                    "drawing_changed=True",
                    "copy_changed=False",
                    "geometry_changed=False",
                    "native_calls_added=False",
                    "resize_behavior_changed=True",
                    "resize_clip_expansion_bounded=True",
                ]
            )

            RESIZE_EVENT_TRACE_PATH.write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
            )
            state["last_resize_event_trace_error"] = ""
            self._safe_write_diagnostic(
                vte,
                state.get("active", False),
                "resize_event_trace",
            )

        except Exception as exc:
            state["resize_event_trace_failure_count"] += 1
            state["last_resize_event_trace_error"] = (
                f"{type(exc).__name__}: {exc}"
            )

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

        # Opening the existing Copy menu is a strong bounded indication that
        # the user may click it next.  Re-arm preparation here after any
        # intervening terminal change without rebuilding after every output
        # burst while selection mode remains active.
        self._schedule_copy_snapshot(state)

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

    # TRANSACTIONAL_COPY_FAST_PATH_CANARY_V1
    @staticmethod
    def _copy_source_signature(vte, state):
        """Return the complete cheap certificate for one Copy snapshot."""

        adjustment = vte.get_vadjustment()

        try:
            cursor_position = vte.get_cursor_position()
            cursor_column = int(
                cursor_position.column
                if hasattr(cursor_position, "column")
                else cursor_position[0]
            )
            cursor_row = int(
                cursor_position.row
                if hasattr(cursor_position, "row")
                else cursor_position[1]
            )
        except Exception:
            cursor_column = -1
            cursor_row = -1

        return (
            int(state.get("copy_source_generation", 0)),
            round(float(adjustment.get_lower()), 9),
            round(float(adjustment.get_upper()), 9),
            round(float(adjustment.get_value()), 9),
            round(float(adjustment.get_page_size()), 9),
            max(int(vte.get_column_count()), 1),
            max(int(vte.get_row_count()), 1),
            cursor_column,
            cursor_row,
            str(
                state.get(
                    "visual_coordinate_validation",
                    "none",
                )
            ),
            int(state.get("visual_row_coordinate_offset", 0)),
            int(state.get("visual_absolute_top", 0)),
        )

    @classmethod
    def _certified_copy_coordinate_resolution(
        cls,
        vte,
        state,
    ):
        """Reuse an exact current visual mapping without another text scan."""

        if state.get("visual_coordinate_validation") != (
            "logical_window_exact"
        ):
            return None

        cache = state.get("render_cache")

        if not cache:
            return None

        geometry = cls._calculate_geometry(vte)

        if cache.get("signature") != cls._render_signature(
            vte,
            geometry,
        ):
            return None

        adjustment = vte.get_vadjustment()
        adjustment_top = math.floor(
            float(adjustment.get_value())
        )
        offset = int(
            state.get("visual_row_coordinate_offset", 0)
        )
        absolute_top = int(
            state.get("visual_absolute_top", 0)
        )

        if int(
            state.get("visual_adjustment_top", adjustment_top)
        ) != adjustment_top:
            return None

        if absolute_top != adjustment_top + offset:
            return None

        cursor_row = int(
            state.get("visual_cursor_absolute_row", -1)
        )
        visible_line_count = int(
            state.get("visual_coordinate_logical_line_count", 0)
        )

        return {
            "row_coordinate_offset": offset,
            "coordinate_match_score": 1.0,
            "coordinate_matching_line_count": visible_line_count,
            "coordinate_visible_line_count": visible_line_count,
            "coordinate_validation": "exact",
            "coordinate_window_mode": (
                "certified_visual_logical_window_exact"
            ),
            "coordinate_preferred_offset": offset,
            "coordinate_preferred_top": absolute_top,
            "coordinate_preferred_candidate_used": True,
            "adjustment_top": adjustment_top,
            "absolute_top": absolute_top,
            "cursor_absolute_row": cursor_row,
        }

    def _build_transactional_copy_payload(
        self,
        vte,
        state,
    ):
        """Build one stable immutable payload, retrying a changed source."""

        transaction_started_ns = time.perf_counter_ns()
        state["copy_transaction_last_coordinate_ns"] = 0
        state["copy_transaction_last_extract_ns"] = 0

        try:
            for attempt_index in range(
                COPY_TRANSACTION_MAX_ATTEMPTS
            ):
                state["copy_transaction_attempt_count"] += 1
                before_signature = self._copy_source_signature(
                    vte,
                    state,
                )

                coordinate_started_ns = time.perf_counter_ns()
                coordinate_resolution = (
                    self._certified_copy_coordinate_resolution(
                        vte,
                        state,
                    )
                )

                if coordinate_resolution is None:
                    state[
                        "copy_coordinate_certificate_miss_count"
                    ] += 1
                    preferred_visual_offset = None

                    if state.get(
                        "visual_coordinate_validation"
                    ) == "logical_window_exact":
                        preferred_visual_offset = int(
                            state.get(
                                "visual_row_coordinate_offset",
                                0,
                            )
                        )

                    coordinate_resolution = (
                        self._resolve_copy_row_coordinates(
                            vte,
                            preferred_offset=(
                                preferred_visual_offset
                            ),
                        )
                    )
                else:
                    state[
                        "copy_coordinate_certificate_hit_count"
                    ] += 1

                state["copy_transaction_last_coordinate_ns"] = (
                    time.perf_counter_ns()
                    - coordinate_started_ns
                )

                extract_started_ns = time.perf_counter_ns()
                text, start_row, end_row = (
                    self._extract_complete_scrollback(
                        vte,
                        coordinate_resolution[
                            "row_coordinate_offset"
                        ],
                    )
                )
                state["copy_transaction_last_extract_ns"] = (
                    time.perf_counter_ns()
                    - extract_started_ns
                )

                after_signature = self._copy_source_signature(
                    vte,
                    state,
                )

                if before_signature == after_signature:
                    state["copy_transaction_last_status"] = (
                        "stable"
                    )
                    return {
                        "text": text,
                        "character_count": len(text),
                        "line_count": (
                            len(text.splitlines())
                            if text
                            else 0
                        ),
                        "start_row": int(start_row),
                        "end_row": int(end_row),
                        "source_signature": after_signature,
                        "source_generation": int(
                            state.get(
                                "copy_source_generation",
                                0,
                            )
                        ),
                        "coordinate_resolution": dict(
                            coordinate_resolution
                        ),
                    }

                if (
                    attempt_index + 1
                    < COPY_TRANSACTION_MAX_ATTEMPTS
                ):
                    state["copy_transaction_retry_count"] += 1

            raise RuntimeError(
                "The terminal changed during every full-scrollback "
                "Copy transaction."
            )

        except Exception:
            state["copy_transaction_failure_count"] += 1
            state["copy_transaction_last_status"] = "failed"
            raise

        finally:
            elapsed_ns = (
                time.perf_counter_ns()
                - transaction_started_ns
            )
            state["copy_transaction_count"] += 1
            state["copy_transaction_last_total_ns"] = elapsed_ns
            state["copy_transaction_total_ns"] += elapsed_ns
            state["copy_transaction_max_ns"] = max(
                int(state["copy_transaction_max_ns"]),
                elapsed_ns,
            )

    def _schedule_copy_snapshot(self, state):
        """Debounce one prepared snapshot while selection mode is active."""

        if (
            not TRANSACTIONAL_COPY_ENABLED
            or not state.get("active", False)
        ):
            return

        snapshot = state.get("copy_snapshot")

        if snapshot is not None:
            try:
                if snapshot.get("source_signature") == (
                    self._copy_source_signature(
                        state["vte"],
                        state,
                    )
                ):
                    return
            except Exception:
                pass

        source_id = state.get("copy_snapshot_source_id")

        if source_id is not None:
            return

        generation = int(
            state.get("copy_source_generation", 0)
        )
        state["copy_snapshot_prepare_request_count"] += 1
        state["copy_snapshot_source_id"] = GLib.timeout_add(
            COPY_SNAPSHOT_QUIET_MS,
            self._prepare_copy_snapshot,
            id(state["vte"]),
            generation,
        )

    def _invalidate_copy_snapshot(
        self,
        state,
        reason,
        *,
        schedule=False,
    ):
        """Invalidate all prepared Copy data after a VTE state change."""

        if not TRANSACTIONAL_COPY_ENABLED:
            return

        source_id = state.get("copy_snapshot_source_id")

        if source_id is not None:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
            state["copy_snapshot_source_id"] = None

        state["copy_source_generation"] += 1
        state["copy_snapshot"] = None
        state["copy_snapshot_invalidation_count"] += 1
        state["copy_transaction_last_invalidation_reason"] = str(
            reason
        )

        if schedule:
            self._schedule_copy_snapshot(state)

    def _prepare_copy_snapshot(
        self,
        key,
        scheduled_generation,
    ):
        """Prepare a bounded full-scrollback payload after terminal quiet."""

        state = self.states.get(key)

        if state is None:
            return False

        state["copy_snapshot_source_id"] = None

        if (
            not TRANSACTIONAL_COPY_ENABLED
            or not state.get("active", False)
            or int(state.get("copy_source_generation", 0))
            != int(scheduled_generation)
        ):
            state["copy_snapshot_prepare_discard_count"] += 1
            return False

        try:
            adjustment = state["vte"].get_vadjustment()
            estimated_character_count = max(
                math.ceil(float(adjustment.get_upper()))
                - math.floor(float(adjustment.get_lower())),
                0,
            ) * max(
                int(state["vte"].get_column_count()),
                1,
            )
            state[
                "copy_snapshot_last_estimated_character_count"
            ] = estimated_character_count

            if estimated_character_count > (
                COPY_SNAPSHOT_MAX_CHARACTERS
            ):
                state["copy_snapshot_oversize_count"] += 1
                state["copy_snapshot"] = None
                return False

            payload = self._build_transactional_copy_payload(
                state["vte"],
                state,
            )

            if int(state.get("copy_source_generation", 0)) != int(
                scheduled_generation
            ):
                state[
                    "copy_snapshot_prepare_discard_count"
                ] += 1
                return False

            if payload["character_count"] > (
                COPY_SNAPSHOT_MAX_CHARACTERS
            ):
                state["copy_snapshot_oversize_count"] += 1
                state["copy_snapshot"] = None
                return False

            state["copy_snapshot"] = payload
            state["copy_snapshot_prepare_count"] += 1

        except Exception as exc:
            state["copy_snapshot"] = None
            state["copy_snapshot_prepare_failure_count"] += 1
            state["last_copy_error"] = (
                "snapshot_prepare_"
                f"{type(exc).__name__}: {exc}"
            )

        return False

    def _flush_copy_clipboard_store(
        self,
        key,
        clipboard,
    ):
        """Persist the already-published clipboard during GTK idle time."""

        state = self.states.get(key)

        if state is not None:
            state["copy_clipboard_store_source_id"] = None

        try:
            clipboard.store()

            if state is not None:
                state["copy_clipboard_store_success_count"] += 1
                state["copy_clipboard_store_last_error"] = ""

        except Exception as exc:
            if state is not None:
                state["copy_clipboard_store_failure_count"] += 1
                state["copy_clipboard_store_last_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

        finally:
            if state is not None:
                state["copy_clipboard_store_pending"] = False
                state["copy_clipboard_store_clipboard"] = None

                # Publish one final post-persistence report.  The menu
                # callback suppresses its earlier report for a successful
                # transactional Copy, avoiding duplicate diagnostic work.
                self._safe_write_diagnostic(
                    state["vte"],
                    bool(state.get("active", False)),
                    state.get(
                        "last_copy_action",
                        "native_copy_menu",
                    ),
                )

        return False

    def _schedule_copy_clipboard_store(
        self,
        state,
        clipboard,
    ):
        """Schedule persistence after set_text has acknowledged the Copy."""

        source_id = state.get(
            "copy_clipboard_store_source_id"
        )

        if source_id is not None:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass

        state["copy_clipboard_store_request_count"] += 1
        state["copy_clipboard_store_pending"] = True
        state["copy_clipboard_store_clipboard"] = clipboard

        try:
            state["copy_clipboard_store_source_id"] = (
                GLib.idle_add(
                    self._flush_copy_clipboard_store,
                    id(state["vte"]),
                    clipboard,
                )
            )
        except Exception:
            # Scheduling failure must not weaken clipboard persistence.
            self._flush_copy_clipboard_store(
                id(state["vte"]),
                clipboard,
            )

    @staticmethod
    def _persist_pending_copy_clipboard_on_teardown(state):
        """Finish deferred persistence before a pane/plugin disappears."""

        if not state.get("copy_clipboard_store_pending", False):
            return

        source_id = state.get(
            "copy_clipboard_store_source_id"
        )

        if source_id is not None:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass
            state["copy_clipboard_store_source_id"] = None

        clipboard = state.get("copy_clipboard_store_clipboard")

        try:
            if clipboard is not None:
                clipboard.store()
                state["copy_clipboard_store_success_count"] += 1
                state["copy_clipboard_store_last_error"] = ""
        except Exception as exc:
            state["copy_clipboard_store_failure_count"] += 1
            state["copy_clipboard_store_last_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            state["copy_clipboard_store_pending"] = False
            state["copy_clipboard_store_clipboard"] = None

    def _clear_copy_confirmation(
        self,
        key,
        expected_until_ns,
    ):
        """Remove only the confirmation associated with this timeout."""

        state = self.states.get(key)

        if state is None:
            return False

        state["copy_confirmation_source_id"] = None

        if int(state.get("copy_confirmation_until_ns", 0)) != int(
            expected_until_ns
        ):
            return False

        state["copy_confirmation_text"] = ""
        state["copy_confirmation_until_ns"] = 0
        state["copy_confirmation_clear_count"] += 1

        try:
            self._force_overlay_redraw(
                state["vte"],
                state,
            )
        except Exception:
            pass

        return False

    def _show_copy_confirmation(
        self,
        state,
        payload,
    ):
        """Show one bounded badge; it is not a second selection layer."""

        until_ns = (
            time.monotonic_ns()
            + COPY_CONFIRMATION_DURATION_MS
            * 1_000_000
        )
        state["copy_confirmation_text"] = (
            "Full scrollback copied · "
            f"{payload['line_count']:,} lines · "
            f"{payload['character_count']:,} chars"
        )
        state["copy_confirmation_until_ns"] = until_ns
        state["copy_confirmation_show_count"] += 1

        source_id = state.get("copy_confirmation_source_id")

        if source_id is not None:
            try:
                GLib.source_remove(source_id)
            except Exception:
                pass

        try:
            state["copy_confirmation_source_id"] = (
                GLib.timeout_add(
                    COPY_CONFIRMATION_DURATION_MS,
                    self._clear_copy_confirmation,
                    id(state["vte"]),
                    until_ns,
                )
            )
            self._force_overlay_redraw(
                state["vte"],
                state,
            )
        except Exception as exc:
            state["copy_confirmation_failure_count"] += 1
            state["copy_confirmation_last_error"] = (
                f"{type(exc).__name__}: {exc}"
            )

    def _copy_complete_visual_selection_transactional(
        self,
        terminal,
        state,
        action,
    ):
        """Publish one certified full-scrollback payload atomically."""

        acknowledged_started_ns = time.perf_counter_ns()
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
                    "copy_coordinate_window_mode": (
                        "transactional_logical_multi_row"
                    ),
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

            current_signature = self._copy_source_signature(
                vte,
                state,
            )
            snapshot = state.get("copy_snapshot")

            if (
                snapshot is not None
                and snapshot.get("source_signature")
                == current_signature
            ):
                payload = snapshot
                state["copy_snapshot_hit_count"] += 1
                state["copy_transaction_last_path"] = (
                    "prepared_snapshot"
                )
            else:
                state["copy_snapshot_miss_count"] += 1
                payload = self._build_transactional_copy_payload(
                    vte,
                    state,
                )
                state["copy_transaction_last_path"] = (
                    "same_click_exact_fallback"
                )

            # One final cheap certificate check closes the interval between
            # selecting a prepared payload and publishing it.
            if payload["source_signature"] != (
                self._copy_source_signature(vte, state)
            ):
                state["copy_transaction_retry_count"] += 1
                payload = self._build_transactional_copy_payload(
                    vte,
                    state,
                )
                state["copy_transaction_last_path"] = (
                    "same_click_changed_source_retry"
                )

            if (
                state["copy_transaction_last_path"]
                != "prepared_snapshot"
                and payload["character_count"]
                <= COPY_SNAPSHOT_MAX_CHARACTERS
            ):
                state["copy_snapshot"] = payload
                state[
                    "copy_snapshot_same_click_seed_count"
                ] += 1

            coordinate_resolution = payload[
                "coordinate_resolution"
            ]
            state["copy_coordinate_resolution_count"] = (
                state.get("copy_coordinate_resolution_count", 0)
                + 1
            )
            state.update(
                {
                    f"copy_{name}": value
                    for name, value in (
                        coordinate_resolution.items()
                    )
                }
            )
            state["copy_mapped_start_row"] = payload[
                "start_row"
            ]
            state["copy_mapped_end_row"] = payload["end_row"]

            # set_text atomically replaces clipboard ownership only after the
            # complete immutable payload and all coordinate checks succeed.
            clipboard = terminal.clipboard
            clipboard.set_text(payload["text"], -1)
            state["copy_clipboard_set_count"] += 1

            state["copy_count"] += 1
            state["last_copy_characters"] = payload[
                "character_count"
            ]
            state["last_copy_lines"] = payload["line_count"]
            state["last_copy_start_row"] = payload["start_row"]
            state["last_copy_end_row"] = payload["end_row"]

            acknowledged_ns = (
                time.perf_counter_ns()
                - acknowledged_started_ns
            )
            state["copy_ack_last_ns"] = acknowledged_ns
            state["copy_ack_total_ns"] += acknowledged_ns
            state["copy_ack_max_ns"] = max(
                int(state["copy_ack_max_ns"]),
                acknowledged_ns,
            )

            self._schedule_copy_clipboard_store(
                state,
                clipboard,
            )
            self._show_copy_confirmation(
                state,
                payload,
            )
            return True

        except Exception as exc:
            state["last_copy_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            return False

    def _copy_complete_visual_selection(
        self,
        terminal,
        state,
        action,
    ):
        """
        Copy the complete retained scrollback to the normal clipboard.
        """

        if TRANSACTIONAL_COPY_ENABLED:
            return (
                self._copy_complete_visual_selection_transactional(
                    terminal,
                    state,
                    action,
                )
            )

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

        copied = self._copy_complete_visual_selection(
            terminal,
            state,
            "native_copy_menu",
        )

        if not (
            TRANSACTIONAL_COPY_ENABLED
            and copied
        ):
            # Legacy and failure paths still report immediately. A successful
            # transactional Copy writes once after deferred persistence.
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

            self._cancel_resize_frame_repaint(
                vte,
                state,
            )

            self._persist_pending_copy_clipboard_on_teardown(
                state
            )

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
                "bottom_damage_repaint_source_id",
                "toggle_repaint_source_id",
                "copy_snapshot_source_id",
                "copy_clipboard_store_source_id",
                "copy_confirmation_source_id",
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
            "bottom_damage_repaint_source_id": None,
            "toggle_repaint_source_id": None,
            "copy_snapshot_source_id": None,
            "copy_clipboard_store_source_id": None,
            "copy_confirmation_source_id": None,
            "resize_frame_repaint_tick_callback_id": None,
            "resize_frame_repaint_last_allocation_ns": 0,
            "resize_frame_repaint_frames_since_allocation": 0,
            "resize_frame_repaint_settled_attempt_count": 0,
            "resize_frame_repaint_last_quiet_ms": 0.0,
            "resize_frame_repaint_request_count": 0,
            "resize_frame_repaint_install_count": 0,
            "resize_frame_repaint_coalesced_count": 0,
            "resize_frame_repaint_tick_count": 0,
            "resize_frame_repaint_full_overlay_queue_count": 0,
            "resize_frame_repaint_full_overlay_skip_count": 0,
            "resize_frame_repaint_final_full_count": 0,
            "resize_frame_repaint_cache_refresh_count": 0,
            "resize_frame_repaint_coordinate_retry_failure_count": 0,
            "resize_frame_repaint_cancel_count": 0,
            "resize_frame_repaint_skip_inactive_count": 0,
            "resize_frame_repaint_failure_count": 0,
            "last_resize_frame_repaint_error": "",
            "last_resize_frame_repaint_x": 0,
            "last_resize_frame_repaint_y": 0,
            "last_resize_frame_repaint_width": 0,
            "last_resize_frame_repaint_height": 0,
            "resize_event_trace": (
                deque(maxlen=RESIZE_EVENT_TRACE_LIMIT)
                if RESIZE_EVENT_TRACE_ENABLED
                else None
            ),
            "resize_event_trace_origin_ns": 0,
            "resize_event_trace_next_sequence": 0,
            "resize_event_trace_record_count": 0,
            "resize_event_trace_dropped_count": 0,
            "resize_event_trace_write_count": 0,
            "resize_event_trace_failure_count": 0,
            "last_resize_event_trace_error": "",
            "resize_draw_clip_expansion_check_count": 0,
            "resize_draw_clip_expansion_request_count": 0,
            "resize_draw_clip_expansion_applied_count": 0,
            "resize_draw_clip_expansion_effective_count": 0,
            "resize_draw_clip_expansion_ineffective_count": 0,
            "resize_draw_clip_expansion_complete_skip_count": 0,
            "resize_draw_clip_expansion_failure_count": 0,
            "last_resize_draw_clip_expansion_effective": False,
            "last_resize_draw_clip_expansion_error": "",
            "last_resize_draw_clip_expansion_x1": 0.0,
            "last_resize_draw_clip_expansion_y1": 0.0,
            "last_resize_draw_clip_expansion_x2": 0.0,
            "last_resize_draw_clip_expansion_y2": 0.0,
            "last_resize_draw_clip_target_x": 0,
            "last_resize_draw_clip_target_y": 0,
            "last_resize_draw_clip_target_width": 0,
            "last_resize_draw_clip_target_height": 0,
            "resize_reflow_continuity_snapshot": None,
            "resize_reflow_continuity_check_count": 0,
            "resize_reflow_continuity_candidate_count": 0,
            "resize_reflow_continuity_applied_count": 0,
            "resize_reflow_continuity_empty_count": 0,
            "resize_reflow_continuity_non_resize_skip_count": 0,
            "resize_reflow_continuity_no_snapshot_skip_count": 0,
            "resize_reflow_continuity_age_skip_count": 0,
            "resize_reflow_continuity_stable_geometry_skip_count": 0,
            "resize_reflow_continuity_summary_changed_skip_count": 0,
            "resize_reflow_continuity_no_shift_skip_count": 0,
            "resize_reflow_continuity_no_wrap_edge_skip_count": 0,
            "resize_reflow_continuity_coordinate_skip_count": 0,
            "resize_reflow_continuity_failure_count": 0,
            "resize_reflow_continuity_segment_total": 0,
            "resize_reflow_continuity_ordinary_segment_total": 0,
            "resize_reflow_continuity_command_segment_total": 0,
            "resize_reflow_continuity_line_number_segment_total": 0,
            "last_resize_reflow_continuity_error": "",
            "last_resize_reflow_continuity_age_ms": 0.0,
            "last_resize_reflow_continuity_shift_rows": 0,
            "last_resize_reflow_continuity_previous_columns": 0,
            "last_resize_reflow_continuity_previous_rows": 0,
            "last_resize_reflow_continuity_current_columns": 0,
            "last_resize_reflow_continuity_current_rows": 0,
            "last_resize_reflow_continuity_previous_segment_count": 0,
            "last_resize_reflow_continuity_previous_lowest_row": -1,
            "last_resize_reflow_continuity_current_segment_count": 0,
            "last_resize_reflow_continuity_current_lowest_row": -1,
            "last_resize_reflow_continuity_output_count": 0,
            "last_resize_reflow_continuity_source_tail_count": 0,
            "visible_row_overscan_draw_count": 0,
            "visible_row_overscan_segment_total": 0,
            "visible_row_overscan_last_segment_count": 0,
            "visible_row_overscan_last_extension_height": 0,
            "visible_row_overscan_max_extension_height": 0,
            "visible_row_overscan_last_lowest_display_row": -1,
            "toggle_repaint_target_active": False,
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
            "toggle_repaint_request_count": 0,
            "toggle_repaint_activation_request_count": 0,
            "toggle_repaint_deactivation_request_count": 0,
            "toggle_repaint_full_count": 0,
            "toggle_repaint_follow_up_count": 0,
            "toggle_repaint_cancel_count": 0,
            "toggle_repaint_failure_count": 0,
            "last_toggle_repaint_error": "",
            "resize_refresh_count": 0,
            "resize_bottom_damage_request_count": 0,
            "resize_bottom_damage_queue_count": 0,
            "resize_bottom_damage_skip_count": 0,
            "resize_bottom_damage_failure_count": 0,
            "last_resize_bottom_damage_error": "",
            "last_resize_bottom_damage_x": 0,
            "last_resize_bottom_damage_y": 0,
            "last_resize_bottom_damage_width": 0,
            "last_resize_bottom_damage_height": 0,
            "bottom_damage_repaint_request_count": 0,
            "bottom_damage_repaint_schedule_count": 0,
            "bottom_damage_repaint_coalesced_count": 0,
            "bottom_damage_repaint_full_count": 0,
            "bottom_damage_repaint_skip_inactive_count": 0,
            "bottom_damage_repaint_failure_count": 0,
            "last_bottom_damage_repaint_error": "",
            "size_allocate_event_count": 0,
            "draw_damage_clip_probe_count": 0,
            "draw_damage_clip_probe_failure_count": 0,
            "last_draw_damage_clip_error": "",
            "last_draw_clip_x1": 0.0,
            "last_draw_clip_y1": 0.0,
            "last_draw_clip_x2": 0.0,
            "last_draw_clip_y2": 0.0,
            "last_draw_clip_resize_pending": False,
            "last_bottom_probe_absolute_row": -1,
            "last_bottom_probe_display_row": -1,
            "last_bottom_probe_x": 0,
            "last_bottom_probe_y": 0,
            "last_bottom_probe_width": 0,
            "last_bottom_probe_height": 0,
            "last_bottom_probe_visible_x1": 0.0,
            "last_bottom_probe_visible_y1": 0.0,
            "last_bottom_probe_visible_x2": 0.0,
            "last_bottom_probe_visible_y2": 0.0,
            "last_bottom_probe_expected_point_count": (
                BOTTOM_DAMAGE_PROBE_POINT_COUNT
            ),
            "last_bottom_probe_points_in_clip": 0,
            "last_bottom_probe_top_points_in_clip": 0,
            "last_bottom_probe_middle_points_in_clip": 0,
            "last_bottom_probe_bottom_points_in_clip": 0,
            "bottom_probe_no_segment_count": 0,
            "bottom_probe_full_clip_count": 0,
            "bottom_probe_partial_clip_count": 0,
            "bottom_probe_excluded_count": 0,
            "resize_draw_count": 0,
            "resize_bottom_full_clip_count": 0,
            "resize_bottom_partial_clip_count": 0,
            "resize_bottom_excluded_count": 0,
            "resize_bottom_incomplete_streak": 0,
            "resize_bottom_incomplete_max_streak": 0,
            "last_refresh_reason": "none",
            "render_cache": None,
            "cache_generation": 0,
            "cache_refresh_count": 0,
            "last_cache_reason": "none",
            "last_draw_cache_miss_count": 0,
            "draw_cache_miss_frame_count": 0,
            "draw_cache_miss_row_total": 0,
            "resize_draw_cache_miss_frame_count": 0,
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
            "copy_source_generation": 0,
            "copy_snapshot": None,
            "copy_snapshot_invalidation_count": 0,
            "copy_snapshot_prepare_request_count": 0,
            "copy_snapshot_prepare_count": 0,
            "copy_snapshot_prepare_failure_count": 0,
            "copy_snapshot_prepare_discard_count": 0,
            "copy_snapshot_oversize_count": 0,
            "copy_snapshot_last_estimated_character_count": 0,
            "copy_snapshot_hit_count": 0,
            "copy_snapshot_miss_count": 0,
            "copy_snapshot_same_click_seed_count": 0,
            "copy_coordinate_certificate_hit_count": 0,
            "copy_coordinate_certificate_miss_count": 0,
            "copy_transaction_count": 0,
            "copy_transaction_attempt_count": 0,
            "copy_transaction_retry_count": 0,
            "copy_transaction_failure_count": 0,
            "copy_transaction_last_status": "not_run",
            "copy_transaction_last_path": "none",
            "copy_transaction_last_invalidation_reason": "none",
            "copy_transaction_last_total_ns": 0,
            "copy_transaction_total_ns": 0,
            "copy_transaction_max_ns": 0,
            "copy_transaction_last_coordinate_ns": 0,
            "copy_transaction_last_extract_ns": 0,
            "copy_ack_last_ns": 0,
            "copy_ack_total_ns": 0,
            "copy_ack_max_ns": 0,
            "copy_clipboard_set_count": 0,
            "copy_clipboard_store_pending": False,
            "copy_clipboard_store_clipboard": None,
            "copy_clipboard_store_request_count": 0,
            "copy_clipboard_store_success_count": 0,
            "copy_clipboard_store_failure_count": 0,
            "copy_clipboard_store_last_error": "",
            "copy_confirmation_text": "",
            "copy_confirmation_until_ns": 0,
            "copy_confirmation_show_count": 0,
            "copy_confirmation_clear_count": 0,
            "copy_confirmation_draw_count": 0,
            "copy_confirmation_failure_count": 0,
            "copy_confirmation_last_error": "",
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
            "native_geometry_shadow_request_count": 0,
            "native_geometry_shadow_match_count": 0,
            "native_geometry_shadow_mismatch_count": 0,
            "native_geometry_shadow_failure_count": 0,
            "native_geometry_shadow_skip_count": 0,
            "native_geometry_shadow_last_status": (
                "superseded_by_frame_batch"
                if NATIVE_SHADOW_VALIDATION_ENABLED
                else "disabled_production"
            ),
            "native_geometry_shadow_last_mismatch_fields": "",
            "native_geometry_shadow_last_error": "",
            "native_geometry_shadow_total_ns": 0,
            "native_geometry_shadow_last_ns": 0,
            "native_geometry_shadow_max_ns": 0,
            "native_segment_shadow_request_count": 0,
            "native_segment_shadow_match_count": 0,
            "native_segment_shadow_mismatch_count": 0,
            "native_segment_shadow_failure_count": 0,
            "native_segment_shadow_skip_count": 0,
            "native_segment_shadow_last_status": (
                "superseded_by_frame_batch"
                if NATIVE_SHADOW_VALIDATION_ENABLED
                else "disabled_production"
            ),
            "native_segment_shadow_last_mismatch_fields": "",
            "native_segment_shadow_last_error": "",
            "native_segment_shadow_last_row_content_count": 0,
            "native_segment_shadow_last_python_count": 0,
            "native_segment_shadow_last_native_count": 0,
            "native_segment_shadow_total_ns": 0,
            "native_segment_shadow_last_ns": 0,
            "native_segment_shadow_max_ns": 0,
            "native_frame_shadow_request_count": 0,
            "native_frame_shadow_match_count": 0,
            "native_frame_shadow_mismatch_count": 0,
            "native_frame_shadow_failure_count": 0,
            "native_frame_shadow_skip_count": 0,
            "native_frame_shadow_last_status": (
                "not_run"
                if NATIVE_SHADOW_VALIDATION_ENABLED
                else "disabled_production"
            ),
            "native_frame_shadow_last_mismatch_fields": "",
            "native_frame_shadow_last_error": "",
            "native_frame_shadow_last_row_content_count": 0,
            "native_frame_shadow_last_required_capacity": 0,
            "native_frame_shadow_last_python_segment_count": 0,
            "native_frame_shadow_last_native_segment_count": 0,
            "native_frame_shadow_total_ns": 0,
            "native_frame_shadow_last_ns": 0,
            "native_frame_shadow_max_ns": 0,
            "native_frame_authority_attempt_count": 0,
            "native_frame_authority_applied_count": 0,
            "native_frame_authority_fallback_count": 0,
            "native_frame_authority_latch_count": 0,
            "native_frame_authority_latched_skip_count": 0,
            "native_frame_authority_latched_off": False,
            "native_frame_authority_last_applied": False,
            "native_frame_authority_last_fallback_reason": "",
            "native_frame_authority_last_error": "",
            "native_draw_frame_shadow_request_count": 0,
            "native_draw_frame_shadow_match_count": 0,
            "native_draw_frame_shadow_mismatch_count": 0,
            "native_draw_frame_shadow_failure_count": 0,
            "native_draw_frame_shadow_skip_count": 0,
            "native_draw_frame_shadow_last_status": (
                "not_run"
                if NATIVE_DRAW_FRAME_SHADOW_ENABLED
                else "disabled_production"
            ),
            "native_draw_frame_shadow_last_mismatch_fields": "",
            "native_draw_frame_shadow_last_error": "",
            "native_draw_frame_shadow_last_python_segment_count": 0,
            "native_draw_frame_shadow_last_native_segment_count": 0,
            "native_draw_frame_shadow_total_ns": 0,
            "native_draw_frame_shadow_last_ns": 0,
            "native_draw_frame_shadow_max_ns": 0,
            "native_draw_frame_authority_attempt_count": 0,
            "native_draw_frame_authority_applied_count": 0,
            "native_draw_frame_authority_fallback_count": 0,
            "native_draw_frame_authority_latch_count": 0,
            "native_draw_frame_authority_latched_skip_count": 0,
            "native_draw_frame_authority_latched_off": False,
            "native_draw_frame_authority_last_applied": False,
            "native_draw_frame_authority_last_fallback_reason": "",
            "native_draw_frame_authority_last_error": "",
            "native_row_content_frame_extract_count": 0,
            "native_row_content_frame_extract_attempted_row_total": 0,
            "native_row_content_frame_extract_content_row_total": 0,
            "native_row_content_frame_extract_last_content_row_count": 0,
            "native_row_content_frame_extract_total_ns": 0,
            "native_row_content_frame_extract_last_ns": 0,
            "native_row_content_frame_extract_max_ns": 0,
            "native_row_content_frame_projection_count": 0,
            "native_row_content_frame_projection_total_ns": 0,
            "native_row_content_frame_projection_last_ns": 0,
            "native_row_content_frame_projection_max_ns": 0,
            "native_row_content_frame_request_count": 0,
            "native_row_content_frame_match_count": 0,
            "native_row_content_frame_mismatch_count": 0,
            "native_row_content_frame_failure_count": 0,
            "native_row_content_frame_skip_count": 0,
            "native_row_content_frame_fallback_count": 0,
            "native_row_content_frame_latch_count": 0,
            "native_row_content_frame_latched_off": False,
            "native_row_content_frame_last_status": (
                "not_run"
                if NATIVE_ROW_CONTENT_FRAME_ENABLED
                else "disabled"
            ),
            "native_row_content_frame_last_error": "",
            "native_row_content_frame_last_row_content_count": 0,
            "native_row_content_frame_last_python_segment_count": 0,
            "native_row_content_frame_last_native_segment_count": 0,
            "native_row_content_fast_path_request_count": 0,
            "native_row_content_fast_path_native_call_count": 0,
            "native_row_content_fast_path_application_count": 0,
            "native_row_content_fast_path_exact_check_count": 0,
            "native_row_content_fast_path_exact_match_count": 0,
            "native_row_content_fast_path_exact_mismatch_count": 0,
            "native_row_content_fast_path_structural_check_count": 0,
            "native_row_content_fast_path_structural_accept_count": 0,
            "native_row_content_fast_path_structural_reject_count": 0,
            "native_row_content_fast_path_projection_avoided_count": 0,
            "native_row_content_fast_path_fallback_projection_count": 0,
            "native_row_content_fast_path_failure_count": 0,
            "native_row_content_fast_path_fallback_count": 0,
            "native_row_content_fast_path_latch_count": 0,
            "native_row_content_fast_path_latched_skip_count": 0,
            "native_row_content_fast_path_latched_off": False,
            "native_row_content_fast_path_last_status": (
                "not_run"
                if NATIVE_ROW_CONTENT_FAST_PATH_ENABLED
                else "disabled"
            ),
            "native_row_content_fast_path_last_error": "",
            "native_row_content_fast_path_last_exact_check": False,
            "native_row_content_fast_path_last_python_segment_count": 0,
            "native_row_content_fast_path_last_native_segment_count": 0,
            "native_row_content_fast_path_total_ns": 0,
            "native_row_content_fast_path_last_ns": 0,
            "native_row_content_fast_path_max_ns": 0,
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

        self._cancel_toggle_repaint(state)

        state["active"] = bool(active)
        state["resize_reflow_continuity_snapshot"] = None

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

            self._invalidate_copy_snapshot(
                state,
                "selection_activated",
                schedule=True,
            )
        else:
            self._invalidate_copy_snapshot(
                state,
                "selection_deactivated",
                schedule=False,
            )

            confirmation_source_id = state.get(
                "copy_confirmation_source_id"
            )

            if confirmation_source_id is not None:
                try:
                    GLib.source_remove(
                        confirmation_source_id
                    )
                except Exception:
                    pass
                state["copy_confirmation_source_id"] = None

            state["copy_confirmation_text"] = ""
            state["copy_confirmation_until_ns"] = 0

        # DEACTIVATION_REDRAW_LATENCY_REPAIR_V1
        #
        # Keep the completed cache available while writing the optional
        # deactivation diagnostic. Clearing it first made _write_diagnostic()
        # synchronously re-extract and semantically analyse every visible row
        # on GTK's UI thread. That delayed the full repaint which removes the
        # old overlay, especially after VTE clear/reflow activity.
        #
        # The draw callback already observes active=False and therefore cannot
        # paint from this retained cache. It is discarded immediately after
        # the diagnostic attempt and before the redraw is requested.

        self._safe_write_diagnostic(
            vte,
            state["active"],
            action,
        )

        if not state["active"]:
            state["render_cache"] = None

        self._force_overlay_redraw(vte, state)

        self._schedule_toggle_repaint(
            vte,
            state,
        )

        if state["active"]:
            self._schedule_live_refresh(
                id(vte),
                "selection_activated",
            )

    def on_vte_destroy(self, _vte, key):
        state = self.states.pop(key, None)

        if state is None:
            return

        self._cancel_resize_frame_repaint(
            state["vte"],
            state,
        )

        self._persist_pending_copy_clipboard_on_teardown(
            state
        )

        for source_name in (
            "refresh_source_id",
            "resize_source_id",
            "bottom_damage_repaint_source_id",
            "toggle_repaint_source_id",
            "copy_snapshot_source_id",
            "copy_clipboard_store_source_id",
            "copy_confirmation_source_id",
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
        state = self.states.get(key)

        if (
            state is not None
            and state.get("resize_source_id") is None
        ):
            state["resize_reflow_continuity_snapshot"] = None

        if state is not None:
            self._invalidate_copy_snapshot(
                state,
                "contents_changed",
            )

        self._schedule_live_refresh(
            key,
            "contents_changed",
        )

    def on_vte_cursor_moved(self, _vte, key):
        state = self.states.get(key)

        if (
            state is not None
            and state.get("resize_source_id") is None
        ):
            state["resize_reflow_continuity_snapshot"] = None

        if state is not None:
            self._invalidate_copy_snapshot(
                state,
                "cursor_moved",
            )

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
        state = self.states.get(key)

        if (
            state is not None
            and state.get("resize_source_id") is None
        ):
            state["resize_reflow_continuity_snapshot"] = None

        if state is not None:
            self._invalidate_copy_snapshot(
                state,
                "commit",
            )

        self._schedule_live_refresh(
            key,
            "commit",
        )

    @classmethod
    def _record_resize_event_trace(
        cls,
        vte,
        state,
        event,
        *,
        geometry=None,
        bottom_points=-1,
        segment_count=-1,
        lowest_display_row=-1,
        cache_miss_count=-1,
    ):
        """Append one bounded diagnostic event without changing drawing."""

        if not RESIZE_EVENT_TRACE_ENABLED:
            return

        try:
            trace = state.get("resize_event_trace")

            if trace is None:
                return

            timestamp_ns = time.monotonic_ns()

            if not state.get("resize_event_trace_origin_ns", 0):
                state["resize_event_trace_origin_ns"] = timestamp_ns

            sequence = int(
                state.get("resize_event_trace_next_sequence", 0)
            )
            state["resize_event_trace_next_sequence"] = sequence + 1

            if geometry is not None:
                allocated_width = int(
                    geometry.get("allocated_width", -1)
                )
                allocated_height = int(
                    geometry.get("allocated_height", -1)
                )
                columns = int(geometry.get("columns", -1))
                rows = int(geometry.get("rows", -1))
                overlay_clip = cls._calculate_overlay_clip_rectangle(
                    geometry
                )
                overlay_x = int(overlay_clip["x"])
                overlay_y = int(overlay_clip["y"])
                overlay_width = int(overlay_clip["width"])
                overlay_height = int(overlay_clip["height"])
                extension_height = int(
                    overlay_clip["extension_height"]
                )
            else:
                raw_geometry = {}

                for field_name, method_name in (
                    ("allocated_width", "get_allocated_width"),
                    ("allocated_height", "get_allocated_height"),
                    ("columns", "get_column_count"),
                    ("rows", "get_row_count"),
                ):
                    try:
                        raw_geometry[field_name] = int(
                            getattr(vte, method_name)()
                        )
                    except Exception:
                        raw_geometry[field_name] = -1

                allocated_width = raw_geometry["allocated_width"]
                allocated_height = raw_geometry["allocated_height"]
                columns = raw_geometry["columns"]
                rows = raw_geometry["rows"]
                overlay_x = int(
                    state.get("last_resize_frame_repaint_x", -1)
                )
                overlay_y = int(
                    state.get("last_resize_frame_repaint_y", -1)
                )
                overlay_width = int(
                    state.get("last_resize_frame_repaint_width", -1)
                )
                overlay_height = int(
                    state.get(
                        "last_resize_frame_repaint_height",
                        -1,
                    )
                )
                extension_height = -1

            if bottom_points is None:
                bottom_class = "no_segment"
                retained_bottom_points = -1
            else:
                retained_bottom_points = int(bottom_points)

                if retained_bottom_points < 0:
                    bottom_class = "not_sampled"
                elif retained_bottom_points == 0:
                    bottom_class = "excluded"
                elif (
                    retained_bottom_points
                    == BOTTOM_DAMAGE_PROBE_POINT_COUNT
                ):
                    bottom_class = "full"
                else:
                    bottom_class = "partial"

            is_draw = str(event) == "draw"
            entry = {
                "sequence": sequence,
                "timestamp_ns": timestamp_ns,
                "event": str(event),
                "active": bool(state.get("active", False)),
                "resize_pending": (
                    state.get("resize_source_id") is not None
                ),
                "frame_pending": (
                    state.get(
                        "resize_frame_repaint_tick_callback_id"
                    )
                    is not None
                ),
                "allocation_count": int(
                    state.get("size_allocate_event_count", 0)
                ),
                "frame_request_count": int(
                    state.get("resize_frame_repaint_request_count", 0)
                ),
                "frame_tick_count": int(
                    state.get("resize_frame_repaint_tick_count", 0)
                ),
                "full_overlay_queue_count": int(
                    state.get(
                        "resize_frame_repaint_full_overlay_queue_count",
                        0,
                    )
                ),
                "bottom_recovery_request_count": int(
                    state.get("bottom_damage_repaint_request_count", 0)
                ),
                "bottom_recovery_full_count": int(
                    state.get("bottom_damage_repaint_full_count", 0)
                ),
                "allocated_width": allocated_width,
                "allocated_height": allocated_height,
                "columns": columns,
                "rows": rows,
                "overlay_x": overlay_x,
                "overlay_y": overlay_y,
                "overlay_width": overlay_width,
                "overlay_height": overlay_height,
                "extension_height": extension_height,
                "damage_x": int(
                    state.get("last_resize_frame_repaint_x", -1)
                ),
                "damage_y": int(
                    state.get("last_resize_frame_repaint_y", -1)
                ),
                "damage_width": int(
                    state.get("last_resize_frame_repaint_width", -1)
                ),
                "damage_height": int(
                    state.get("last_resize_frame_repaint_height", -1)
                ),
                "clip_x1": float(
                    state.get("last_draw_clip_x1", -1.0)
                    if is_draw
                    else -1.0
                ),
                "clip_y1": float(
                    state.get("last_draw_clip_y1", -1.0)
                    if is_draw
                    else -1.0
                ),
                "clip_x2": float(
                    state.get("last_draw_clip_x2", -1.0)
                    if is_draw
                    else -1.0
                ),
                "clip_y2": float(
                    state.get("last_draw_clip_y2", -1.0)
                    if is_draw
                    else -1.0
                ),
                "bottom_points": retained_bottom_points,
                "bottom_class": bottom_class,
                "segment_count": int(segment_count),
                "lowest_display_row": int(lowest_display_row),
                "cache_miss_count": int(cache_miss_count),
            }

            if len(trace) >= RESIZE_EVENT_TRACE_LIMIT:
                state["resize_event_trace_dropped_count"] += 1

            trace.append(entry)
            state["resize_event_trace_record_count"] += 1
            state["last_resize_event_trace_error"] = ""

        except Exception as exc:
            state["resize_event_trace_failure_count"] += 1
            state["last_resize_event_trace_error"] = (
                f"{type(exc).__name__}: {exc}"
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

        state["size_allocate_event_count"] += 1

        self._invalidate_copy_snapshot(
            state,
            "size_allocate",
        )

        self._queue_live_resize_bottom_damage(
            vte,
            state,
        )

        self._schedule_resize_frame_repaint(
            vte,
            state,
        )

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

        if RESIZE_EVENT_TRACE_ENABLED:
            self._record_resize_event_trace(
                vte,
                state,
                "allocation",
            )

        # Incoming draw damage is still inspected separately. If GTK does not
        # honor or fully cover this targeted request, the v41 classifier
        # schedules its coalesced full backing repaint as a fallback.

    def _queue_live_resize_bottom_damage(self, vte, state):
        """Queue the current bottom cell-height rectangle immediately."""

        state["resize_bottom_damage_request_count"] += 1

        try:
            geometry = self._calculate_geometry(vte)
            overlay_clip = self._calculate_overlay_clip_rectangle(
                geometry
            )
            width = max(int(overlay_clip["width"]), 0)
            height = max(int(overlay_clip["height"]), 0)
            character_height = max(
                int(geometry["character_height"]),
                0,
            )

            if width <= 0 or height <= 0 or character_height <= 0:
                state["resize_bottom_damage_skip_count"] += 1
                return

            area_height = min(
                character_height,
                height,
            )
            x = int(overlay_clip["x"])
            y = int(
                overlay_clip["y"]
                + height
                - area_height
            )

            vte.queue_draw_area(
                x,
                y,
                width,
                area_height,
            )

            state["resize_bottom_damage_queue_count"] += 1
            state["last_resize_bottom_damage_error"] = ""
            state["last_resize_bottom_damage_x"] = x
            state["last_resize_bottom_damage_y"] = y
            state["last_resize_bottom_damage_width"] = width
            state["last_resize_bottom_damage_height"] = area_height

        except Exception as exc:
            state["resize_bottom_damage_failure_count"] += 1
            state["last_resize_bottom_damage_error"] = (
                f"{type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _cancel_resize_frame_repaint(vte, state):
        """Remove a pending GTK frame callback during teardown."""

        callback_id = state.get(
            "resize_frame_repaint_tick_callback_id"
        )

        if callback_id is None:
            return

        state["resize_frame_repaint_tick_callback_id"] = None
        state["resize_frame_repaint_frames_since_allocation"] = 0

        try:
            vte.remove_tick_callback(callback_id)
        except Exception:
            pass

        state["resize_frame_repaint_cancel_count"] += 1

    def _schedule_resize_frame_repaint(self, vte, state):
        """Keep bottom damage queued through committed resize frames."""

        state["resize_frame_repaint_request_count"] += 1
        state["resize_frame_repaint_last_allocation_ns"] = (
            time.monotonic_ns()
        )
        state["resize_frame_repaint_frames_since_allocation"] = 0
        state["resize_frame_repaint_settled_attempt_count"] = 0

        if state.get("resize_frame_repaint_tick_callback_id") is not None:
            state["resize_frame_repaint_coalesced_count"] += 1
            return

        try:
            callback_id = vte.add_tick_callback(
                self._on_resize_frame_repaint_tick,
                id(vte),
            )

            if not callback_id:
                raise RuntimeError(
                    "Gtk.Widget.add_tick_callback returned no callback id"
                )

            state["resize_frame_repaint_tick_callback_id"] = callback_id
            state["resize_frame_repaint_install_count"] += 1
            state["last_resize_frame_repaint_error"] = ""

        except Exception as exc:
            state["resize_frame_repaint_tick_callback_id"] = None
            state["resize_frame_repaint_failure_count"] += 1
            state["last_resize_frame_repaint_error"] = (
                f"schedule:{type(exc).__name__}: {exc}"
            )

    def _queue_resize_frame_full_overlay_damage(self, vte, state):
        """Queue the complete bounded overlay from a committed frame."""

        try:
            geometry = self._calculate_geometry(vte)
            overlay_clip = self._calculate_overlay_clip_rectangle(
                geometry
            )
            width = max(int(overlay_clip["width"]), 0)
            height = max(int(overlay_clip["height"]), 0)

            if width <= 0 or height <= 0:
                state[
                    "resize_frame_repaint_full_overlay_skip_count"
                ] += 1
                return False

            x = int(overlay_clip["x"])
            y = int(overlay_clip["y"])

            vte.queue_draw_area(
                x,
                y,
                width,
                height,
            )

            state[
                "resize_frame_repaint_full_overlay_queue_count"
            ] += 1
            state["last_resize_frame_repaint_error"] = ""
            state["last_resize_frame_repaint_x"] = x
            state["last_resize_frame_repaint_y"] = y
            state["last_resize_frame_repaint_width"] = width
            state["last_resize_frame_repaint_height"] = height
            return True

        except Exception as exc:
            state["resize_frame_repaint_failure_count"] += 1
            state["last_resize_frame_repaint_error"] = (
                f"full_overlay:{type(exc).__name__}: {exc}"
            )
            return False

    @staticmethod
    def _queue_resize_frame_final_full_repaint(vte, state):
        """Invalidate the backing window once after resize becomes quiet."""

        errors = []

        try:
            window = vte.get_window()

            if window is not None:
                window.invalidate_rect(None, True)
        except Exception as exc:
            errors.append(
                f"window:{type(exc).__name__}: {exc}"
            )

        try:
            vte.queue_draw()
        except Exception as exc:
            errors.append(
                f"vte:{type(exc).__name__}: {exc}"
            )

        try:
            parent = vte.get_parent()

            if parent is not None:
                parent.queue_draw()
        except Exception as exc:
            errors.append(
                f"parent:{type(exc).__name__}: {exc}"
            )

        state["resize_frame_repaint_final_full_count"] += 1

        if errors:
            state["resize_frame_repaint_failure_count"] += 1
            state["last_resize_frame_repaint_error"] = "; ".join(errors)
        else:
            state["last_resize_frame_repaint_error"] = ""

    def _on_resize_frame_repaint_tick(
        self,
        vte,
        _frame_clock,
        key,
    ):
        """Repaint per frame, then finish with one settled full pass."""

        state = self.states.get(key)

        if state is None or state.get("vte") is not vte:
            return False

        if not state["active"]:
            state["resize_frame_repaint_tick_callback_id"] = None
            state["resize_frame_repaint_frames_since_allocation"] = 0
            state["resize_frame_repaint_skip_inactive_count"] += 1
            return False

        state["resize_frame_repaint_tick_count"] += 1
        state["resize_frame_repaint_frames_since_allocation"] += 1

        self._queue_resize_frame_full_overlay_damage(
            vte,
            state,
        )

        if RESIZE_EVENT_TRACE_ENABLED:
            self._record_resize_event_trace(
                vte,
                state,
                "frame_tick",
            )

        last_allocation_ns = int(
            state.get("resize_frame_repaint_last_allocation_ns", 0)
        )
        quiet_ms = max(
            (
                time.monotonic_ns() - last_allocation_ns
            ) / NANOSECONDS_PER_MILLISECOND,
            0.0,
        )
        state["resize_frame_repaint_last_quiet_ms"] = quiet_ms

        settled_attempt_count = int(
            state.get("resize_frame_repaint_settled_attempt_count", 0)
        )
        required_quiet_ms = (
            RESIZE_FRAME_REPAINT_QUIET_MS
            + settled_attempt_count
            * RESIZE_FRAME_REPAINT_RETRY_DELAY_MS
        )

        if (
            quiet_ms < required_quiet_ms
            or state["resize_frame_repaint_frames_since_allocation"]
            < RESIZE_FRAME_REPAINT_MIN_TICKS
        ):
            return True

        state["resize_frame_repaint_settled_attempt_count"] = (
            settled_attempt_count + 1
        )
        retry_needed = False

        try:
            self._rebuild_render_cache(
                vte,
                state,
                "resize_frame_clock_settled",
            )
            state["resize_frame_repaint_cache_refresh_count"] += 1

            if state.get("visual_coordinate_error", ""):
                state[
                    "resize_frame_repaint_coordinate_retry_failure_count"
                ] += 1
                retry_needed = True

        except Exception as exc:
            self._record_refresh_failure(
                state,
                "resize_frame_clock_settled",
                exc,
            )
            state["resize_frame_repaint_failure_count"] += 1
            state["last_resize_frame_repaint_error"] = (
                f"refresh:{type(exc).__name__}: {exc}"
            )
            retry_needed = True

        self._queue_resize_frame_final_full_repaint(
            vte,
            state,
        )

        if RESIZE_EVENT_TRACE_ENABLED:
            self._record_resize_event_trace(
                vte,
                state,
                "settled_full",
            )

        if (
            retry_needed
            and state["resize_frame_repaint_settled_attempt_count"]
            < RESIZE_FRAME_REPAINT_MAX_SETTLED_ATTEMPTS
        ):
            return True

        state["resize_frame_repaint_tick_callback_id"] = None

        return False

    @staticmethod
    def _queue_bottom_damage_full_repaint(vte, state):
        """Invalidate the backing window after incomplete bottom damage."""

        errors = []

        try:
            window = vte.get_window()

            if window is not None:
                window.invalidate_rect(None, True)
        except Exception as exc:
            errors.append(
                f"window:{type(exc).__name__}: {exc}"
            )

        try:
            vte.queue_draw()
        except Exception as exc:
            errors.append(
                f"vte:{type(exc).__name__}: {exc}"
            )

        try:
            parent = vte.get_parent()

            if parent is not None:
                parent.queue_draw()
        except Exception as exc:
            errors.append(
                f"parent:{type(exc).__name__}: {exc}"
            )

        state["bottom_damage_repaint_full_count"] += 1

        if errors:
            state["bottom_damage_repaint_failure_count"] += 1
            state["last_bottom_damage_repaint_error"] = (
                "; ".join(errors)
            )

    def _schedule_bottom_damage_repaint(self, vte, state):
        """Coalesce recovery after a draw excludes the lowest segment."""

        state["bottom_damage_repaint_request_count"] += 1

        if state.get("bottom_damage_repaint_source_id") is not None:
            state["bottom_damage_repaint_coalesced_count"] += 1
            return

        try:
            source_id = GLib.timeout_add(
                BOTTOM_DAMAGE_RECOVERY_DELAY_MS,
                self._flush_bottom_damage_repaint,
                id(vte),
            )

            if not source_id:
                raise RuntimeError(
                    "GLib.timeout_add returned no bottom-damage source"
                )

            state["bottom_damage_repaint_source_id"] = source_id
            state["bottom_damage_repaint_schedule_count"] += 1
        except Exception as exc:
            state["bottom_damage_repaint_source_id"] = None
            state["bottom_damage_repaint_failure_count"] += 1
            state["last_bottom_damage_repaint_error"] = (
                f"schedule:{type(exc).__name__}: {exc}"
            )

    def _flush_bottom_damage_repaint(self, key):
        """Perform one coalesced full repaint after incomplete damage."""

        state = self.states.get(key)

        if state is None:
            return False

        state["bottom_damage_repaint_source_id"] = None

        if not state["active"]:
            state["bottom_damage_repaint_skip_inactive_count"] += 1
            return False

        self._queue_bottom_damage_full_repaint(
            state["vte"],
            state,
        )

        if RESIZE_EVENT_TRACE_ENABLED:
            self._record_resize_event_trace(
                state["vte"],
                state,
                "bottom_recovery",
            )

        return False

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

        state["resize_reflow_continuity_snapshot"] = None
        self._invalidate_copy_snapshot(
            state,
            "scroll_adjustment",
        )
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
    def _cancel_toggle_repaint(state):
        """Cancel a pending second-pass repaint before a new toggle."""

        source_id = state.get(
            "toggle_repaint_source_id"
        )

        if source_id is None:
            return

        try:
            GLib.source_remove(source_id)
        except Exception:
            pass

        state["toggle_repaint_source_id"] = None
        state["toggle_repaint_cancel_count"] += 1

    @staticmethod
    def _queue_toggle_full_repaint(vte, state):
        """Invalidate the backing window and both windowless GTK widgets."""

        errors = []

        try:
            window = vte.get_window()

            if window is not None:
                window.invalidate_rect(None, True)
        except Exception as exc:
            errors.append(
                f"window:{type(exc).__name__}: {exc}"
            )

        try:
            vte.queue_draw()
        except Exception as exc:
            errors.append(
                f"vte:{type(exc).__name__}: {exc}"
            )

        try:
            parent = vte.get_parent()

            if parent is not None:
                parent.queue_draw()
        except Exception as exc:
            errors.append(
                f"parent:{type(exc).__name__}: {exc}"
            )

        state["toggle_repaint_full_count"] += 1

        if errors:
            state["toggle_repaint_failure_count"] += 1
            state["last_toggle_repaint_error"] = (
                "; ".join(errors)
            )

    def _schedule_toggle_repaint(self, vte, state):
        """Request an immediate and one bounded follow-up full repaint."""

        target_active = bool(state["active"])

        state["toggle_repaint_target_active"] = target_active
        state["toggle_repaint_request_count"] += 1

        if target_active:
            state["toggle_repaint_activation_request_count"] += 1
        else:
            state["toggle_repaint_deactivation_request_count"] += 1

        self._queue_toggle_full_repaint(
            vte,
            state,
        )

        try:
            state["toggle_repaint_source_id"] = (
                GLib.timeout_add(
                    TOGGLE_REPAINT_FOLLOW_UP_DELAY_MS,
                    self._flush_toggle_repaint,
                    id(vte),
                )
            )
        except Exception as exc:
            state["toggle_repaint_source_id"] = None
            state["toggle_repaint_failure_count"] += 1
            state["last_toggle_repaint_error"] = (
                f"schedule:{type(exc).__name__}: {exc}"
            )

    def _flush_toggle_repaint(self, key):
        """Perform the post-event-loop repaint for an unchanged state."""

        state = self.states.get(key)

        if state is None:
            return False

        state["toggle_repaint_source_id"] = None

        if bool(state["active"]) != bool(
            state.get("toggle_repaint_target_active", False)
        ):
            return False

        state["toggle_repaint_follow_up_count"] += 1

        self._queue_toggle_full_repaint(
            state["vte"],
            state,
        )

        return False

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

    def _shadow_compare_native_geometry(
        self,
        state,
        python_geometry,
    ):
        """Compare C geometry while always retaining Python output."""

        started_ns = time.perf_counter_ns()
        state["native_geometry_shadow_request_count"] += 1

        try:
            if not self._native_shadow_engine.available:
                state[
                    "native_geometry_shadow_skip_count"
                ] += 1
                state[
                    "native_geometry_shadow_last_status"
                ] = "unavailable"
                state[
                    "native_geometry_shadow_last_mismatch_fields"
                ] = ""
                state[
                    "native_geometry_shadow_last_error"
                ] = self._native_shadow_engine.last_error
                return

            native_geometry = (
                self._native_shadow_engine
                .calculate_geometry_shadow(
                    python_geometry
                )
            )

            mismatch_fields = [
                field_name
                for field_name in NATIVE_GEOMETRY_OUTPUT_FIELDS
                if int(python_geometry[field_name])
                != int(native_geometry[field_name])
            ]

            state[
                "native_geometry_shadow_last_error"
            ] = ""

            if mismatch_fields:
                state[
                    "native_geometry_shadow_mismatch_count"
                ] += 1
                state[
                    "native_geometry_shadow_last_status"
                ] = "mismatch"
                state[
                    "native_geometry_shadow_last_mismatch_fields"
                ] = ",".join(
                    (
                        f"{field_name}:"
                        f"{int(python_geometry[field_name])}!="
                        f"{int(native_geometry[field_name])}"
                    )
                    for field_name in mismatch_fields
                )
            else:
                state[
                    "native_geometry_shadow_match_count"
                ] += 1
                state[
                    "native_geometry_shadow_last_status"
                ] = "match"
                state[
                    "native_geometry_shadow_last_mismatch_fields"
                ] = ""

        except Exception as exc:
            state[
                "native_geometry_shadow_failure_count"
            ] += 1
            state[
                "native_geometry_shadow_last_status"
            ] = "error"
            state[
                "native_geometry_shadow_last_mismatch_fields"
            ] = ""
            state[
                "native_geometry_shadow_last_error"
            ] = f"{type(exc).__name__}: {exc}"

        finally:
            elapsed_ns = (
                time.perf_counter_ns()
                - started_ns
            )
            state[
                "native_geometry_shadow_last_ns"
            ] = elapsed_ns
            state[
                "native_geometry_shadow_total_ns"
            ] += elapsed_ns
            state[
                "native_geometry_shadow_max_ns"
            ] = max(
                state[
                    "native_geometry_shadow_max_ns"
                ],
                elapsed_ns,
            )

    def _shadow_compare_native_segments(
        self,
        vte,
        state,
        python_geometry,
        row_coordinate_offset,
        python_segments,
    ):
        """Compare C segment geometry while retaining Python output."""

        started_ns = time.perf_counter_ns()
        state["native_segment_shadow_request_count"] += 1
        overlay_clip = self._calculate_overlay_clip_rectangle(
            python_geometry
        )
        native_shadow_rows = (
            max(int(python_geometry["rows"]), 0)
            + VISIBLE_ROW_OVERSCAN_COUNT
        )
        state["native_segment_shadow_last_row_content_count"] = (
            native_shadow_rows + 1
        )
        state["native_segment_shadow_last_python_count"] = len(
            python_segments
        )
        state["native_segment_shadow_last_native_count"] = 0

        try:
            if not self._native_shadow_engine.available:
                state[
                    "native_segment_shadow_skip_count"
                ] += 1
                state[
                    "native_segment_shadow_last_status"
                ] = "unavailable"
                state[
                    "native_segment_shadow_last_mismatch_fields"
                ] = ""
                state[
                    "native_segment_shadow_last_error"
                ] = self._native_shadow_engine.last_error
                return

            # Both reads occur synchronously on GTK's main thread. No main-loop
            # callback can mutate the adjustment between Python extraction and
            # this shadow call.
            scroll_value = float(
                vte.get_vadjustment().get_value()
            )

            native_segments = (
                self._native_shadow_engine
                .calculate_segments_shadow(
                    python_geometry,
                    scroll_value,
                    row_coordinate_offset,
                    python_segments,
                    overlay_height=overlay_clip["height"],
                    overscan_rows=VISIBLE_ROW_OVERSCAN_COUNT,
                )
            )

            python_projection = [
                {
                    field_name: int(
                        python_segment[field_name]
                    )
                    for field_name in NATIVE_SEGMENT_FIELDS
                }
                for python_segment in python_segments
            ]

            state[
                "native_segment_shadow_last_native_count"
            ] = len(native_segments)

            mismatches = []

            if len(python_projection) != len(native_segments):
                mismatches.append(
                    "count:"
                    f"{len(python_projection)}!="
                    f"{len(native_segments)}"
                )

            for index, (
                python_segment,
                native_segment,
            ) in enumerate(
                zip(
                    python_projection,
                    native_segments,
                )
            ):
                for field_name in NATIVE_SEGMENT_FIELDS:
                    python_value = python_segment[field_name]
                    native_value = native_segment[field_name]

                    if python_value != native_value:
                        mismatches.append(
                            f"{index}.{field_name}:"
                            f"{python_value}!={native_value}"
                        )

            state[
                "native_segment_shadow_last_error"
            ] = ""

            if mismatches:
                state[
                    "native_segment_shadow_mismatch_count"
                ] += 1
                state[
                    "native_segment_shadow_last_status"
                ] = "mismatch"

                retained_mismatches = mismatches[:24]
                omitted_count = (
                    len(mismatches)
                    - len(retained_mismatches)
                )

                if omitted_count:
                    retained_mismatches.append(
                        f"...+{omitted_count}"
                    )

                state[
                    "native_segment_shadow_last_mismatch_fields"
                ] = ",".join(retained_mismatches)
            else:
                state[
                    "native_segment_shadow_match_count"
                ] += 1
                state[
                    "native_segment_shadow_last_status"
                ] = "match"
                state[
                    "native_segment_shadow_last_mismatch_fields"
                ] = ""

        except Exception as exc:
            state[
                "native_segment_shadow_failure_count"
            ] += 1
            state[
                "native_segment_shadow_last_status"
            ] = "error"
            state[
                "native_segment_shadow_last_mismatch_fields"
            ] = ""
            state[
                "native_segment_shadow_last_error"
            ] = f"{type(exc).__name__}: {exc}"

        finally:
            elapsed_ns = (
                time.perf_counter_ns()
                - started_ns
            )
            state[
                "native_segment_shadow_last_ns"
            ] = elapsed_ns
            state[
                "native_segment_shadow_total_ns"
            ] += elapsed_ns
            state[
                "native_segment_shadow_max_ns"
            ] = max(
                state[
                    "native_segment_shadow_max_ns"
                ],
                elapsed_ns,
            )

    def _shadow_compare_native_frame(
        self,
        vte,
        state,
        python_geometry,
        row_coordinate_offset,
        python_segments,
        row_content_frame=None,
    ):
        """Compare one batched C frame and return it only on an exact match."""

        started_ns = time.perf_counter_ns()
        matched_native_frame = None
        state["native_frame_shadow_request_count"] += 1
        expected_row_content_count = (
            max(int(python_geometry["rows"]), 0)
            + VISIBLE_ROW_OVERSCAN_COUNT
            + 1
        )
        state[
            "native_frame_shadow_last_row_content_count"
        ] = expected_row_content_count
        state[
            "native_frame_shadow_last_required_capacity"
        ] = 0
        state[
            "native_frame_shadow_last_python_segment_count"
        ] = len(python_segments)
        state[
            "native_frame_shadow_last_native_segment_count"
        ] = 0

        try:
            if not self._native_shadow_engine.available:
                state[
                    "native_frame_shadow_skip_count"
                ] += 1
                state[
                    "native_frame_shadow_last_status"
                ] = "unavailable"
                state[
                    "native_frame_shadow_last_mismatch_fields"
                ] = ""
                state[
                    "native_frame_shadow_last_error"
                ] = self._native_shadow_engine.last_error
                return

            if row_content_frame is None:
                # Preserve the v55 cache and legacy draw path. Both reads are
                # synchronous on GTK's main thread.
                scroll_value = float(
                    vte.get_vadjustment().get_value()
                )
                native_frame = (
                    self._native_shadow_engine
                    .calculate_frame_shadow(
                        python_geometry,
                        scroll_value,
                        row_coordinate_offset,
                        python_segments,
                        overscan_rows=VISIBLE_ROW_OVERSCAN_COUNT,
                    )
                )
            else:
                row_content_count = int(
                    row_content_frame[
                        "row_content_count"
                    ]
                )

                if row_content_count != expected_row_content_count:
                    raise RuntimeError(
                        "Captured row-content capacity changed before "
                        f"the native call: {row_content_count}!="
                        f"{expected_row_content_count}."
                    )

                scroll_value = float(
                    row_content_frame["scroll_value"]
                )
                native_frame = (
                    self._native_shadow_engine
                    .calculate_frame_from_row_contents(
                        python_geometry,
                        scroll_value,
                        row_coordinate_offset,
                        row_content_frame["row_contents"],
                        overscan_rows=VISIBLE_ROW_OVERSCAN_COUNT,
                    )
                )
            native_geometry = native_frame["geometry"]
            native_segments = native_frame["segments"]
            python_projection = [
                {
                    field_name: int(
                        python_segment[field_name]
                    )
                    for field_name in NATIVE_SEGMENT_FIELDS
                }
                for python_segment in python_segments
            ]
            state[
                "native_frame_shadow_last_row_content_count"
            ] = int(native_frame["row_content_count"])
            state[
                "native_frame_shadow_last_required_capacity"
            ] = int(
                native_frame["required_segment_capacity"]
            )
            state[
                "native_frame_shadow_last_native_segment_count"
            ] = len(native_segments)
            mismatches = []

            for field_name in NATIVE_GEOMETRY_OUTPUT_FIELDS:
                python_value = int(
                    python_geometry[field_name]
                )
                native_value = int(
                    native_geometry[field_name]
                )

                if python_value != native_value:
                    mismatches.append(
                        f"geometry.{field_name}:"
                        f"{python_value}!={native_value}"
                    )

            if len(python_projection) != len(native_segments):
                mismatches.append(
                    "segment.count:"
                    f"{len(python_projection)}!="
                    f"{len(native_segments)}"
                )

            for index, (
                python_segment,
                native_segment,
            ) in enumerate(
                zip(
                    python_projection,
                    native_segments,
                )
            ):
                for field_name in NATIVE_SEGMENT_FIELDS:
                    python_value = python_segment[field_name]
                    native_value = native_segment[field_name]

                    if python_value != native_value:
                        mismatches.append(
                            f"segment.{index}.{field_name}:"
                            f"{python_value}!={native_value}"
                        )

            state[
                "native_frame_shadow_last_error"
            ] = ""

            if mismatches:
                state[
                    "native_frame_shadow_mismatch_count"
                ] += 1
                state[
                    "native_frame_shadow_last_status"
                ] = "mismatch"
                retained_mismatches = mismatches[:24]
                omitted_count = (
                    len(mismatches)
                    - len(retained_mismatches)
                )

                if omitted_count:
                    retained_mismatches.append(
                        f"...+{omitted_count}"
                    )

                state[
                    "native_frame_shadow_last_mismatch_fields"
                ] = ",".join(retained_mismatches)
            else:
                state[
                    "native_frame_shadow_match_count"
                ] += 1
                state[
                    "native_frame_shadow_last_status"
                ] = "match"
                state[
                    "native_frame_shadow_last_mismatch_fields"
                ] = ""
                matched_native_frame = native_frame

        except Exception as exc:
            state[
                "native_frame_shadow_failure_count"
            ] += 1
            state[
                "native_frame_shadow_last_status"
            ] = "error"
            state[
                "native_frame_shadow_last_mismatch_fields"
            ] = ""
            state[
                "native_frame_shadow_last_error"
            ] = f"{type(exc).__name__}: {exc}"

        finally:
            elapsed_ns = (
                time.perf_counter_ns()
                - started_ns
            )
            state[
                "native_frame_shadow_last_ns"
            ] = elapsed_ns
            state[
                "native_frame_shadow_total_ns"
            ] += elapsed_ns
            state[
                "native_frame_shadow_max_ns"
            ] = max(
                state[
                    "native_frame_shadow_max_ns"
                ],
                elapsed_ns,
            )

        return matched_native_frame

    @staticmethod
    def _apply_native_frame_authority(
        python_geometry,
        python_segments,
        native_frame,
    ):
        """Graft Python row text onto exactly matched native frame fields."""

        native_geometry = native_frame.get("geometry")
        native_segments = native_frame.get("segments")

        if not isinstance(native_geometry, dict):
            raise RuntimeError(
                "Native frame geometry is not a dictionary."
            )

        if not isinstance(native_segments, list):
            raise RuntimeError(
                "Native frame segments are not a list."
            )

        if len(native_segments) != len(python_segments):
            raise RuntimeError(
                "Native authority segment count changed after "
                f"comparison: {len(native_segments)}!="
                f"{len(python_segments)}."
            )

        authoritative_geometry = dict(python_geometry)

        for field_name in NATIVE_GEOMETRY_OUTPUT_FIELDS:
            native_value = int(native_geometry[field_name])

            if native_value != int(python_geometry[field_name]):
                raise RuntimeError(
                    "Native authority geometry changed after "
                    f"comparison at {field_name}."
                )

            authoritative_geometry[field_name] = native_value

        authoritative_segments = []
        seen_display_rows = set()

        for index, (python_segment, native_segment) in enumerate(
            zip(python_segments, native_segments)
        ):
            authoritative_segment = dict(python_segment)

            for field_name in NATIVE_SEGMENT_FIELDS:
                native_value = int(native_segment[field_name])

                if native_value != int(python_segment[field_name]):
                    raise RuntimeError(
                        "Native authority segment changed after "
                        f"comparison at {index}.{field_name}."
                    )

                authoritative_segment[field_name] = native_value

            display_row = int(
                authoritative_segment["display_row"]
            )

            if display_row in seen_display_rows:
                raise RuntimeError(
                    "Native authority emitted a duplicate display "
                    f"row: {display_row}."
                )

            if "row_text" not in authoritative_segment:
                raise RuntimeError(
                    "Python row text was unavailable for native "
                    f"authority segment {index}."
                )

            seen_display_rows.add(display_row)
            authoritative_segments.append(
                authoritative_segment
            )

        return (
            authoritative_geometry,
            authoritative_segments,
        )

    def _select_native_frame_authority(
        self,
        state,
        python_geometry,
        python_segments,
        matched_native_frame,
    ):
        """Select native output or return the same-frame Python fallback."""

        state["native_frame_authority_last_applied"] = False

        if not NATIVE_FRAME_AUTHORITY_ENABLED:
            return python_geometry, python_segments

        state["native_frame_authority_attempt_count"] += 1

        if state.get("native_frame_authority_latched_off", False):
            state[
                "native_frame_authority_latched_skip_count"
            ] += 1
            return python_geometry, python_segments

        if matched_native_frame is None:
            fallback_reason = str(
                state.get(
                    "native_frame_shadow_last_status",
                    "unavailable",
                )
            )
            state["native_frame_authority_fallback_count"] += 1
            state["native_frame_authority_latch_count"] += 1
            state["native_frame_authority_latched_off"] = True
            state[
                "native_frame_authority_last_fallback_reason"
            ] = fallback_reason
            state["native_frame_authority_last_error"] = str(
                state.get(
                    "native_frame_shadow_last_error",
                    "",
                )
            )
            return python_geometry, python_segments

        try:
            (
                authoritative_geometry,
                authoritative_segments,
            ) = self._apply_native_frame_authority(
                python_geometry,
                python_segments,
                matched_native_frame,
            )
        except Exception as exc:
            state["native_frame_authority_fallback_count"] += 1
            state["native_frame_authority_latch_count"] += 1
            state["native_frame_authority_latched_off"] = True
            state[
                "native_frame_authority_last_fallback_reason"
            ] = "conversion_error"
            state[
                "native_frame_authority_last_error"
            ] = f"{type(exc).__name__}: {exc}"
            return python_geometry, python_segments

        state["native_frame_authority_applied_count"] += 1
        state["native_frame_authority_last_applied"] = True
        state[
            "native_frame_authority_last_fallback_reason"
        ] = ""
        state["native_frame_authority_last_error"] = ""

        return (
            authoritative_geometry,
            authoritative_segments,
        )

    def _shadow_compare_native_draw_frame(
        self,
        vte,
        state,
        python_geometry,
        row_coordinate_offset,
        python_segments,
        row_content_frame=None,
    ):
        """Compare one real draw frame without changing its Python output."""

        started_ns = time.perf_counter_ns()
        direct_row_content = row_content_frame is not None
        state["native_draw_frame_shadow_request_count"] += 1
        state[
            "native_draw_frame_shadow_last_python_segment_count"
        ] = len(python_segments)
        state[
            "native_draw_frame_shadow_last_native_segment_count"
        ] = 0

        if direct_row_content:
            state["native_row_content_frame_request_count"] += 1
            state[
                "native_row_content_frame_last_row_content_count"
            ] = int(
                row_content_frame["row_content_count"]
            )
            state[
                "native_row_content_frame_last_python_segment_count"
            ] = len(python_segments)
            state[
                "native_row_content_frame_last_native_segment_count"
            ] = 0

        try:
            matched_native_frame = self._shadow_compare_native_frame(
                vte,
                state,
                python_geometry,
                row_coordinate_offset,
                python_segments,
                row_content_frame=row_content_frame,
            )
            status = str(
                state.get(
                    "native_frame_shadow_last_status",
                    "error",
                )
            )
            state[
                "native_draw_frame_shadow_last_native_segment_count"
            ] = int(
                state.get(
                    "native_frame_shadow_last_native_segment_count",
                    0,
                )
            )
            state[
                "native_draw_frame_shadow_last_mismatch_fields"
            ] = str(
                state.get(
                    "native_frame_shadow_last_mismatch_fields",
                    "",
                )
            )
            state[
                "native_draw_frame_shadow_last_error"
            ] = str(
                state.get(
                    "native_frame_shadow_last_error",
                    "",
                )
            )

            if direct_row_content:
                state[
                    "native_row_content_frame_last_native_segment_count"
                ] = int(
                    state.get(
                        "native_frame_shadow_last_native_segment_count",
                        0,
                    )
                )
                state[
                    "native_row_content_frame_last_error"
                ] = str(
                    state.get(
                        "native_frame_shadow_last_error",
                        "",
                    )
                )

            if matched_native_frame is not None:
                state[
                    "native_draw_frame_shadow_match_count"
                ] += 1
                state[
                    "native_draw_frame_shadow_last_status"
                ] = "match"

                if direct_row_content:
                    state[
                        "native_row_content_frame_match_count"
                    ] += 1
                    state[
                        "native_row_content_frame_last_status"
                    ] = "match"
            elif status == "mismatch":
                state[
                    "native_draw_frame_shadow_mismatch_count"
                ] += 1
                state[
                    "native_draw_frame_shadow_last_status"
                ] = "mismatch"

                if direct_row_content:
                    state[
                        "native_row_content_frame_mismatch_count"
                    ] += 1
                    state[
                        "native_row_content_frame_last_status"
                    ] = "mismatch"
            elif status == "error":
                state[
                    "native_draw_frame_shadow_failure_count"
                ] += 1
                state[
                    "native_draw_frame_shadow_last_status"
                ] = "error"

                if direct_row_content:
                    state[
                        "native_row_content_frame_failure_count"
                    ] += 1
                    state[
                        "native_row_content_frame_last_status"
                    ] = "error"
            else:
                state[
                    "native_draw_frame_shadow_skip_count"
                ] += 1
                state[
                    "native_draw_frame_shadow_last_status"
                ] = status

                if direct_row_content:
                    state[
                        "native_row_content_frame_skip_count"
                    ] += 1
                    state[
                        "native_row_content_frame_last_status"
                    ] = status

            if (
                direct_row_content
                and matched_native_frame is None
                and not state.get(
                    "native_row_content_frame_latched_off",
                    False,
                )
            ):
                state[
                    "native_row_content_frame_fallback_count"
                ] += 1
                state[
                    "native_row_content_frame_latch_count"
                ] += 1
                state[
                    "native_row_content_frame_latched_off"
                ] = True

            return matched_native_frame

        except Exception as exc:
            state[
                "native_draw_frame_shadow_failure_count"
            ] += 1
            state[
                "native_draw_frame_shadow_last_status"
            ] = "error"
            state[
                "native_draw_frame_shadow_last_mismatch_fields"
            ] = ""
            state[
                "native_draw_frame_shadow_last_error"
            ] = f"{type(exc).__name__}: {exc}"

            if direct_row_content:
                state[
                    "native_row_content_frame_failure_count"
                ] += 1
                state[
                    "native_row_content_frame_last_status"
                ] = "error"
                state[
                    "native_row_content_frame_last_error"
                ] = f"{type(exc).__name__}: {exc}"
                state[
                    "native_row_content_frame_fallback_count"
                ] += 1

                if not state.get(
                    "native_row_content_frame_latched_off",
                    False,
                ):
                    state[
                        "native_row_content_frame_latch_count"
                    ] += 1
                    state[
                        "native_row_content_frame_latched_off"
                    ] = True
            return None

        finally:
            elapsed_ns = time.perf_counter_ns() - started_ns
            state[
                "native_draw_frame_shadow_last_ns"
            ] = elapsed_ns
            state[
                "native_draw_frame_shadow_total_ns"
            ] += elapsed_ns
            state[
                "native_draw_frame_shadow_max_ns"
            ] = max(
                state[
                    "native_draw_frame_shadow_max_ns"
                ],
                elapsed_ns,
            )

    def _select_native_draw_frame_authority(
        self,
        state,
        python_geometry,
        python_segments,
        matched_native_frame,
    ):
        """Select a matched native draw frame or same-frame Python fallback."""

        state["native_draw_frame_authority_last_applied"] = False

        if not NATIVE_DRAW_FRAME_AUTHORITY_ENABLED:
            return python_geometry, python_segments

        state["native_draw_frame_authority_attempt_count"] += 1

        if state.get(
            "native_draw_frame_authority_latched_off",
            False,
        ):
            state[
                "native_draw_frame_authority_latched_skip_count"
            ] += 1
            return python_geometry, python_segments

        if matched_native_frame is None:
            fallback_reason = str(
                state.get(
                    "native_draw_frame_shadow_last_status",
                    "unavailable",
                )
            )
            state[
                "native_draw_frame_authority_fallback_count"
            ] += 1
            state[
                "native_draw_frame_authority_latch_count"
            ] += 1
            state[
                "native_draw_frame_authority_latched_off"
            ] = True
            state[
                "native_draw_frame_authority_last_fallback_reason"
            ] = fallback_reason
            state[
                "native_draw_frame_authority_last_error"
            ] = str(
                state.get(
                    "native_draw_frame_shadow_last_error",
                    "",
                )
            )
            return python_geometry, python_segments

        try:
            (
                authoritative_geometry,
                authoritative_segments,
            ) = self._apply_native_frame_authority(
                python_geometry,
                python_segments,
                matched_native_frame,
            )
        except Exception as exc:
            state[
                "native_draw_frame_authority_fallback_count"
            ] += 1
            state[
                "native_draw_frame_authority_latch_count"
            ] += 1
            state[
                "native_draw_frame_authority_latched_off"
            ] = True
            state[
                "native_draw_frame_authority_last_fallback_reason"
            ] = "conversion_error"
            state[
                "native_draw_frame_authority_last_error"
            ] = f"{type(exc).__name__}: {exc}"
            return python_geometry, python_segments

        state[
            "native_draw_frame_authority_applied_count"
        ] += 1
        state[
            "native_draw_frame_authority_last_applied"
        ] = True
        state[
            "native_draw_frame_authority_last_fallback_reason"
        ] = ""
        state[
            "native_draw_frame_authority_last_error"
        ] = ""

        return (
            authoritative_geometry,
            authoritative_segments,
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

        # Validation-only: compare geometry and segments through one batched
        # FFI call. The separately gated authority canary uses only an exact
        # native match, while retaining Python row text and a same-frame
        # fallback. Production avoids reconstruction and ctypes marshalling.
        matched_native_frame = None

        if NATIVE_SHADOW_VALIDATION_ENABLED:
            matched_native_frame = self._shadow_compare_native_frame(
                vte,
                state,
                geometry,
                visual_offset,
                row_segments,
            )

        (
            geometry,
            row_segments,
        ) = self._select_native_frame_authority(
            state,
            geometry,
            row_segments,
            matched_native_frame,
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

    @classmethod
    def _record_draw_damage_clip(
        cls,
        cairo_context,
        state,
        row_segments,
        resize_pending,
        geometry,
    ):
        """Classify damage across the visible area of the lowest band."""

        state["draw_damage_clip_probe_count"] += 1

        if resize_pending:
            state["resize_draw_count"] += 1

        try:
            overlay_clip = cls._calculate_overlay_clip_rectangle(
                geometry
            )
            clip_x1, clip_y1, clip_x2, clip_y2 = (
                cairo_context.clip_extents()
            )

            state["last_draw_clip_x1"] = float(clip_x1)
            state["last_draw_clip_y1"] = float(clip_y1)
            state["last_draw_clip_x2"] = float(clip_x2)
            state["last_draw_clip_y2"] = float(clip_y2)
            state["last_draw_clip_resize_pending"] = bool(
                resize_pending
            )

            if not row_segments:
                state["bottom_probe_no_segment_count"] += 1
                state["last_bottom_probe_absolute_row"] = -1
                state["last_bottom_probe_display_row"] = -1
                state["last_bottom_probe_points_in_clip"] = 0
                state["last_bottom_probe_top_points_in_clip"] = 0
                state["last_bottom_probe_middle_points_in_clip"] = 0
                state["last_bottom_probe_bottom_points_in_clip"] = 0
                state["last_draw_damage_clip_error"] = ""
                return None

            bottom_segment = max(
                row_segments,
                key=lambda segment: (
                    int(segment["y"])
                    + int(segment["height"]),
                    int(segment["display_row"]),
                ),
            )

            segment_x = int(bottom_segment["x"])
            segment_y = int(bottom_segment["y"])
            segment_width = max(
                int(bottom_segment["width"]),
                1,
            )
            segment_height = max(
                int(bottom_segment["height"]),
                1,
            )

            visible_x1 = max(
                float(segment_x),
                float(overlay_clip["x"]),
            )
            visible_y1 = max(
                float(segment_y),
                float(overlay_clip["y"]),
            )
            visible_x2 = min(
                float(segment_x + segment_width),
                float(
                    overlay_clip["x"]
                    + overlay_clip["width"]
                ),
            )
            visible_y2 = min(
                float(segment_y + segment_height),
                float(
                    overlay_clip["y"]
                    + overlay_clip["height"]
                ),
            )

            if (
                visible_x2 <= visible_x1
                or visible_y2 <= visible_y1
            ):
                state["bottom_probe_no_segment_count"] += 1
                state["last_bottom_probe_points_in_clip"] = 0
                state["last_bottom_probe_top_points_in_clip"] = 0
                state["last_bottom_probe_middle_points_in_clip"] = 0
                state["last_bottom_probe_bottom_points_in_clip"] = 0
                state["last_draw_damage_clip_error"] = ""
                return None

            visible_width = visible_x2 - visible_x1
            visible_height = visible_y2 - visible_y1
            sample_x_inset = min(
                0.5,
                visible_width / 2.0,
            )
            sample_y_inset = min(
                0.5,
                visible_height / 2.0,
            )
            sample_x_values = (
                visible_x1 + sample_x_inset,
                visible_x1 + visible_width / 2.0,
                visible_x2 - sample_x_inset,
            )
            sample_y_values = (
                visible_y1 + sample_y_inset,
                visible_y1 + visible_height / 2.0,
                visible_y2 - sample_y_inset,
            )
            row_point_counts = tuple(
                sum(
                    1
                    for sample_x in sample_x_values
                    if cairo_context.in_clip(
                        sample_x,
                        sample_y,
                    )
                )
                for sample_y in sample_y_values
            )
            points_in_clip = sum(row_point_counts)

            state["last_bottom_probe_absolute_row"] = int(
                bottom_segment["absolute_row"]
            )
            state["last_bottom_probe_display_row"] = int(
                bottom_segment["display_row"]
            )
            state["last_bottom_probe_x"] = segment_x
            state["last_bottom_probe_y"] = segment_y
            state["last_bottom_probe_width"] = segment_width
            state["last_bottom_probe_height"] = segment_height
            state["last_bottom_probe_visible_x1"] = visible_x1
            state["last_bottom_probe_visible_y1"] = visible_y1
            state["last_bottom_probe_visible_x2"] = visible_x2
            state["last_bottom_probe_visible_y2"] = visible_y2
            state["last_bottom_probe_expected_point_count"] = (
                BOTTOM_DAMAGE_PROBE_POINT_COUNT
            )
            state["last_bottom_probe_points_in_clip"] = (
                points_in_clip
            )
            (
                state["last_bottom_probe_top_points_in_clip"],
                state["last_bottom_probe_middle_points_in_clip"],
                state["last_bottom_probe_bottom_points_in_clip"],
            ) = row_point_counts
            state["last_draw_damage_clip_error"] = ""

            if points_in_clip == BOTTOM_DAMAGE_PROBE_POINT_COUNT:
                state["bottom_probe_full_clip_count"] += 1

                if resize_pending:
                    state["resize_bottom_full_clip_count"] += 1
                    state["resize_bottom_incomplete_streak"] = 0

            elif points_in_clip == 0:
                state["bottom_probe_excluded_count"] += 1

                if resize_pending:
                    state["resize_bottom_excluded_count"] += 1

            else:
                state["bottom_probe_partial_clip_count"] += 1

                if resize_pending:
                    state["resize_bottom_partial_clip_count"] += 1

            if (
                resize_pending
                and points_in_clip < BOTTOM_DAMAGE_PROBE_POINT_COUNT
            ):
                state["resize_bottom_incomplete_streak"] += 1
                state["resize_bottom_incomplete_max_streak"] = max(
                    state["resize_bottom_incomplete_max_streak"],
                    state["resize_bottom_incomplete_streak"],
                )

            return points_in_clip

        except Exception as exc:
            state["draw_damage_clip_probe_failure_count"] += 1
            state["last_draw_damage_clip_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            return None

    @staticmethod
    def _clip_extents_cover_rectangle(
        clip_extents,
        rectangle,
    ):
        """Return whether clip extents contain the bounded rectangle."""

        clip_x1, clip_y1, clip_x2, clip_y2 = clip_extents
        target_x = int(rectangle["x"])
        target_y = int(rectangle["y"])
        target_width = max(int(rectangle["width"]), 0)
        target_height = max(int(rectangle["height"]), 0)
        epsilon = RESIZE_DRAW_CLIP_EXPANSION_EPSILON

        return (
            target_width > 0
            and target_height > 0
            and float(clip_x1) <= target_x + epsilon
            and float(clip_y1) <= target_y + epsilon
            and float(clip_x2) >= target_x + target_width - epsilon
            and float(clip_y2) >= target_y + target_height - epsilon
        )

    @classmethod
    def _expand_incomplete_resize_draw_clip(
        cls,
        cairo_context,
        state,
        overlay_clip,
        resize_pending,
    ):
        """Expand one incomplete resize draw, bounded by the caller."""

        if not resize_pending:
            return False

        state["resize_draw_clip_expansion_check_count"] = (
            state.get("resize_draw_clip_expansion_check_count", 0)
            + 1
        )

        try:
            clip_x1, clip_y1, clip_x2, clip_y2 = (
                cairo_context.clip_extents()
            )
            target_x = int(overlay_clip["x"])
            target_y = int(overlay_clip["y"])
            target_width = max(
                int(overlay_clip["width"]),
                0,
            )
            target_height = max(
                int(overlay_clip["height"]),
                0,
            )
            state["last_resize_draw_clip_target_x"] = target_x
            state["last_resize_draw_clip_target_y"] = target_y
            state["last_resize_draw_clip_target_width"] = (
                target_width
            )
            state["last_resize_draw_clip_target_height"] = (
                target_height
            )

            if cls._clip_extents_cover_rectangle(
                (clip_x1, clip_y1, clip_x2, clip_y2),
                overlay_clip,
            ):
                state[
                    "resize_draw_clip_expansion_complete_skip_count"
                ] = state.get(
                    "resize_draw_clip_expansion_complete_skip_count",
                    0,
                ) + 1
                state[
                    "last_resize_draw_clip_expansion_effective"
                ] = False
                state["last_resize_draw_clip_expansion_error"] = ""
                return False

            state["resize_draw_clip_expansion_request_count"] = (
                state.get(
                    "resize_draw_clip_expansion_request_count",
                    0,
                )
                + 1
            )

            cairo_context.reset_clip()

            (
                expanded_x1,
                expanded_y1,
                expanded_x2,
                expanded_y2,
            ) = cairo_context.clip_extents()
            state["resize_draw_clip_expansion_applied_count"] = (
                state.get(
                    "resize_draw_clip_expansion_applied_count",
                    0,
                )
                + 1
            )
            effective = cls._clip_extents_cover_rectangle(
                (
                    expanded_x1,
                    expanded_y1,
                    expanded_x2,
                    expanded_y2,
                ),
                overlay_clip,
            )

            if effective:
                state[
                    "resize_draw_clip_expansion_effective_count"
                ] = state.get(
                    "resize_draw_clip_expansion_effective_count",
                    0,
                ) + 1
            else:
                state[
                    "resize_draw_clip_expansion_ineffective_count"
                ] = state.get(
                    "resize_draw_clip_expansion_ineffective_count",
                    0,
                ) + 1

            state["last_resize_draw_clip_expansion_effective"] = (
                effective
            )
            state["last_resize_draw_clip_expansion_x1"] = float(
                expanded_x1
            )
            state["last_resize_draw_clip_expansion_y1"] = float(
                expanded_y1
            )
            state["last_resize_draw_clip_expansion_x2"] = float(
                expanded_x2
            )
            state["last_resize_draw_clip_expansion_y2"] = float(
                expanded_y2
            )
            state["last_resize_draw_clip_expansion_error"] = ""
            return True

        except Exception as exc:
            state["resize_draw_clip_expansion_failure_count"] = (
                state.get(
                    "resize_draw_clip_expansion_failure_count",
                    0,
                )
                + 1
            )
            state["last_resize_draw_clip_expansion_effective"] = (
                False
            )
            state["last_resize_draw_clip_expansion_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            return False

    @staticmethod
    def _resize_reflow_geometry_signature(geometry):
        """Return the bounded fields which can reposition terminal rows."""

        return tuple(
            int(geometry.get(field_name, 0))
            for field_name in (
                "allocated_width",
                "allocated_height",
                "columns",
                "rows",
                "character_width",
                "character_height",
                "x",
                "y",
                "width",
                "height",
            )
        )

    @staticmethod
    def _retain_resize_reflow_tail_segments(segments, tail_floor):
        """Copy only segments inside the bounded trailing row window."""

        return [
            dict(segment)
            for segment in segments
            if int(segment.get("display_row", -1)) >= int(tail_floor)
        ]

    @classmethod
    def _store_resize_reflow_continuity_snapshot(
        cls,
        state,
        geometry,
        row_segments,
        ordinary_segments,
        command_segments,
        line_number_segments,
        row_coordinate_offset,
        timestamp_ns,
    ):
        """Retain only one bounded tail partition for the following draw."""

        lowest_display_row = max(
            (
                int(segment["display_row"])
                for segment in row_segments
            ),
            default=-1,
        )
        tail_floor = max(
            lowest_display_row
            - RESIZE_REFLOW_CONTINUITY_TAIL_ROWS
            + 1,
            0,
        )
        character_height = max(
            int(geometry.get("character_height", 0)),
            0,
        )

        if row_segments and character_height > 0:
            first_segment = min(
                row_segments,
                key=lambda segment: int(segment["display_row"]),
            )
            row_y_offset = (
                int(first_segment["y"])
                - int(geometry.get("y", 0))
                - int(first_segment["display_row"])
                * character_height
            )
        else:
            row_y_offset = 0

        state["resize_reflow_continuity_snapshot"] = {
            "timestamp_ns": int(timestamp_ns),
            "geometry": {
                field_name: int(geometry.get(field_name, 0))
                for field_name in (
                    "allocated_width",
                    "allocated_height",
                    "columns",
                    "rows",
                    "character_width",
                    "character_height",
                    "x",
                    "y",
                    "width",
                    "height",
                )
            },
            "geometry_signature": (
                cls._resize_reflow_geometry_signature(geometry)
            ),
            "summary": (
                len(row_segments),
                lowest_display_row,
            ),
            "row_coordinate_offset": int(row_coordinate_offset),
            "row_y_offset": int(row_y_offset),
            "tail_floor": int(tail_floor),
            "row_segments": cls._retain_resize_reflow_tail_segments(
                row_segments,
                tail_floor,
            ),
            "ordinary_segments": cls._retain_resize_reflow_tail_segments(
                ordinary_segments,
                tail_floor,
            ),
            "command_segments": cls._retain_resize_reflow_tail_segments(
                command_segments,
                tail_floor,
            ),
            "line_number_segments": (
                cls._retain_resize_reflow_tail_segments(
                    line_number_segments,
                    tail_floor,
                )
            ),
        }

    @classmethod
    def _calculate_resize_reflow_continuity_segments(
        cls,
        state,
        geometry,
        overlay_clip,
        row_segments,
        ordinary_segments,
        command_segments,
        line_number_segments,
        row_coordinate_offset,
        resize_pending,
        timestamp_ns,
    ):
        """Project one stale resize tail, excluding current coverage."""

        empty = ([], [], [])
        state["last_resize_reflow_continuity_error"] = ""

        if not resize_pending:
            state[
                "resize_reflow_continuity_non_resize_skip_count"
            ] += 1
            state["last_resize_reflow_continuity_output_count"] = 0
            return empty

        state["resize_reflow_continuity_check_count"] += 1
        previous = state.get(
            "resize_reflow_continuity_snapshot"
        )

        if not previous:
            state[
                "resize_reflow_continuity_no_snapshot_skip_count"
            ] += 1
            state["last_resize_reflow_continuity_output_count"] = 0
            return empty

        try:
            previous_geometry = previous["geometry"]
            previous_columns = int(
                previous_geometry.get("columns", 0)
            )
            previous_rows = int(
                previous_geometry.get("rows", 0)
            )
            current_columns = int(geometry.get("columns", 0))
            current_rows = int(geometry.get("rows", 0))
            current_lowest_row = max(
                (
                    int(segment["display_row"])
                    for segment in row_segments
                ),
                default=-1,
            )
            previous_summary = tuple(previous["summary"])
            current_summary = (
                len(row_segments),
                current_lowest_row,
            )
            age_ms = max(
                (
                    int(timestamp_ns)
                    - int(previous["timestamp_ns"])
                )
                / NANOSECONDS_PER_MILLISECOND,
                0.0,
            )

            state["last_resize_reflow_continuity_age_ms"] = age_ms
            state[
                "last_resize_reflow_continuity_previous_columns"
            ] = previous_columns
            state[
                "last_resize_reflow_continuity_previous_rows"
            ] = previous_rows
            state[
                "last_resize_reflow_continuity_current_columns"
            ] = current_columns
            state[
                "last_resize_reflow_continuity_current_rows"
            ] = current_rows
            state[
                "last_resize_reflow_continuity_previous_segment_count"
            ] = int(previous_summary[0])
            state[
                "last_resize_reflow_continuity_previous_lowest_row"
            ] = int(previous_summary[1])
            state[
                "last_resize_reflow_continuity_current_segment_count"
            ] = int(current_summary[0])
            state[
                "last_resize_reflow_continuity_current_lowest_row"
            ] = int(current_summary[1])

            if age_ms > RESIZE_REFLOW_CONTINUITY_MAX_AGE_MS:
                state[
                    "resize_reflow_continuity_age_skip_count"
                ] += 1
                state[
                    "last_resize_reflow_continuity_output_count"
                ] = 0
                return empty

            if (
                previous.get("geometry_signature")
                == cls._resize_reflow_geometry_signature(geometry)
            ):
                state[
                    "resize_reflow_continuity_stable_geometry_skip_count"
                ] += 1
                state[
                    "last_resize_reflow_continuity_output_count"
                ] = 0
                return empty

            if (
                int(previous.get("row_coordinate_offset", 0))
                != int(row_coordinate_offset)
            ):
                state[
                    "resize_reflow_continuity_coordinate_skip_count"
                ] += 1
                state[
                    "last_resize_reflow_continuity_output_count"
                ] = 0
                return empty

            if previous_summary != current_summary:
                state[
                    "resize_reflow_continuity_summary_changed_skip_count"
                ] += 1
                state[
                    "last_resize_reflow_continuity_output_count"
                ] = 0
                return empty

            previous_character_width = int(
                previous_geometry.get("character_width", 0)
            )
            previous_character_height = int(
                previous_geometry.get("character_height", 0)
            )
            character_width = int(
                geometry.get("character_width", 0)
            )
            character_height = int(
                geometry.get("character_height", 0)
            )

            if (
                current_columns <= 0
                or current_rows <= 0
                or character_width <= 0
                or character_height <= 0
                or character_width != previous_character_width
                or character_height != previous_character_height
            ):
                state[
                    "resize_reflow_continuity_no_shift_skip_count"
                ] += 1
                state[
                    "last_resize_reflow_continuity_output_count"
                ] = 0
                return empty

            row_delta = current_rows - previous_rows
            column_delta = current_columns - previous_columns

            if row_delta:
                shift_rows = (
                    RESIZE_REFLOW_CONTINUITY_SHIFT_ROWS
                    if row_delta > 0
                    else -RESIZE_REFLOW_CONTINUITY_SHIFT_ROWS
                )
            elif column_delta:
                shift_rows = (
                    -RESIZE_REFLOW_CONTINUITY_SHIFT_ROWS
                    if column_delta > 0
                    else RESIZE_REFLOW_CONTINUITY_SHIFT_ROWS
                )
                wrap_edge_column = max(
                    min(previous_columns, current_columns) - 1,
                    0,
                )
                wrap_edge_active = any(
                    int(segment.get("end_column", 0))
                    >= wrap_edge_column
                    for segment in previous.get(
                        "row_segments",
                        (),
                    )
                )

                if not wrap_edge_active:
                    state[
                        "resize_reflow_continuity_no_wrap_edge_skip_count"
                    ] += 1
                    state[
                        "last_resize_reflow_continuity_output_count"
                    ] = 0
                    return empty
            else:
                state[
                    "resize_reflow_continuity_no_shift_skip_count"
                ] += 1
                state[
                    "last_resize_reflow_continuity_output_count"
                ] = 0
                return empty

            state["resize_reflow_continuity_candidate_count"] += 1
            state[
                "last_resize_reflow_continuity_shift_rows"
            ] = shift_rows

            current_coverage = {}

            for segment in (
                list(ordinary_segments)
                + list(command_segments)
                + list(line_number_segments)
            ):
                display_row = int(segment["display_row"])
                current_coverage.setdefault(
                    display_row,
                    [],
                ).append(
                    (
                        int(segment["start_column"]),
                        int(segment["end_column"]),
                    )
                )

            current_coverage = {
                display_row: cls._merge_column_ranges(ranges)
                for display_row, ranges in current_coverage.items()
            }
            generated_coverage = {}
            bridge = {
                "ordinary": [],
                "command": [],
                "line_number": [],
            }
            maximum_display_row = (
                current_rows
                + VISIBLE_ROW_OVERSCAN_COUNT
                - 1
            )
            grid_y = int(geometry.get("y", 0))
            row_y_offset = int(
                previous.get("row_y_offset", 0)
            )

            if row_segments:
                first_segment = min(
                    row_segments,
                    key=lambda segment: int(
                        segment["display_row"]
                    ),
                )
                row_y_offset = (
                    int(first_segment["y"])
                    - grid_y
                    - int(first_segment["display_row"])
                    * character_height
                )

            overlay_x = int(overlay_clip["x"])
            overlay_y = int(overlay_clip["y"])
            overlay_right = overlay_x + int(
                overlay_clip["width"]
            )
            overlay_bottom = overlay_y + int(
                overlay_clip["height"]
            )

            source_groups = (
                (
                    "command",
                    previous.get("command_segments", ()),
                ),
                (
                    "line_number",
                    previous.get("line_number_segments", ()),
                ),
                (
                    "ordinary",
                    previous.get("ordinary_segments", ()),
                ),
            )
            source_tail_count = sum(
                len(source_segments)
                for _semantic_type, source_segments in source_groups
            )
            state[
                "last_resize_reflow_continuity_source_tail_count"
            ] = source_tail_count

            for semantic_type, source_segments in source_groups:
                for source in source_segments:
                    target_display_row = (
                        int(source["display_row"])
                        + shift_rows
                    )

                    if (
                        target_display_row < 0
                        or target_display_row > maximum_display_row
                    ):
                        continue

                    start_column = min(
                        max(int(source["start_column"]), 0),
                        current_columns,
                    )
                    end_column = min(
                        max(
                            int(source["end_column"]),
                            start_column,
                        ),
                        current_columns,
                    )

                    if end_column <= start_column:
                        continue

                    excluded = list(
                        current_coverage.get(
                            target_display_row,
                            (),
                        )
                    )
                    excluded.extend(
                        generated_coverage.get(
                            target_display_row,
                            (),
                        )
                    )
                    uncovered_ranges = (
                        cls._subtract_column_ranges(
                            start_column,
                            end_column,
                            excluded,
                        )
                    )

                    for uncovered_start, uncovered_end in uncovered_ranges:
                        x = (
                            int(geometry["x"])
                            + uncovered_start * character_width
                        )
                        y = (
                            grid_y
                            + target_display_row * character_height
                            + row_y_offset
                        )
                        width = (
                            uncovered_end - uncovered_start
                        ) * character_width
                        height = max(
                            min(
                                int(source.get("height", 0)),
                                character_height,
                            ),
                            1,
                        )

                        if (
                            x + width <= overlay_x
                            or x >= overlay_right
                            or y + height <= overlay_y
                            or y >= overlay_bottom
                        ):
                            continue

                        bridge[semantic_type].append(
                            {
                                "semantic_type": semantic_type,
                                "absolute_row": (
                                    int(source.get("absolute_row", -1))
                                    + shift_rows
                                ),
                                "display_row": target_display_row,
                                "start_column": uncovered_start,
                                "end_column": uncovered_end,
                                "x": x,
                                "y": y,
                                "width": width,
                                "height": height,
                            }
                        )
                        generated_coverage.setdefault(
                            target_display_row,
                            [],
                        ).append(
                            (uncovered_start, uncovered_end)
                        )

                generated_coverage = {
                    display_row: cls._merge_column_ranges(ranges)
                    for display_row, ranges
                    in generated_coverage.items()
                }

            ordinary_bridge = bridge["ordinary"]
            command_bridge = bridge["command"]
            line_number_bridge = bridge["line_number"]
            output_count = (
                len(ordinary_bridge)
                + len(command_bridge)
                + len(line_number_bridge)
            )
            state[
                "last_resize_reflow_continuity_output_count"
            ] = output_count
            state["last_resize_reflow_continuity_error"] = ""

            if output_count:
                state[
                    "resize_reflow_continuity_applied_count"
                ] += 1
                state[
                    "resize_reflow_continuity_segment_total"
                ] += output_count
                state[
                    "resize_reflow_continuity_ordinary_segment_total"
                ] += len(ordinary_bridge)
                state[
                    "resize_reflow_continuity_command_segment_total"
                ] += len(command_bridge)
                state[
                    "resize_reflow_continuity_line_number_segment_total"
                ] += len(line_number_bridge)
            else:
                state[
                    "resize_reflow_continuity_empty_count"
                ] += 1

            return (
                ordinary_bridge,
                command_bridge,
                line_number_bridge,
            )

        except Exception as exc:
            state[
                "resize_reflow_continuity_failure_count"
            ] += 1
            state["last_resize_reflow_continuity_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            state["last_resize_reflow_continuity_output_count"] = 0
            return empty

    def _capture_native_row_content_draw_frame(
        self,
        vte,
        state,
        geometry,
        row_coordinate_offset,
    ):
        """Capture one raw row snapshot and record its extraction cost."""

        extract_started_ns = time.perf_counter_ns()
        row_content_frame = self._extract_visible_row_content_frame(
            vte,
            geometry,
            row_coordinate_offset,
        )
        extract_elapsed_ns = (
            time.perf_counter_ns() - extract_started_ns
        )
        row_contents = row_content_frame["row_contents"]
        state["native_row_content_frame_extract_count"] += 1
        state[
            "native_row_content_frame_extract_attempted_row_total"
        ] += int(row_content_frame["attempted_row_count"])
        state[
            "native_row_content_frame_extract_content_row_total"
        ] += len(row_contents)
        state[
            "native_row_content_frame_extract_last_content_row_count"
        ] = len(row_contents)
        state[
            "native_row_content_frame_extract_last_ns"
        ] = extract_elapsed_ns
        state[
            "native_row_content_frame_extract_total_ns"
        ] += extract_elapsed_ns
        state[
            "native_row_content_frame_extract_max_ns"
        ] = max(
            state["native_row_content_frame_extract_max_ns"],
            extract_elapsed_ns,
        )
        return row_content_frame

    def _project_native_row_content_reference(
        self,
        state,
        geometry,
        row_content_frame,
    ):
        """Build and time the same-frame Python numeric reference."""

        projection_started_ns = time.perf_counter_ns()
        python_segments = self._project_visible_row_content_frame(
            geometry,
            row_content_frame,
        )
        projection_elapsed_ns = (
            time.perf_counter_ns() - projection_started_ns
        )
        state["native_row_content_frame_projection_count"] += 1
        state[
            "native_row_content_frame_projection_last_ns"
        ] = projection_elapsed_ns
        state[
            "native_row_content_frame_projection_total_ns"
        ] += projection_elapsed_ns
        state[
            "native_row_content_frame_projection_max_ns"
        ] = max(
            state["native_row_content_frame_projection_max_ns"],
            projection_elapsed_ns,
        )
        return python_segments

    def _prepare_native_row_content_draw_frame(
        self,
        vte,
        state,
        geometry,
        row_coordinate_offset,
    ):
        """Retain the exact-every-draw v56 path when v57 is not active."""

        if not NATIVE_ROW_CONTENT_FRAME_ENABLED:
            return None, None

        if state.get(
            "native_row_content_frame_latched_off",
            False,
        ):
            return None, None

        try:
            row_content_frame = (
                self._capture_native_row_content_draw_frame(
                    vte,
                    state,
                    geometry,
                    row_coordinate_offset,
                )
            )
            python_segments = (
                self._project_native_row_content_reference(
                    state,
                    geometry,
                    row_content_frame,
                )
            )
            state["native_row_content_frame_last_error"] = ""
            return row_content_frame, python_segments

        except Exception as exc:
            state[
                "native_row_content_frame_failure_count"
            ] += 1
            state[
                "native_row_content_frame_fallback_count"
            ] += 1
            state[
                "native_row_content_frame_last_status"
            ] = "error"
            state[
                "native_row_content_frame_last_error"
            ] = f"{type(exc).__name__}: {exc}"

            if not state.get(
                "native_row_content_frame_latched_off",
                False,
            ):
                state[
                    "native_row_content_frame_latch_count"
                ] += 1
                state[
                    "native_row_content_frame_latched_off"
                ] = True

            return None, None

    @staticmethod
    def _native_row_content_fast_exact_check_required(
        request_index,
    ):
        """Return whether this v57 frame receives an exact Python check."""

        request_index = int(request_index)

        if request_index <= (
            NATIVE_ROW_CONTENT_FAST_PATH_CALIBRATION_FRAMES
        ):
            return True

        return (
            request_index
            % NATIVE_ROW_CONTENT_FAST_PATH_SENTINEL_INTERVAL
            == 0
        )

    @classmethod
    def _apply_native_row_content_fast_authority(
        cls,
        python_geometry,
        row_content_frame,
        native_frame,
    ):
        """Validate structure and graft Python text without a projection."""

        native_geometry = native_frame.get("geometry")
        native_segments = native_frame.get("segments")

        if not isinstance(native_geometry, dict):
            raise RuntimeError(
                "Native fast-path geometry is not a dictionary."
            )

        if not isinstance(native_segments, list):
            raise RuntimeError(
                "Native fast-path segments are not a list."
            )

        rows = max(int(python_geometry["rows"]), 0)
        columns = max(int(python_geometry["columns"]), 0)
        character_width = int(
            python_geometry["character_width"]
        )
        character_height = int(
            python_geometry["character_height"]
        )

        if character_width <= 0 or character_height <= 0:
            raise RuntimeError(
                "Native fast-path character geometry is invalid."
            )

        expected_capacity = (
            rows + VISIBLE_ROW_OVERSCAN_COUNT + 1
        )

        if int(row_content_frame["row_content_count"]) != expected_capacity:
            raise RuntimeError(
                "Captured fast-path capacity changed before authority."
            )

        if int(native_frame["row_content_count"]) != expected_capacity:
            raise RuntimeError(
                "Native fast-path row-content capacity is invalid."
            )

        if int(native_frame["required_segment_capacity"]) != expected_capacity:
            raise RuntimeError(
                "Native fast-path segment capacity is invalid."
            )

        authoritative_geometry = dict(python_geometry)

        for field_name in NATIVE_GEOMETRY_OUTPUT_FIELDS:
            native_value = int(native_geometry[field_name])

            if native_value != int(python_geometry[field_name]):
                raise RuntimeError(
                    "Native fast-path geometry differs at "
                    f"{field_name}."
                )

            authoritative_geometry[field_name] = native_value

        source_by_display_row = {}

        for source in row_content_frame["row_contents"]:
            display_row = int(source["display_row"])

            if not 0 <= display_row < expected_capacity:
                raise RuntimeError(
                    "Captured fast-path display row is invalid: "
                    f"{display_row}."
                )

            if display_row in source_by_display_row:
                raise RuntimeError(
                    "Captured fast-path display row is duplicated: "
                    f"{display_row}."
                )

            source_by_display_row[display_row] = source

        overlay_clip = cls._calculate_overlay_clip_rectangle(
            python_geometry
        )
        clip_x = int(overlay_clip["x"])
        clip_y = int(overlay_clip["y"])
        clip_right = clip_x + int(overlay_clip["width"])
        clip_bottom = clip_y + int(overlay_clip["height"])
        fractional_row = (
            float(row_content_frame["scroll_value"])
            - math.floor(float(row_content_frame["scroll_value"]))
        )
        pixel_scroll_offset = int(
            round(fractional_row * character_height)
        )

        if pixel_scroll_offset >= character_height:
            pixel_scroll_offset = 0

        row_gap = min(
            max(int(ROW_GAP_PX), 0),
            character_height - 1,
        )
        upper_gap = row_gap // 2
        band_height = max(character_height - row_gap, 1)
        expected_y_by_display_row = {}

        for display_row in source_by_display_row:
            expected_y = (
                int(python_geometry["y"])
                + display_row * character_height
                - pixel_scroll_offset
                + upper_gap
            )

            if (
                expected_y + band_height > clip_y
                and expected_y < clip_bottom
            ):
                expected_y_by_display_row[display_row] = expected_y

        authoritative_segments = []
        seen_display_rows = set()
        previous_display_row = -1

        for native_segment in native_segments:
            display_row = int(native_segment["display_row"])
            source = source_by_display_row.get(display_row)

            if source is None:
                raise RuntimeError(
                    "Native fast-path segment has no captured row: "
                    f"{display_row}."
                )

            if display_row in seen_display_rows:
                raise RuntimeError(
                    "Native fast-path display row is duplicated: "
                    f"{display_row}."
                )

            if display_row <= previous_display_row:
                raise RuntimeError(
                    "Native fast-path rows are not strictly ordered."
                )

            for field_name in (
                "absolute_row",
                "display_row",
                "start_column",
                "end_column",
            ):
                if int(native_segment[field_name]) != int(source[field_name]):
                    raise RuntimeError(
                        "Native fast-path row identity differs at "
                        f"{display_row}.{field_name}."
                    )

            start_column = int(native_segment["start_column"])
            end_column = int(native_segment["end_column"])
            x = int(native_segment["x"])
            y = int(native_segment["y"])
            width = int(native_segment["width"])
            height = int(native_segment["height"])

            if not 0 <= start_column < end_column <= columns:
                raise RuntimeError(
                    "Native fast-path columns are outside the grid."
                )

            if width <= 0 or height != band_height:
                raise RuntimeError(
                    "Native fast-path band dimensions are invalid."
                )

            source_left = (
                int(python_geometry["x"])
                + start_column * character_width
            )
            source_right = (
                int(python_geometry["x"])
                + end_column * character_width
            )

            if x < max(source_left, clip_x) or x + width > min(
                source_right,
                clip_right,
            ):
                raise RuntimeError(
                    "Native fast-path horizontal band is unbounded."
                )

            if y != expected_y_by_display_row.get(display_row):
                raise RuntimeError(
                    "Native fast-path vertical band is invalid."
                )

            authoritative_segment = {
                field_name: int(native_segment[field_name])
                for field_name in NATIVE_SEGMENT_FIELDS
            }
            authoritative_segment["row_text"] = str(
                source["row_text"]
            )
            authoritative_segments.append(authoritative_segment)
            seen_display_rows.add(display_row)
            previous_display_row = display_row

        if seen_display_rows != set(expected_y_by_display_row):
            raise RuntimeError(
                "Native fast-path visible row coverage is incomplete."
            )

        return authoritative_geometry, authoritative_segments

    def _run_native_row_content_fast_path(
        self,
        vte,
        state,
        geometry,
        row_coordinate_offset,
    ):
        """Run one guarded v57 frame or return None for the v56 path."""

        if not NATIVE_ROW_CONTENT_FAST_PATH_ENABLED:
            return None

        if state.get(
            "native_row_content_fast_path_latched_off",
            False,
        ):
            state[
                "native_row_content_fast_path_latched_skip_count"
            ] += 1
            return None

        started_ns = time.perf_counter_ns()
        row_content_frame = None
        python_segments = None
        phase = "capture"
        state["native_row_content_fast_path_request_count"] += 1
        request_index = state[
            "native_row_content_fast_path_request_count"
        ]
        exact_check = (
            self._native_row_content_fast_exact_check_required(
                request_index
            )
        )
        state[
            "native_row_content_fast_path_last_exact_check"
        ] = exact_check
        state[
            "native_row_content_fast_path_last_python_segment_count"
        ] = 0
        state[
            "native_row_content_fast_path_last_native_segment_count"
        ] = 0
        state[
            "native_draw_frame_authority_last_applied"
        ] = False
        state["native_draw_frame_authority_attempt_count"] += 1

        try:
            row_content_frame = (
                self._capture_native_row_content_draw_frame(
                    vte,
                    state,
                    geometry,
                    row_coordinate_offset,
                )
            )
            state[
                "native_row_content_frame_last_row_content_count"
            ] = int(row_content_frame["row_content_count"])

            phase = "native_call"

            if not self._native_shadow_engine.available:
                raise RuntimeError(
                    "Authenticated native engine is unavailable: "
                    f"{self._native_shadow_engine.last_error}"
                )

            state[
                "native_row_content_fast_path_native_call_count"
            ] += 1
            native_frame = (
                self._native_shadow_engine
                .calculate_frame_from_row_contents(
                    geometry,
                    float(row_content_frame["scroll_value"]),
                    row_coordinate_offset,
                    row_content_frame["row_contents"],
                    overscan_rows=VISIBLE_ROW_OVERSCAN_COUNT,
                )
            )
            native_segments = native_frame["segments"]
            state[
                "native_row_content_fast_path_last_native_segment_count"
            ] = len(native_segments)
            state[
                "native_row_content_frame_last_native_segment_count"
            ] = len(native_segments)

            if exact_check:
                phase = "exact_check"
                state[
                    "native_row_content_fast_path_exact_check_count"
                ] += 1
                python_segments = (
                    self._project_native_row_content_reference(
                        state,
                        geometry,
                        row_content_frame,
                    )
                )
                state[
                    "native_row_content_fast_path_last_python_segment_count"
                ] = len(python_segments)
                state[
                    "native_row_content_frame_last_python_segment_count"
                ] = len(python_segments)

                try:
                    authoritative = self._apply_native_frame_authority(
                        geometry,
                        python_segments,
                        native_frame,
                    )
                except Exception:
                    state[
                        "native_row_content_fast_path_exact_mismatch_count"
                    ] += 1
                    raise

                state[
                    "native_row_content_fast_path_exact_match_count"
                ] += 1
                status = "exact_match"
            else:
                phase = "structural_check"
                state[
                    "native_row_content_fast_path_structural_check_count"
                ] += 1

                try:
                    authoritative = (
                        self._apply_native_row_content_fast_authority(
                            geometry,
                            row_content_frame,
                            native_frame,
                        )
                    )
                except Exception:
                    state[
                        "native_row_content_fast_path_structural_reject_count"
                    ] += 1
                    raise

                state[
                    "native_row_content_fast_path_structural_accept_count"
                ] += 1
                state[
                    "native_row_content_fast_path_projection_avoided_count"
                ] += 1
                status = "structural_accept"

            state[
                "native_row_content_fast_path_application_count"
            ] += 1
            state[
                "native_row_content_fast_path_last_status"
            ] = status
            state[
                "native_row_content_fast_path_last_error"
            ] = ""
            state[
                "native_row_content_frame_last_status"
            ] = "superseded_by_fast_path"
            state["native_row_content_frame_last_error"] = ""
            state["native_draw_frame_authority_applied_count"] += 1
            state[
                "native_draw_frame_authority_last_applied"
            ] = True
            state[
                "native_draw_frame_authority_last_fallback_reason"
            ] = ""
            state[
                "native_draw_frame_authority_last_error"
            ] = ""
            return authoritative

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

            if row_content_frame is not None and python_segments is None:
                try:
                    python_segments = (
                        self._project_native_row_content_reference(
                            state,
                            geometry,
                            row_content_frame,
                        )
                    )
                    state[
                        "native_row_content_fast_path_fallback_projection_count"
                    ] += 1
                except Exception as fallback_exc:
                    error += (
                        "; fallback_projection="
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    )

            if python_segments is None:
                python_segments = self._calculate_visible_row_segments(
                    vte,
                    geometry,
                    row_coordinate_offset,
                )

            state[
                "native_row_content_fast_path_failure_count"
            ] += 1
            state[
                "native_row_content_fast_path_fallback_count"
            ] += 1
            state[
                "native_row_content_fast_path_last_status"
            ] = f"{phase}_fallback"
            state[
                "native_row_content_fast_path_last_error"
            ] = error

            if not state.get(
                "native_row_content_fast_path_latched_off",
                False,
            ):
                state[
                    "native_row_content_fast_path_latch_count"
                ] += 1
                state[
                    "native_row_content_fast_path_latched_off"
                ] = True

            state["native_draw_frame_authority_fallback_count"] += 1
            state[
                "native_draw_frame_authority_last_fallback_reason"
            ] = f"fast_path_{phase}"
            state[
                "native_draw_frame_authority_last_error"
            ] = error
            return geometry, python_segments

        finally:
            elapsed_ns = time.perf_counter_ns() - started_ns
            state[
                "native_row_content_fast_path_last_ns"
            ] = elapsed_ns
            state[
                "native_row_content_fast_path_total_ns"
            ] += elapsed_ns
            state[
                "native_row_content_fast_path_max_ns"
            ] = max(
                state["native_row_content_fast_path_max_ns"],
                elapsed_ns,
            )

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

        row_coordinate_offset = int(
            state.get(
                "visual_row_coordinate_offset",
                0,
            )
        )

        row_content_frame = None
        row_segments = None
        fast_path_frame = self._run_native_row_content_fast_path(
            vte,
            state,
            geometry,
            row_coordinate_offset,
        )

        if fast_path_frame is not None:
            geometry, row_segments = fast_path_frame

        elif NATIVE_ROW_CONTENT_FRAME_ENABLED:
            (
                row_content_frame,
                row_segments,
            ) = self._prepare_native_row_content_draw_frame(
                vte,
                state,
                geometry,
                row_coordinate_offset,
            )

        if row_segments is None:
            row_segments = self._calculate_visible_row_segments(
                vte,
                geometry,
                row_coordinate_offset,
            )

        if fast_path_frame is None:
            # Validation-only draw-path parity. A separately gated canary may
            # select only an exact native match. A missing/mismatched frame
            # falls back immediately and latches draw authority off for this
            # pane. The v57 fast path has its own bounded validation above.
            matched_native_draw_frame = None

            if NATIVE_DRAW_FRAME_SHADOW_ENABLED:
                matched_native_draw_frame = (
                    self._shadow_compare_native_draw_frame(
                        vte,
                        state,
                        geometry,
                        row_coordinate_offset,
                        row_segments,
                        row_content_frame=row_content_frame,
                    )
                )

            (
                geometry,
                row_segments,
            ) = self._select_native_draw_frame_authority(
                state,
                geometry,
                row_segments,
                matched_native_draw_frame,
            )

        # Derive the bounded clip only after authority selection so every
        # downstream coordinate originates from the same chosen frame.
        overlay_clip = self._calculate_overlay_clip_rectangle(
            geometry
        )

        grid_bottom = int(
            geometry["y"] + geometry["height"]
        )
        overlay_bottom = int(
            overlay_clip["y"] + overlay_clip["height"]
        )
        overscan_segment_count = sum(
            1
            for segment in row_segments
            if (
                int(segment["y"]) + int(segment["height"])
                > grid_bottom
                and int(segment["y"]) < overlay_bottom
            )
        )

        extension_height = int(
            overlay_clip["extension_height"]
        )
        state["visible_row_overscan_last_segment_count"] = (
            overscan_segment_count
        )
        state["visible_row_overscan_last_extension_height"] = (
            extension_height
        )
        state["visible_row_overscan_last_lowest_display_row"] = (
            max(
                (
                    int(segment["display_row"])
                    for segment in row_segments
                ),
                default=-1,
            )
        )
        state["visible_row_overscan_max_extension_height"] = max(
            int(
                state.get(
                    "visible_row_overscan_max_extension_height",
                    0,
                )
            ),
            extension_height,
        )

        if extension_height > 0:
            state["visible_row_overscan_draw_count"] += 1
            state["visible_row_overscan_segment_total"] += (
                overscan_segment_count
            )

        resize_pending = (
            state.get("resize_source_id") is not None
        )

        bottom_points_in_clip = self._record_draw_damage_clip(
            cairo_context,
            state,
            row_segments,
            resize_pending,
            geometry,
        )

        if (
            bottom_points_in_clip is not None
            and bottom_points_in_clip < BOTTOM_DAMAGE_PROBE_POINT_COUNT
        ):
            self._schedule_bottom_damage_repaint(
                vte,
                state,
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

        if RESIZE_EVENT_TRACE_ENABLED:
            self._record_resize_event_trace(
                vte,
                state,
                "draw",
                geometry=geometry,
                bottom_points=bottom_points_in_clip,
                segment_count=len(row_segments),
                lowest_display_row=max(
                    (
                        int(segment["display_row"])
                        for segment in row_segments
                    ),
                    default=-1,
                ),
                cache_miss_count=cache_miss_count,
            )

        if cache_miss_count:
            state["draw_cache_miss_frame_count"] += 1
            state["draw_cache_miss_row_total"] += int(
                cache_miss_count
            )

            if resize_pending:
                state[
                    "resize_draw_cache_miss_frame_count"
                ] += 1

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
            # GTK's incoming dirty clip can trail a live reflow by one row.
            # Expand only that incomplete resize draw, then immediately
            # intersect it with the bounded overlay rectangle below.
            if row_segments:
                self._expand_incomplete_resize_draw_clip(
                    cairo_context,
                    state,
                    overlay_clip,
                    resize_pending,
                )

            # The base geometry remains the authoritative VTE cell grid.
            # A bounded lower extension covers rows exposed by a GTK
            # allocation one frame before VTE advances get_row_count().
            cairo_context.rectangle(
                overlay_clip["x"],
                overlay_clip["y"],
                overlay_clip["width"],
                overlay_clip["height"],
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

            if TRANSACTIONAL_COPY_ENABLED:
                try:
                    self._draw_copy_confirmation(
                        cairo_context,
                        geometry,
                        state,
                    )
                except Exception as exc:
                    state[
                        "copy_confirmation_failure_count"
                    ] += 1
                    state["copy_confirmation_last_error"] = (
                        f"{type(exc).__name__}: {exc}"
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
    def _draw_copy_confirmation(
        cls,
        cairo_context,
        geometry,
        state,
    ):
        """Draw one compact no-shadow confirmation in the current pass."""

        text = str(state.get("copy_confirmation_text", ""))
        until_ns = int(
            state.get("copy_confirmation_until_ns", 0)
        )

        if (
            not text
            or until_ns <= time.monotonic_ns()
        ):
            return

        font_size = min(
            max(float(geometry["character_height"]) * 0.68, 10.0),
            13.0,
        )
        padding_x = 10.0
        padding_y = 6.0
        maximum_badge_width = max(
            float(geometry["width"]) - 16.0,
            1.0,
        )

        cairo_context.select_font_face(
            "Sans",
            0,
            1,
        )
        cairo_context.set_font_size(font_size)

        def measure(candidate):
            extents = cairo_context.text_extents(candidate)

            if hasattr(extents, "width"):
                return (
                    float(extents.x_bearing),
                    float(extents.y_bearing),
                    float(extents.width),
                    float(extents.height),
                )

            return tuple(float(value) for value in extents[:4])

        x_bearing, y_bearing, text_width, text_height = (
            measure(text)
        )

        if text_width + 2.0 * padding_x > maximum_badge_width:
            line_count = int(state.get("last_copy_lines", 0))
            text = (
                "Full scrollback copied · "
                f"{line_count:,} lines"
            )
            x_bearing, y_bearing, text_width, text_height = (
                measure(text)
            )

        if text_width + 2.0 * padding_x > maximum_badge_width:
            text = "Full scrollback copied"
            x_bearing, y_bearing, text_width, text_height = (
                measure(text)
            )

        if text_width + 2.0 * padding_x > maximum_badge_width:
            text = "Copied"
            x_bearing, y_bearing, text_width, text_height = (
                measure(text)
            )

        badge_width = min(
            text_width + 2.0 * padding_x,
            maximum_badge_width,
        )
        badge_height = text_height + 2.0 * padding_y
        badge_x = (
            float(geometry["x"])
            + float(geometry["width"])
            - badge_width
            - 8.0
        )
        badge_y = float(geometry["y"]) + 8.0

        cairo_context.new_path()
        cls._append_rounded_rectangle(
            cairo_context,
            badge_x,
            badge_y,
            badge_width,
            badge_height,
            5.0,
        )
        cairo_context.set_source_rgba(
            0.08,
            0.09,
            0.12,
            0.90,
        )
        cairo_context.fill()

        cairo_context.set_source_rgba(
            1.0,
            1.0,
            1.0,
            0.96,
        )
        cairo_context.move_to(
            badge_x + padding_x - x_bearing,
            badge_y + padding_y - y_bearing,
        )
        cairo_context.show_text(text)
        state["copy_confirmation_draw_count"] += 1
        state["copy_confirmation_last_error"] = ""

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
                # FIRST_FRAME_LEXICAL_CACHE_MISS_V1
                #
                # Draw callbacks can encounter newly exposed rows before
                # the deferred semantic cache refresh completes.  Use the
                # same deterministic lexical detector as the authoritative
                # cache builder so commands are violet on that first frame
                # instead of briefly falling back to ordinary blue.
                command_ranges = cls._find_lexical_command_ranges(
                    row_text,
                    maximum_columns,
                )
                line_ranges = cls._find_line_number_ranges(
                    row_text,
                    maximum_columns,
                )

                line_without_commands = []

                for line_start, line_end in line_ranges:
                    line_without_commands.extend(
                        cls._subtract_column_ranges(
                            line_start,
                            line_end,
                            command_ranges,
                        )
                    )

                line_ranges = cls._merge_column_ranges(
                    line_without_commands
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

    # OVERLAY_RIGHTMOST_CELL_REPAIR_V1
    @classmethod
    def _extract_overlay_terminal_row(
        cls,
        vte,
        absolute_row,
        columns,
    ):
        """Read all physical cells for overlay band calculation.

        The v31 runtime probe established that VTE 0.84 treats the
        formatted range's end column as a boundary for this operation:
        ``columns - 1`` returned one cell fewer than the terminal width,
        while ``columns`` returned exactly the complete physical row and
        did not consume any cell from the following row.

        Copy and coordinate-resolution extraction deliberately continue
        using _extract_terminal_row() and their previously validated
        endpoint behavior.
        """

        if columns <= 0:
            return ""

        result = vte.get_text_range_format(
            format=Vte.Format.TEXT,
            start_row=int(absolute_row),
            start_col=0,
            end_row=int(absolute_row),
            end_col=int(columns),
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
    def _extract_visible_row_content_frame(
        cls,
        vte,
        geometry,
        row_coordinate_offset=0,
    ):
        """Capture text and cell bounds once for one immutable draw input.

        GTK/VTE access stays on the Python main thread. The returned records
        contain no pixel projection, so the same snapshot can be consumed by
        both the Python reference projector and the C frame-v2 engine.
        """

        rows = int(geometry["rows"])
        columns = int(geometry["columns"])
        character_height = int(
            geometry["character_height"]
        )
        adjustment = vte.get_vadjustment()
        scroll_value = float(adjustment.get_value())
        row_content_count = (
            max(rows, 0)
            + VISIBLE_ROW_OVERSCAN_COUNT
            + 1
        )

        if (
            rows <= 0
            or columns <= 0
            or character_height <= 0
        ):
            return {
                "scroll_value": scroll_value,
                "row_content_count": row_content_count,
                "attempted_row_count": 0,
                "row_contents": [],
            }

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

        if pixel_scroll_offset >= character_height:
            top_absolute_row += 1
            pixel_scroll_offset = 0

        iteration_count = (
            rows + VISIBLE_ROW_OVERSCAN_COUNT
        )

        if pixel_scroll_offset:
            iteration_count += 1

        if iteration_count > row_content_count:
            raise RuntimeError(
                "Row-content extraction exceeded the native frame "
                f"capacity: {iteration_count}>{row_content_count}."
            )

        row_contents = []

        for display_row in range(iteration_count):
            absolute_row = (
                top_absolute_row + display_row
            )

            try:
                row_text = cls._extract_overlay_terminal_row(
                    vte,
                    absolute_row,
                    columns,
                )
            except Exception:
                # Match the proven v55 behavior when a row disappears while
                # VTE is concurrently producing output.
                continue

            content_columns = cls._row_content_columns(
                row_text,
                columns,
            )

            if content_columns is None:
                continue

            start_column, end_column = content_columns
            row_contents.append(
                {
                    "absolute_row": absolute_row,
                    "display_row": display_row,
                    "start_column": start_column,
                    "end_column": end_column,
                    "row_text": row_text,
                }
            )

        return {
            "scroll_value": scroll_value,
            "row_content_count": row_content_count,
            "attempted_row_count": iteration_count,
            "row_contents": row_contents,
        }

    @classmethod
    def _project_visible_row_content_frame(
        cls,
        geometry,
        row_content_frame,
    ):
        """Project a captured raw row snapshot with the v55 Python rules."""

        rows = int(geometry["rows"])
        columns = int(geometry["columns"])
        character_width = int(
            geometry["character_width"]
        )
        character_height = int(
            geometry["character_height"]
        )

        if (
            rows <= 0
            or columns <= 0
            or character_width <= 0
            or character_height <= 0
        ):
            return []

        expected_row_content_count = (
            rows + VISIBLE_ROW_OVERSCAN_COUNT + 1
        )
        row_content_count = int(
            row_content_frame["row_content_count"]
        )

        if row_content_count != expected_row_content_count:
            raise RuntimeError(
                "Captured row-content capacity changed before Python "
                f"projection: {row_content_count}!="
                f"{expected_row_content_count}."
            )

        scroll_value = float(
            row_content_frame["scroll_value"]
        )
        adjustment_top_row = math.floor(
            scroll_value
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

        if pixel_scroll_offset >= character_height:
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
        overlay_clip = cls._calculate_overlay_clip_rectangle(
            geometry
        )
        grid_top = int(overlay_clip["y"])
        grid_bottom = int(
            overlay_clip["y"]
            + overlay_clip["height"]
        )
        segments = []
        seen_display_rows = set()

        for row_content in row_content_frame["row_contents"]:
            display_row = int(
                row_content["display_row"]
            )

            if not 0 <= display_row < row_content_count:
                raise RuntimeError(
                    "Captured display row is outside the frame: "
                    f"{display_row}."
                )

            if display_row in seen_display_rows:
                raise RuntimeError(
                    "Captured duplicate display row: "
                    f"{display_row}."
                )

            seen_display_rows.add(display_row)
            start_column = int(
                row_content["start_column"]
            )
            end_column = int(
                row_content["end_column"]
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
                int(geometry["x"])
                + start_column * character_width
                + horizontal_inset
            )
            y = (
                int(geometry["y"])
                + display_row * character_height
                - pixel_scroll_offset
                + upper_gap
            )
            width = raw_width - 2 * horizontal_inset

            if width <= 0:
                continue

            if (
                y + band_height <= grid_top
                or y >= grid_bottom
            ):
                continue

            segments.append(
                {
                    "absolute_row": int(
                        row_content["absolute_row"]
                    ),
                    "display_row": display_row,
                    "start_column": start_column,
                    "end_column": end_column,
                    "x": x,
                    "y": y,
                    "width": width,
                    "height": band_height,
                    "row_text": str(
                        row_content["row_text"]
                    ),
                }
            )

        return segments

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

        # GTK can publish a taller allocation before VTE advances its
        # reported row count. Inspect two bounded look-ahead rows so a newly
        # exposed lower row has a segment on that first committed frame.
        # The overlay clip rectangle below still rejects rows outside the
        # widget's real CSS content area.
        iteration_count = rows + VISIBLE_ROW_OVERSCAN_COUNT

        if pixel_scroll_offset:
            iteration_count += 1

        overlay_clip = cls._calculate_overlay_clip_rectangle(
            geometry
        )
        grid_top = overlay_clip["y"]
        grid_bottom = (
            overlay_clip["y"]
            + overlay_clip["height"]
        )

        segments = []

        for display_row in range(iteration_count):
            absolute_row = (
                top_absolute_row + display_row
            )

            try:
                row_text = cls._extract_overlay_terminal_row(
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
    def _calculate_overlay_clip_rectangle(geometry):
        """Return the base grid plus bounded, allocated bottom overscan."""

        x = int(geometry["x"])
        y = int(geometry["y"])
        width = max(int(geometry["width"]), 0)
        height = max(int(geometry["height"]), 0)
        character_height = max(
            int(geometry.get("character_height", 0)),
            0,
        )
        style_bottom = max(
            int(geometry.get("style_bottom", 0)),
            0,
        )
        allocated_height = max(
            int(
                geometry.get(
                    "allocated_height",
                    y + height + style_bottom,
                )
            ),
            0,
        )

        grid_bottom = y + height
        allocated_content_bottom = max(
            allocated_height - style_bottom,
            y,
        )
        overscan_limit_bottom = (
            grid_bottom
            + VISIBLE_ROW_OVERSCAN_COUNT * character_height
        )
        clip_bottom = max(
            min(
                allocated_content_bottom,
                overscan_limit_bottom,
            ),
            y,
        )
        clip_height = max(clip_bottom - y, 0)

        return {
            "x": x,
            "y": y,
            "width": width,
            "height": clip_height,
            "grid_bottom": grid_bottom,
            "allocated_content_bottom": allocated_content_bottom,
            "overscan_limit_bottom": overscan_limit_bottom,
            "extension_height": max(clip_height - height, 0),
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

        native_shadow = (
            self._native_shadow_engine
            .diagnostic_snapshot()
        )

        geometry = self._calculate_geometry(vte)
        overlay_clip = self._calculate_overlay_clip_rectangle(
            geometry
        )

        cache = state.get("render_cache")

        segments = self._calculate_visible_row_segments(
            vte,
            geometry,
            state.get(
                "visual_row_coordinate_offset",
                0,
            ),
        )

        if cache is not None:
            (
                ordinary_segments,
                command_segments,
                line_number_segments,
                _diagnostic_cache_miss_count,
            ) = self._partition_from_semantic_rows(
                geometry,
                segments,
                cache.get("semantic_rows", {}),
            )
        else:
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
        diagnostic_grid_bottom = int(
            geometry["y"] + geometry["height"]
        )
        diagnostic_overlay_bottom = int(
            overlay_clip["y"] + overlay_clip["height"]
        )
        diagnostic_overscan_segment_count = sum(
            1
            for segment in segments
            if (
                int(segment["y"]) + int(segment["height"])
                > diagnostic_grid_bottom
                and int(segment["y"])
                < diagnostic_overlay_bottom
            )
        )
        diagnostic_lowest_display_row = max(
            (
                int(segment["display_row"])
                for segment in segments
            ),
            default=-1,
        )

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
                "validation_mode_environment="
                f"{VALIDATION_MODE_ENVIRONMENT}"
            ),
            "production_hotpath_cleanup=True",
            "functional_behavior_changed=True",
            f"validation_mode_enabled={VALIDATION_MODE_ENABLED}",
            "transactional_copy_mode=default_on_production",
            (
                "transactional_copy_default_enabled="
                f"{TRANSACTIONAL_COPY_DEFAULT_ENABLED}"
            ),
            (
                "transactional_copy_disable_environment="
                f"{TRANSACTIONAL_COPY_DISABLE_ENVIRONMENT}"
            ),
            (
                "transactional_copy_disable_requested="
                f"{TRANSACTIONAL_COPY_DISABLE_REQUESTED}"
            ),
            (
                "transactional_copy_enabled="
                f"{TRANSACTIONAL_COPY_ENABLED}"
            ),
            "transactional_copy_path=prepared_snapshot_exact_same_click_fallback",
            "transactional_copy_clipboard_commit=atomic_set_then_idle_store",
            "transactional_copy_visual_feedback=single_pass_no_shadow_badge",
            "transactional_copy_kill_switch_fallback=v59_exact_copy",
            (
                "copy_snapshot_quiet_ms="
                f"{COPY_SNAPSHOT_QUIET_MS}"
            ),
            (
                "copy_snapshot_max_characters="
                f"{COPY_SNAPSHOT_MAX_CHARACTERS}"
            ),
            (
                "copy_transaction_max_attempts="
                f"{COPY_TRANSACTION_MAX_ATTEMPTS}"
            ),
            (
                "copy_confirmation_duration_ms="
                f"{COPY_CONFIRMATION_DURATION_MS}"
            ),
            (
                "native_shadow_validation_enabled="
                f"{NATIVE_SHADOW_VALIDATION_ENABLED}"
            ),
            (
                "native_production_fast_path_mode="
                "default_on_authenticated"
            ),
            (
                "native_production_fast_path_default_enabled="
                f"{NATIVE_PRODUCTION_FAST_PATH_DEFAULT_ENABLED}"
            ),
            (
                "native_production_fast_path_disable_environment="
                f"{NATIVE_PRODUCTION_FAST_PATH_DISABLE_ENVIRONMENT}"
            ),
            (
                "native_production_fast_path_disable_requested="
                f"{NATIVE_PRODUCTION_FAST_PATH_DISABLE_REQUESTED}"
            ),
            (
                "native_production_fast_path_enabled="
                f"{NATIVE_PRODUCTION_FAST_PATH_ENABLED}"
            ),
            (
                "native_production_fast_path_effective="
                f"{NATIVE_PRODUCTION_FAST_PATH_ENABLED and not state.get('native_row_content_fast_path_latched_off', False)}"
            ),
            "native_production_fast_path_validation_mode_required=False",
            "native_production_fast_path_structural_guard_every_draw=True",
            "native_production_fast_path_sparse_exact_sentinels=True",
            "native_production_fast_path_same_frame_fallback=True",
            "native_production_fast_path_per_pane_latch=True",
            "native_production_fast_path_kill_switch_fallback=python_numeric_draw",
            (
                "native_frame_authority_environment="
                f"{NATIVE_FRAME_AUTHORITY_ENVIRONMENT}"
            ),
            (
                "native_frame_authority_requested="
                f"{NATIVE_FRAME_AUTHORITY_REQUESTED}"
            ),
            (
                "native_frame_authority_enabled="
                f"{NATIVE_FRAME_AUTHORITY_ENABLED}"
            ),
            (
                "native_draw_frame_shadow_enabled="
                f"{NATIVE_DRAW_FRAME_SHADOW_ENABLED}"
            ),
            (
                "native_draw_frame_authority_environment="
                f"{NATIVE_DRAW_FRAME_AUTHORITY_ENVIRONMENT}"
            ),
            (
                "native_draw_frame_authority_requested="
                f"{NATIVE_DRAW_FRAME_AUTHORITY_REQUESTED}"
            ),
            (
                "native_draw_frame_authority_enabled="
                f"{NATIVE_DRAW_FRAME_AUTHORITY_ENABLED}"
            ),
            (
                "native_row_content_frame_environment="
                f"{NATIVE_ROW_CONTENT_FRAME_ENVIRONMENT}"
            ),
            (
                "native_row_content_frame_requested="
                f"{NATIVE_ROW_CONTENT_FRAME_REQUESTED}"
            ),
            (
                "native_row_content_frame_enabled="
                f"{NATIVE_ROW_CONTENT_FRAME_ENABLED}"
            ),
            (
                "native_row_content_fast_path_environment="
                f"{NATIVE_ROW_CONTENT_FAST_PATH_ENVIRONMENT}"
            ),
            (
                "native_row_content_fast_path_requested="
                f"{NATIVE_ROW_CONTENT_FAST_PATH_REQUESTED}"
            ),
            (
                "native_row_content_fast_path_enabled="
                f"{NATIVE_ROW_CONTENT_FAST_PATH_ENABLED}"
            ),
            (
                "native_row_content_fast_path_calibration_frames="
                f"{NATIVE_ROW_CONTENT_FAST_PATH_CALIBRATION_FRAMES}"
            ),
            (
                "native_row_content_fast_path_sentinel_interval="
                f"{NATIVE_ROW_CONTENT_FAST_PATH_SENTINEL_INTERVAL}"
            ),
            (
                "native_draw_frame_stage="
                + (
                    "direct_row_content_sparse_production_fast_authority"
                    if NATIVE_PRODUCTION_FAST_PATH_ENABLED
                    else (
                        "direct_row_content_sparse_validation_fast_authority"
                        if NATIVE_ROW_CONTENT_FAST_PATH_ENABLED
                        else (
                            "direct_row_content_compare_native_authority"
                            if NATIVE_ROW_CONTENT_FRAME_ENABLED
                            else (
                                "draw_frame_native_authority_python_guarded"
                                if NATIVE_DRAW_FRAME_AUTHORITY_ENABLED
                                else (
                                    "draw_frame_compare_python_authoritative"
                                    if NATIVE_DRAW_FRAME_SHADOW_ENABLED
                                    else "disabled_production"
                                )
                            )
                        )
                    )
                )
            ),
            (
                "resize_event_trace_enabled="
                f"{RESIZE_EVENT_TRACE_ENABLED}"
            ),
            (
                "native_shadow_stage="
                + (
                    "authenticated_production_draw_fast_path"
                    if NATIVE_PRODUCTION_FAST_PATH_ENABLED
                    else (
                        "frame_batch_native_authority_python_guarded"
                        if NATIVE_FRAME_AUTHORITY_ENABLED
                        else (
                            "frame_batch_compare_python_authoritative"
                            if NATIVE_SHADOW_VALIDATION_ENABLED
                            else "loaded_dormant_python_authoritative"
                        )
                    )
                )
            ),
            (
                "native_shadow_enabled="
                f"{native_shadow['enabled']}"
            ),
            (
                "native_engine_authoritative="
                f"{native_shadow['authoritative']}"
            ),
            (
                "native_load_attempt_count="
                f"{native_shadow['load_attempt_count']}"
            ),
            (
                "native_load_success_count="
                f"{native_shadow['load_success_count']}"
            ),
            (
                "native_load_failure_count="
                f"{native_shadow['load_failure_count']}"
            ),
            (
                "native_engine_loaded="
                f"{native_shadow['loaded']}"
            ),
            (
                "native_engine_validated="
                f"{native_shadow['validated']}"
            ),
            (
                "native_engine_available="
                f"{native_shadow['available']}"
            ),
            (
                "native_last_error="
                f"{native_shadow['last_error']}"
            ),
            (
                "native_load_elapsed_ns="
                f"{native_shadow['load_elapsed_ns']}"
            ),
            (
                "native_binary_path="
                f"{native_shadow['binary_path']}"
            ),
            (
                "native_binary_sha256="
                f"{native_shadow['binary_sha256']}"
            ),
            (
                "native_binary_size="
                f"{native_shadow['binary_size']}"
            ),
            (
                "native_binary_device="
                f"{native_shadow['binary_device']}"
            ),
            (
                "native_binary_inode="
                f"{native_shadow['binary_inode']}"
            ),
            (
                "native_abi_version="
                f"{native_shadow['abi_version']}"
            ),
            (
                "native_feature_flags="
                f"{native_shadow['feature_flags']}"
            ),
            (
                "native_engine_build_id="
                f"{native_shadow['build_id']}"
            ),
            (
                "native_abi_query_status="
                f"{native_shadow['query_status']}"
            ),
            (
                "native_frame_abi_version="
                f"{NATIVE_ENGINE_FRAME_ABI_VERSION}"
            ),
            (
                "native_frame_abi_query_status="
                f"{native_shadow['frame_query_status']}"
            ),
            (
                "native_abi_info_size="
                f"{native_shadow['abi_sizes']['abi_info']}"
            ),
            (
                "native_geometry_input_size="
                f"{native_shadow['abi_sizes']['geometry_input']}"
            ),
            (
                "native_geometry_output_size="
                f"{native_shadow['abi_sizes']['geometry_output']}"
            ),
            (
                "native_viewport_input_size="
                f"{native_shadow['abi_sizes']['viewport_input']}"
            ),
            (
                "native_row_content_size="
                f"{native_shadow['abi_sizes']['row_content']}"
            ),
            (
                "native_segment_size="
                f"{native_shadow['abi_sizes']['segment']}"
            ),
            (
                "native_frame_abi_info_size="
                f"{native_shadow['abi_sizes']['frame_abi_info']}"
            ),
            (
                "native_frame_input_size="
                f"{native_shadow['abi_sizes']['frame_input']}"
            ),
            (
                "native_frame_output_size="
                f"{native_shadow['abi_sizes']['frame_output']}"
            ),
            (
                "native_geometry_call_count="
                f"{native_shadow['geometry_call_count']}"
            ),
            (
                "native_segment_call_count="
                f"{native_shadow['segment_call_count']}"
            ),
            (
                "native_frame_call_count="
                f"{native_shadow['frame_call_count']}"
            ),
            (
                "native_frame_python_output_used="
                f"{not (state.get('native_frame_authority_last_applied', False) or state.get('native_draw_frame_authority_last_applied', False))}"
            ),
            (
                "native_frame_shadow_input_mode="
                + (
                    "cache_segments_draw_row_content_sparse_fast_batched_with_geometry"
                    if NATIVE_ROW_CONTENT_FAST_PATH_ENABLED
                    else (
                        "cache_segments_draw_row_content_direct_batched_with_geometry"
                        if NATIVE_ROW_CONTENT_FRAME_ENABLED
                        else "python_emitted_segments_reconstructed_batched_with_geometry"
                    )
                )
            ),
            "native_legacy_separate_shadow_calls_enabled=False",
            (
                "native_frame_shadow_request_count="
                f"{state.get('native_frame_shadow_request_count', 0)}"
            ),
            (
                "native_frame_shadow_match_count="
                f"{state.get('native_frame_shadow_match_count', 0)}"
            ),
            (
                "native_frame_shadow_mismatch_count="
                f"{state.get('native_frame_shadow_mismatch_count', 0)}"
            ),
            (
                "native_frame_shadow_failure_count="
                f"{state.get('native_frame_shadow_failure_count', 0)}"
            ),
            (
                "native_frame_shadow_skip_count="
                f"{state.get('native_frame_shadow_skip_count', 0)}"
            ),
            (
                "native_frame_shadow_last_status="
                f"{state.get('native_frame_shadow_last_status', 'not_run')}"
            ),
            (
                "native_frame_shadow_last_mismatch_fields="
                f"{state.get('native_frame_shadow_last_mismatch_fields', '')}"
            ),
            (
                "native_frame_shadow_last_error="
                f"{state.get('native_frame_shadow_last_error', '')}"
            ),
            (
                "native_frame_shadow_last_row_content_count="
                f"{state.get('native_frame_shadow_last_row_content_count', 0)}"
            ),
            (
                "native_frame_shadow_last_required_capacity="
                f"{state.get('native_frame_shadow_last_required_capacity', 0)}"
            ),
            (
                "native_frame_shadow_last_python_segment_count="
                f"{state.get('native_frame_shadow_last_python_segment_count', 0)}"
            ),
            (
                "native_frame_shadow_last_native_segment_count="
                f"{state.get('native_frame_shadow_last_native_segment_count', 0)}"
            ),
            (
                "native_frame_shadow_last_ms="
                f"{state.get('native_frame_shadow_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_frame_shadow_average_ms="
                f"{state.get('native_frame_shadow_total_ns', 0) / max(state.get('native_frame_shadow_request_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_frame_shadow_max_ms="
                f"{state.get('native_frame_shadow_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_frame_authority_effective="
                f"{NATIVE_FRAME_AUTHORITY_ENABLED and not state.get('native_frame_authority_latched_off', False)}"
            ),
            (
                "native_frame_authority_attempt_count="
                f"{state.get('native_frame_authority_attempt_count', 0)}"
            ),
            (
                "native_frame_authority_applied_count="
                f"{state.get('native_frame_authority_applied_count', 0)}"
            ),
            (
                "native_frame_authority_fallback_count="
                f"{state.get('native_frame_authority_fallback_count', 0)}"
            ),
            (
                "native_frame_authority_latch_count="
                f"{state.get('native_frame_authority_latch_count', 0)}"
            ),
            (
                "native_frame_authority_latched_skip_count="
                f"{state.get('native_frame_authority_latched_skip_count', 0)}"
            ),
            (
                "native_frame_authority_latched_off="
                f"{state.get('native_frame_authority_latched_off', False)}"
            ),
            (
                "native_frame_authority_last_applied="
                f"{state.get('native_frame_authority_last_applied', False)}"
            ),
            (
                "native_frame_authority_last_fallback_reason="
                f"{state.get('native_frame_authority_last_fallback_reason', '')}"
            ),
            (
                "native_frame_authority_last_error="
                f"{state.get('native_frame_authority_last_error', '')}"
            ),
            (
                "native_draw_frame_authority_effective="
                f"{(NATIVE_DRAW_FRAME_AUTHORITY_ENABLED or NATIVE_PRODUCTION_FAST_PATH_ENABLED) and not state.get('native_draw_frame_authority_latched_off', False) and not state.get('native_row_content_fast_path_latched_off', False)}"
            ),
            (
                "native_draw_frame_python_output_used="
                f"{not state.get('native_draw_frame_authority_last_applied', False)}"
            ),
            (
                "native_draw_frame_shadow_request_count="
                f"{state.get('native_draw_frame_shadow_request_count', 0)}"
            ),
            (
                "native_draw_frame_shadow_match_count="
                f"{state.get('native_draw_frame_shadow_match_count', 0)}"
            ),
            (
                "native_draw_frame_shadow_mismatch_count="
                f"{state.get('native_draw_frame_shadow_mismatch_count', 0)}"
            ),
            (
                "native_draw_frame_shadow_failure_count="
                f"{state.get('native_draw_frame_shadow_failure_count', 0)}"
            ),
            (
                "native_draw_frame_shadow_skip_count="
                f"{state.get('native_draw_frame_shadow_skip_count', 0)}"
            ),
            (
                "native_draw_frame_shadow_last_status="
                f"{state.get('native_draw_frame_shadow_last_status', 'not_run')}"
            ),
            (
                "native_draw_frame_shadow_last_mismatch_fields="
                f"{state.get('native_draw_frame_shadow_last_mismatch_fields', '')}"
            ),
            (
                "native_draw_frame_shadow_last_error="
                f"{state.get('native_draw_frame_shadow_last_error', '')}"
            ),
            (
                "native_draw_frame_shadow_last_python_segment_count="
                f"{state.get('native_draw_frame_shadow_last_python_segment_count', 0)}"
            ),
            (
                "native_draw_frame_shadow_last_native_segment_count="
                f"{state.get('native_draw_frame_shadow_last_native_segment_count', 0)}"
            ),
            (
                "native_draw_frame_shadow_last_ms="
                f"{state.get('native_draw_frame_shadow_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_draw_frame_shadow_average_ms="
                f"{state.get('native_draw_frame_shadow_total_ns', 0) / max(state.get('native_draw_frame_shadow_request_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_draw_frame_shadow_max_ms="
                f"{state.get('native_draw_frame_shadow_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_draw_frame_authority_attempt_count="
                f"{state.get('native_draw_frame_authority_attempt_count', 0)}"
            ),
            (
                "native_draw_frame_authority_applied_count="
                f"{state.get('native_draw_frame_authority_applied_count', 0)}"
            ),
            (
                "native_draw_frame_authority_fallback_count="
                f"{state.get('native_draw_frame_authority_fallback_count', 0)}"
            ),
            (
                "native_draw_frame_authority_latch_count="
                f"{state.get('native_draw_frame_authority_latch_count', 0)}"
            ),
            (
                "native_draw_frame_authority_latched_skip_count="
                f"{state.get('native_draw_frame_authority_latched_skip_count', 0)}"
            ),
            (
                "native_draw_frame_authority_latched_off="
                f"{state.get('native_draw_frame_authority_latched_off', False)}"
            ),
            (
                "native_draw_frame_authority_last_applied="
                f"{state.get('native_draw_frame_authority_last_applied', False)}"
            ),
            (
                "native_draw_frame_authority_last_fallback_reason="
                f"{state.get('native_draw_frame_authority_last_fallback_reason', '')}"
            ),
            (
                "native_draw_frame_authority_last_error="
                f"{state.get('native_draw_frame_authority_last_error', '')}"
            ),
            (
                "native_row_content_frame_effective="
                f"{(NATIVE_ROW_CONTENT_FRAME_ENABLED or NATIVE_PRODUCTION_FAST_PATH_ENABLED) and not state.get('native_row_content_frame_latched_off', False) and not state.get('native_row_content_fast_path_latched_off', False)}"
            ),
            (
                "native_row_content_frame_extract_count="
                f"{state.get('native_row_content_frame_extract_count', 0)}"
            ),
            (
                "native_row_content_frame_extract_attempted_row_total="
                f"{state.get('native_row_content_frame_extract_attempted_row_total', 0)}"
            ),
            (
                "native_row_content_frame_extract_content_row_total="
                f"{state.get('native_row_content_frame_extract_content_row_total', 0)}"
            ),
            (
                "native_row_content_frame_extract_last_content_row_count="
                f"{state.get('native_row_content_frame_extract_last_content_row_count', 0)}"
            ),
            (
                "native_row_content_frame_extract_last_ms="
                f"{state.get('native_row_content_frame_extract_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_frame_extract_average_ms="
                f"{state.get('native_row_content_frame_extract_total_ns', 0) / max(state.get('native_row_content_frame_extract_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_frame_extract_max_ms="
                f"{state.get('native_row_content_frame_extract_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_frame_projection_count="
                f"{state.get('native_row_content_frame_projection_count', 0)}"
            ),
            (
                "native_row_content_frame_projection_last_ms="
                f"{state.get('native_row_content_frame_projection_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_frame_projection_average_ms="
                f"{state.get('native_row_content_frame_projection_total_ns', 0) / max(state.get('native_row_content_frame_projection_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_frame_projection_max_ms="
                f"{state.get('native_row_content_frame_projection_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_frame_request_count="
                f"{state.get('native_row_content_frame_request_count', 0)}"
            ),
            (
                "native_row_content_frame_match_count="
                f"{state.get('native_row_content_frame_match_count', 0)}"
            ),
            (
                "native_row_content_frame_mismatch_count="
                f"{state.get('native_row_content_frame_mismatch_count', 0)}"
            ),
            (
                "native_row_content_frame_failure_count="
                f"{state.get('native_row_content_frame_failure_count', 0)}"
            ),
            (
                "native_row_content_frame_skip_count="
                f"{state.get('native_row_content_frame_skip_count', 0)}"
            ),
            (
                "native_row_content_frame_fallback_count="
                f"{state.get('native_row_content_frame_fallback_count', 0)}"
            ),
            (
                "native_row_content_frame_latch_count="
                f"{state.get('native_row_content_frame_latch_count', 0)}"
            ),
            (
                "native_row_content_frame_latched_off="
                f"{state.get('native_row_content_frame_latched_off', False)}"
            ),
            (
                "native_row_content_frame_last_status="
                f"{state.get('native_row_content_frame_last_status', 'not_run')}"
            ),
            (
                "native_row_content_frame_last_error="
                f"{state.get('native_row_content_frame_last_error', '')}"
            ),
            (
                "native_row_content_frame_last_row_content_count="
                f"{state.get('native_row_content_frame_last_row_content_count', 0)}"
            ),
            (
                "native_row_content_frame_last_python_segment_count="
                f"{state.get('native_row_content_frame_last_python_segment_count', 0)}"
            ),
            (
                "native_row_content_frame_last_native_segment_count="
                f"{state.get('native_row_content_frame_last_native_segment_count', 0)}"
            ),
            (
                "native_row_content_fast_path_effective="
                f"{NATIVE_ROW_CONTENT_FAST_PATH_ENABLED and not state.get('native_row_content_fast_path_latched_off', False)}"
            ),
            (
                "native_row_content_fast_path_request_count="
                f"{state.get('native_row_content_fast_path_request_count', 0)}"
            ),
            (
                "native_row_content_fast_path_native_call_count="
                f"{state.get('native_row_content_fast_path_native_call_count', 0)}"
            ),
            (
                "native_row_content_fast_path_application_count="
                f"{state.get('native_row_content_fast_path_application_count', 0)}"
            ),
            (
                "native_row_content_fast_path_exact_check_count="
                f"{state.get('native_row_content_fast_path_exact_check_count', 0)}"
            ),
            (
                "native_row_content_fast_path_exact_match_count="
                f"{state.get('native_row_content_fast_path_exact_match_count', 0)}"
            ),
            (
                "native_row_content_fast_path_exact_mismatch_count="
                f"{state.get('native_row_content_fast_path_exact_mismatch_count', 0)}"
            ),
            (
                "native_row_content_fast_path_structural_check_count="
                f"{state.get('native_row_content_fast_path_structural_check_count', 0)}"
            ),
            (
                "native_row_content_fast_path_structural_accept_count="
                f"{state.get('native_row_content_fast_path_structural_accept_count', 0)}"
            ),
            (
                "native_row_content_fast_path_structural_reject_count="
                f"{state.get('native_row_content_fast_path_structural_reject_count', 0)}"
            ),
            (
                "native_row_content_fast_path_projection_avoided_count="
                f"{state.get('native_row_content_fast_path_projection_avoided_count', 0)}"
            ),
            (
                "native_row_content_fast_path_fallback_projection_count="
                f"{state.get('native_row_content_fast_path_fallback_projection_count', 0)}"
            ),
            (
                "native_row_content_fast_path_failure_count="
                f"{state.get('native_row_content_fast_path_failure_count', 0)}"
            ),
            (
                "native_row_content_fast_path_fallback_count="
                f"{state.get('native_row_content_fast_path_fallback_count', 0)}"
            ),
            (
                "native_row_content_fast_path_latch_count="
                f"{state.get('native_row_content_fast_path_latch_count', 0)}"
            ),
            (
                "native_row_content_fast_path_latched_skip_count="
                f"{state.get('native_row_content_fast_path_latched_skip_count', 0)}"
            ),
            (
                "native_row_content_fast_path_latched_off="
                f"{state.get('native_row_content_fast_path_latched_off', False)}"
            ),
            (
                "native_row_content_fast_path_last_status="
                f"{state.get('native_row_content_fast_path_last_status', 'not_run')}"
            ),
            (
                "native_row_content_fast_path_last_error="
                f"{state.get('native_row_content_fast_path_last_error', '')}"
            ),
            (
                "native_row_content_fast_path_last_exact_check="
                f"{state.get('native_row_content_fast_path_last_exact_check', False)}"
            ),
            (
                "native_row_content_fast_path_last_python_segment_count="
                f"{state.get('native_row_content_fast_path_last_python_segment_count', 0)}"
            ),
            (
                "native_row_content_fast_path_last_native_segment_count="
                f"{state.get('native_row_content_fast_path_last_native_segment_count', 0)}"
            ),
            (
                "native_row_content_fast_path_last_ms="
                f"{state.get('native_row_content_fast_path_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_fast_path_average_ms="
                f"{state.get('native_row_content_fast_path_total_ns', 0) / max(state.get('native_row_content_fast_path_request_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_row_content_fast_path_max_ms="
                f"{state.get('native_row_content_fast_path_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            "native_geometry_python_output_used=True",
            (
                "native_geometry_shadow_request_count="
                f"{state.get('native_geometry_shadow_request_count', 0)}"
            ),
            (
                "native_geometry_shadow_match_count="
                f"{state.get('native_geometry_shadow_match_count', 0)}"
            ),
            (
                "native_geometry_shadow_mismatch_count="
                f"{state.get('native_geometry_shadow_mismatch_count', 0)}"
            ),
            (
                "native_geometry_shadow_failure_count="
                f"{state.get('native_geometry_shadow_failure_count', 0)}"
            ),
            (
                "native_geometry_shadow_skip_count="
                f"{state.get('native_geometry_shadow_skip_count', 0)}"
            ),
            (
                "native_geometry_shadow_last_status="
                f"{state.get('native_geometry_shadow_last_status', 'not_run')}"
            ),
            (
                "native_geometry_shadow_last_mismatch_fields="
                f"{state.get('native_geometry_shadow_last_mismatch_fields', '')}"
            ),
            (
                "native_geometry_shadow_last_error="
                f"{state.get('native_geometry_shadow_last_error', '')}"
            ),
            (
                "native_geometry_shadow_last_ms="
                f"{state.get('native_geometry_shadow_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_geometry_shadow_average_ms="
                f"{state.get('native_geometry_shadow_total_ns', 0) / max(state.get('native_geometry_shadow_request_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_geometry_shadow_max_ms="
                f"{state.get('native_geometry_shadow_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            "native_segment_python_output_used=True",
            "native_segment_shadow_input_mode=python_emitted_segments_reconstructed_with_bounded_overscan",
            (
                "native_segment_shadow_request_count="
                f"{state.get('native_segment_shadow_request_count', 0)}"
            ),
            (
                "native_segment_shadow_match_count="
                f"{state.get('native_segment_shadow_match_count', 0)}"
            ),
            (
                "native_segment_shadow_mismatch_count="
                f"{state.get('native_segment_shadow_mismatch_count', 0)}"
            ),
            (
                "native_segment_shadow_failure_count="
                f"{state.get('native_segment_shadow_failure_count', 0)}"
            ),
            (
                "native_segment_shadow_skip_count="
                f"{state.get('native_segment_shadow_skip_count', 0)}"
            ),
            (
                "native_segment_shadow_last_status="
                f"{state.get('native_segment_shadow_last_status', 'not_run')}"
            ),
            (
                "native_segment_shadow_last_mismatch_fields="
                f"{state.get('native_segment_shadow_last_mismatch_fields', '')}"
            ),
            (
                "native_segment_shadow_last_error="
                f"{state.get('native_segment_shadow_last_error', '')}"
            ),
            (
                "native_segment_shadow_last_row_content_count="
                f"{state.get('native_segment_shadow_last_row_content_count', 0)}"
            ),
            (
                "native_segment_shadow_last_python_count="
                f"{state.get('native_segment_shadow_last_python_count', 0)}"
            ),
            (
                "native_segment_shadow_last_native_count="
                f"{state.get('native_segment_shadow_last_native_count', 0)}"
            ),
            (
                "native_segment_shadow_last_ms="
                f"{state.get('native_segment_shadow_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_segment_shadow_average_ms="
                f"{state.get('native_segment_shadow_total_ns', 0) / max(state.get('native_segment_shadow_request_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}"
            ),
            (
                "native_segment_shadow_max_ms="
                f"{state.get('native_segment_shadow_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}"
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
                "toggle_repaint_follow_up_delay_ms="
                f"{TOGGLE_REPAINT_FOLLOW_UP_DELAY_MS}"
            ),
            (
                "toggle_repaint_pending="
                f"{state.get('toggle_repaint_source_id') is not None}"
            ),
            (
                "toggle_repaint_target_active="
                f"{state.get('toggle_repaint_target_active', False)}"
            ),
            (
                "toggle_repaint_request_count="
                f"{state.get('toggle_repaint_request_count', 0)}"
            ),
            (
                "toggle_repaint_activation_request_count="
                f"{state.get('toggle_repaint_activation_request_count', 0)}"
            ),
            (
                "toggle_repaint_deactivation_request_count="
                f"{state.get('toggle_repaint_deactivation_request_count', 0)}"
            ),
            (
                "toggle_repaint_full_count="
                f"{state.get('toggle_repaint_full_count', 0)}"
            ),
            (
                "toggle_repaint_follow_up_count="
                f"{state.get('toggle_repaint_follow_up_count', 0)}"
            ),
            (
                "toggle_repaint_cancel_count="
                f"{state.get('toggle_repaint_cancel_count', 0)}"
            ),
            (
                "toggle_repaint_failure_count="
                f"{state.get('toggle_repaint_failure_count', 0)}"
            ),
            (
                "last_toggle_repaint_error="
                f"{state.get('last_toggle_repaint_error', '')}"
            ),
            (
                "resize_refresh_count="
                f"{state.get('resize_refresh_count', 0)}"
            ),
            "live_resize_bottom_damage_queue=targeted_cell_height",
            (
                "resize_bottom_damage_request_count="
                f"{state.get('resize_bottom_damage_request_count', 0)}"
            ),
            (
                "resize_bottom_damage_queue_count="
                f"{state.get('resize_bottom_damage_queue_count', 0)}"
            ),
            (
                "resize_bottom_damage_skip_count="
                f"{state.get('resize_bottom_damage_skip_count', 0)}"
            ),
            (
                "resize_bottom_damage_failure_count="
                f"{state.get('resize_bottom_damage_failure_count', 0)}"
            ),
            (
                "last_resize_bottom_damage_error="
                f"{state.get('last_resize_bottom_damage_error', '')}"
            ),
            (
                "last_resize_bottom_damage_x="
                f"{state.get('last_resize_bottom_damage_x', 0)}"
            ),
            (
                "last_resize_bottom_damage_y="
                f"{state.get('last_resize_bottom_damage_y', 0)}"
            ),
            (
                "last_resize_bottom_damage_width="
                f"{state.get('last_resize_bottom_damage_width', 0)}"
            ),
            (
                "last_resize_bottom_damage_height="
                f"{state.get('last_resize_bottom_damage_height', 0)}"
            ),
            "resize_frame_repaint_mode=gtk_tick_full_overlay_then_settled_full",
            (
                "resize_frame_repaint_quiet_ms="
                f"{RESIZE_FRAME_REPAINT_QUIET_MS}"
            ),
            (
                "resize_frame_repaint_min_ticks="
                f"{RESIZE_FRAME_REPAINT_MIN_TICKS}"
            ),
            (
                "resize_frame_repaint_retry_delay_ms="
                f"{RESIZE_FRAME_REPAINT_RETRY_DELAY_MS}"
            ),
            (
                "resize_frame_repaint_max_settled_attempts="
                f"{RESIZE_FRAME_REPAINT_MAX_SETTLED_ATTEMPTS}"
            ),
            (
                "resize_frame_repaint_pending="
                f"{state.get('resize_frame_repaint_tick_callback_id') is not None}"
            ),
            (
                "resize_frame_repaint_frames_since_allocation="
                f"{state.get('resize_frame_repaint_frames_since_allocation', 0)}"
            ),
            (
                "resize_frame_repaint_settled_attempt_count="
                f"{state.get('resize_frame_repaint_settled_attempt_count', 0)}"
            ),
            (
                "resize_frame_repaint_last_quiet_ms="
                f"{state.get('resize_frame_repaint_last_quiet_ms', 0.0):.3f}"
            ),
            (
                "resize_frame_repaint_request_count="
                f"{state.get('resize_frame_repaint_request_count', 0)}"
            ),
            (
                "resize_frame_repaint_install_count="
                f"{state.get('resize_frame_repaint_install_count', 0)}"
            ),
            (
                "resize_frame_repaint_coalesced_count="
                f"{state.get('resize_frame_repaint_coalesced_count', 0)}"
            ),
            (
                "resize_frame_repaint_tick_count="
                f"{state.get('resize_frame_repaint_tick_count', 0)}"
            ),
            (
                "resize_frame_repaint_full_overlay_queue_count="
                f"{state.get('resize_frame_repaint_full_overlay_queue_count', 0)}"
            ),
            (
                "resize_frame_repaint_full_overlay_skip_count="
                f"{state.get('resize_frame_repaint_full_overlay_skip_count', 0)}"
            ),
            (
                "resize_frame_repaint_final_full_count="
                f"{state.get('resize_frame_repaint_final_full_count', 0)}"
            ),
            (
                "resize_frame_repaint_cache_refresh_count="
                f"{state.get('resize_frame_repaint_cache_refresh_count', 0)}"
            ),
            (
                "resize_frame_repaint_coordinate_retry_failure_count="
                f"{state.get('resize_frame_repaint_coordinate_retry_failure_count', 0)}"
            ),
            (
                "resize_frame_repaint_cancel_count="
                f"{state.get('resize_frame_repaint_cancel_count', 0)}"
            ),
            (
                "resize_frame_repaint_skip_inactive_count="
                f"{state.get('resize_frame_repaint_skip_inactive_count', 0)}"
            ),
            (
                "resize_frame_repaint_failure_count="
                f"{state.get('resize_frame_repaint_failure_count', 0)}"
            ),
            (
                "last_resize_frame_repaint_error="
                f"{state.get('last_resize_frame_repaint_error', '')}"
            ),
            (
                "last_resize_frame_repaint_x="
                f"{state.get('last_resize_frame_repaint_x', 0)}"
            ),
            (
                "last_resize_frame_repaint_y="
                f"{state.get('last_resize_frame_repaint_y', 0)}"
            ),
            (
                "last_resize_frame_repaint_width="
                f"{state.get('last_resize_frame_repaint_width', 0)}"
            ),
            (
                "last_resize_frame_repaint_height="
                f"{state.get('last_resize_frame_repaint_height', 0)}"
            ),
            (
                "resize_event_trace_mode="
                + (
                    "bounded_event_order_diagnostic_only"
                    if RESIZE_EVENT_TRACE_ENABLED
                    else "disabled_production"
                )
            ),
            f"resize_event_trace_path={RESIZE_EVENT_TRACE_PATH}",
            (
                "resize_event_trace_limit="
                f"{RESIZE_EVENT_TRACE_LIMIT}"
            ),
            (
                "resize_event_trace_retained_count="
                f"{len(state.get('resize_event_trace') or ())}"
            ),
            (
                "resize_event_trace_record_count="
                f"{state.get('resize_event_trace_record_count', 0)}"
            ),
            (
                "resize_event_trace_dropped_count="
                f"{state.get('resize_event_trace_dropped_count', 0)}"
            ),
            (
                "resize_event_trace_write_count="
                f"{state.get('resize_event_trace_write_count', 0)}"
            ),
            (
                "resize_event_trace_failure_count="
                f"{state.get('resize_event_trace_failure_count', 0)}"
            ),
            (
                "last_resize_event_trace_error="
                f"{state.get('last_resize_event_trace_error', '')}"
            ),
            "resize_draw_clip_expansion_mode=resize_only_saved_state_bounded_overlay",
            (
                "resize_draw_clip_expansion_check_count="
                f"{state.get('resize_draw_clip_expansion_check_count', 0)}"
            ),
            (
                "resize_draw_clip_expansion_request_count="
                f"{state.get('resize_draw_clip_expansion_request_count', 0)}"
            ),
            (
                "resize_draw_clip_expansion_applied_count="
                f"{state.get('resize_draw_clip_expansion_applied_count', 0)}"
            ),
            (
                "resize_draw_clip_expansion_effective_count="
                f"{state.get('resize_draw_clip_expansion_effective_count', 0)}"
            ),
            (
                "resize_draw_clip_expansion_ineffective_count="
                f"{state.get('resize_draw_clip_expansion_ineffective_count', 0)}"
            ),
            (
                "resize_draw_clip_expansion_complete_skip_count="
                f"{state.get('resize_draw_clip_expansion_complete_skip_count', 0)}"
            ),
            (
                "resize_draw_clip_expansion_failure_count="
                f"{state.get('resize_draw_clip_expansion_failure_count', 0)}"
            ),
            (
                "last_resize_draw_clip_expansion_effective="
                f"{state.get('last_resize_draw_clip_expansion_effective', False)}"
            ),
            (
                "last_resize_draw_clip_expansion_error="
                f"{state.get('last_resize_draw_clip_expansion_error', '')}"
            ),
            (
                "last_resize_draw_clip_expansion_x1="
                f"{state.get('last_resize_draw_clip_expansion_x1', 0.0):.3f}"
            ),
            (
                "last_resize_draw_clip_expansion_y1="
                f"{state.get('last_resize_draw_clip_expansion_y1', 0.0):.3f}"
            ),
            (
                "last_resize_draw_clip_expansion_x2="
                f"{state.get('last_resize_draw_clip_expansion_x2', 0.0):.3f}"
            ),
            (
                "last_resize_draw_clip_expansion_y2="
                f"{state.get('last_resize_draw_clip_expansion_y2', 0.0):.3f}"
            ),
            (
                "last_resize_draw_clip_target_x="
                f"{state.get('last_resize_draw_clip_target_x', 0)}"
            ),
            (
                "last_resize_draw_clip_target_y="
                f"{state.get('last_resize_draw_clip_target_y', 0)}"
            ),
            (
                "last_resize_draw_clip_target_width="
                f"{state.get('last_resize_draw_clip_target_width', 0)}"
            ),
            (
                "last_resize_draw_clip_target_height="
                f"{state.get('last_resize_draw_clip_target_height', 0)}"
            ),
            "resize_reflow_continuity_mode=disabled_current_partition_only",
            (
                "resize_reflow_continuity_enabled="
                f"{RESIZE_REFLOW_CONTINUITY_ENABLED}"
            ),
            "resize_reflow_continuity_previous_frame_segments_drawn=False",
            "resize_reflow_continuity_ghost_cleanup=True",
            (
                "resize_reflow_continuity_max_age_ms="
                f"{RESIZE_REFLOW_CONTINUITY_MAX_AGE_MS}"
            ),
            (
                "resize_reflow_continuity_shift_rows="
                f"{RESIZE_REFLOW_CONTINUITY_SHIFT_ROWS}"
            ),
            (
                "resize_reflow_continuity_tail_rows="
                f"{RESIZE_REFLOW_CONTINUITY_TAIL_ROWS}"
            ),
            "resize_reflow_continuity_current_partition_authoritative=True",
            "resize_reflow_continuity_native_shadow_excluded=True",
            (
                "resize_reflow_continuity_snapshot_available="
                f"{state.get('resize_reflow_continuity_snapshot') is not None}"
            ),
            (
                "resize_reflow_continuity_check_count="
                f"{state.get('resize_reflow_continuity_check_count', 0)}"
            ),
            (
                "resize_reflow_continuity_candidate_count="
                f"{state.get('resize_reflow_continuity_candidate_count', 0)}"
            ),
            (
                "resize_reflow_continuity_applied_count="
                f"{state.get('resize_reflow_continuity_applied_count', 0)}"
            ),
            (
                "resize_reflow_continuity_empty_count="
                f"{state.get('resize_reflow_continuity_empty_count', 0)}"
            ),
            (
                "resize_reflow_continuity_non_resize_skip_count="
                f"{state.get('resize_reflow_continuity_non_resize_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_no_snapshot_skip_count="
                f"{state.get('resize_reflow_continuity_no_snapshot_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_age_skip_count="
                f"{state.get('resize_reflow_continuity_age_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_stable_geometry_skip_count="
                f"{state.get('resize_reflow_continuity_stable_geometry_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_summary_changed_skip_count="
                f"{state.get('resize_reflow_continuity_summary_changed_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_no_shift_skip_count="
                f"{state.get('resize_reflow_continuity_no_shift_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_no_wrap_edge_skip_count="
                f"{state.get('resize_reflow_continuity_no_wrap_edge_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_coordinate_skip_count="
                f"{state.get('resize_reflow_continuity_coordinate_skip_count', 0)}"
            ),
            (
                "resize_reflow_continuity_failure_count="
                f"{state.get('resize_reflow_continuity_failure_count', 0)}"
            ),
            (
                "resize_reflow_continuity_segment_total="
                f"{state.get('resize_reflow_continuity_segment_total', 0)}"
            ),
            (
                "resize_reflow_continuity_ordinary_segment_total="
                f"{state.get('resize_reflow_continuity_ordinary_segment_total', 0)}"
            ),
            (
                "resize_reflow_continuity_command_segment_total="
                f"{state.get('resize_reflow_continuity_command_segment_total', 0)}"
            ),
            (
                "resize_reflow_continuity_line_number_segment_total="
                f"{state.get('resize_reflow_continuity_line_number_segment_total', 0)}"
            ),
            (
                "last_resize_reflow_continuity_error="
                f"{state.get('last_resize_reflow_continuity_error', '')}"
            ),
            (
                "last_resize_reflow_continuity_age_ms="
                f"{state.get('last_resize_reflow_continuity_age_ms', 0.0):.3f}"
            ),
            (
                "last_resize_reflow_continuity_shift_rows="
                f"{state.get('last_resize_reflow_continuity_shift_rows', 0)}"
            ),
            (
                "last_resize_reflow_continuity_previous_columns="
                f"{state.get('last_resize_reflow_continuity_previous_columns', 0)}"
            ),
            (
                "last_resize_reflow_continuity_previous_rows="
                f"{state.get('last_resize_reflow_continuity_previous_rows', 0)}"
            ),
            (
                "last_resize_reflow_continuity_current_columns="
                f"{state.get('last_resize_reflow_continuity_current_columns', 0)}"
            ),
            (
                "last_resize_reflow_continuity_current_rows="
                f"{state.get('last_resize_reflow_continuity_current_rows', 0)}"
            ),
            (
                "last_resize_reflow_continuity_previous_segment_count="
                f"{state.get('last_resize_reflow_continuity_previous_segment_count', 0)}"
            ),
            (
                "last_resize_reflow_continuity_previous_lowest_row="
                f"{state.get('last_resize_reflow_continuity_previous_lowest_row', -1)}"
            ),
            (
                "last_resize_reflow_continuity_current_segment_count="
                f"{state.get('last_resize_reflow_continuity_current_segment_count', 0)}"
            ),
            (
                "last_resize_reflow_continuity_current_lowest_row="
                f"{state.get('last_resize_reflow_continuity_current_lowest_row', -1)}"
            ),
            (
                "last_resize_reflow_continuity_output_count="
                f"{state.get('last_resize_reflow_continuity_output_count', 0)}"
            ),
            (
                "last_resize_reflow_continuity_source_tail_count="
                f"{state.get('last_resize_reflow_continuity_source_tail_count', 0)}"
            ),
            "visible_row_overscan_mode=bounded_allocated_content_height",
            (
                "visible_row_overscan_count="
                f"{VISIBLE_ROW_OVERSCAN_COUNT}"
            ),
            (
                "visible_row_overscan_draw_count="
                f"{state.get('visible_row_overscan_draw_count', 0)}"
            ),
            (
                "visible_row_overscan_segment_total="
                f"{state.get('visible_row_overscan_segment_total', 0)}"
            ),
            (
                "visible_row_overscan_last_segment_count="
                f"{state.get('visible_row_overscan_last_segment_count', 0)}"
            ),
            (
                "visible_row_overscan_last_extension_height="
                f"{state.get('visible_row_overscan_last_extension_height', 0)}"
            ),
            (
                "visible_row_overscan_max_extension_height="
                f"{state.get('visible_row_overscan_max_extension_height', 0)}"
            ),
            (
                "visible_row_overscan_last_lowest_display_row="
                f"{state.get('visible_row_overscan_last_lowest_display_row', -1)}"
            ),
            (
                "bottom_damage_recovery_delay_ms="
                f"{BOTTOM_DAMAGE_RECOVERY_DELAY_MS}"
            ),
            "bottom_damage_probe_grid=3x3_visible_band",
            (
                "bottom_damage_probe_point_count="
                f"{BOTTOM_DAMAGE_PROBE_POINT_COUNT}"
            ),
            (
                "bottom_damage_repaint_pending="
                f"{state.get('bottom_damage_repaint_source_id') is not None}"
            ),
            (
                "bottom_damage_repaint_request_count="
                f"{state.get('bottom_damage_repaint_request_count', 0)}"
            ),
            (
                "bottom_damage_repaint_schedule_count="
                f"{state.get('bottom_damage_repaint_schedule_count', 0)}"
            ),
            (
                "bottom_damage_repaint_coalesced_count="
                f"{state.get('bottom_damage_repaint_coalesced_count', 0)}"
            ),
            (
                "bottom_damage_repaint_full_count="
                f"{state.get('bottom_damage_repaint_full_count', 0)}"
            ),
            (
                "bottom_damage_repaint_skip_inactive_count="
                f"{state.get('bottom_damage_repaint_skip_inactive_count', 0)}"
            ),
            (
                "bottom_damage_repaint_failure_count="
                f"{state.get('bottom_damage_repaint_failure_count', 0)}"
            ),
            (
                "last_bottom_damage_repaint_error="
                f"{state.get('last_bottom_damage_repaint_error', '')}"
            ),
            "resize_damage_clip_probe=classification_and_recovery_trigger",
            (
                "size_allocate_event_count="
                f"{state.get('size_allocate_event_count', 0)}"
            ),
            (
                "draw_damage_clip_probe_count="
                f"{state.get('draw_damage_clip_probe_count', 0)}"
            ),
            (
                "draw_damage_clip_probe_failure_count="
                f"{state.get('draw_damage_clip_probe_failure_count', 0)}"
            ),
            (
                "last_draw_damage_clip_error="
                f"{state.get('last_draw_damage_clip_error', '')}"
            ),
            (
                "last_draw_clip_x1="
                f"{state.get('last_draw_clip_x1', 0.0):.3f}"
            ),
            (
                "last_draw_clip_y1="
                f"{state.get('last_draw_clip_y1', 0.0):.3f}"
            ),
            (
                "last_draw_clip_x2="
                f"{state.get('last_draw_clip_x2', 0.0):.3f}"
            ),
            (
                "last_draw_clip_y2="
                f"{state.get('last_draw_clip_y2', 0.0):.3f}"
            ),
            (
                "last_draw_clip_resize_pending="
                f"{state.get('last_draw_clip_resize_pending', False)}"
            ),
            (
                "last_bottom_probe_absolute_row="
                f"{state.get('last_bottom_probe_absolute_row', -1)}"
            ),
            (
                "last_bottom_probe_display_row="
                f"{state.get('last_bottom_probe_display_row', -1)}"
            ),
            (
                "last_bottom_probe_x="
                f"{state.get('last_bottom_probe_x', 0)}"
            ),
            (
                "last_bottom_probe_y="
                f"{state.get('last_bottom_probe_y', 0)}"
            ),
            (
                "last_bottom_probe_width="
                f"{state.get('last_bottom_probe_width', 0)}"
            ),
            (
                "last_bottom_probe_height="
                f"{state.get('last_bottom_probe_height', 0)}"
            ),
            (
                "last_bottom_probe_visible_x1="
                f"{state.get('last_bottom_probe_visible_x1', 0.0):.3f}"
            ),
            (
                "last_bottom_probe_visible_y1="
                f"{state.get('last_bottom_probe_visible_y1', 0.0):.3f}"
            ),
            (
                "last_bottom_probe_visible_x2="
                f"{state.get('last_bottom_probe_visible_x2', 0.0):.3f}"
            ),
            (
                "last_bottom_probe_visible_y2="
                f"{state.get('last_bottom_probe_visible_y2', 0.0):.3f}"
            ),
            (
                "last_bottom_probe_expected_point_count="
                f"{state.get('last_bottom_probe_expected_point_count', BOTTOM_DAMAGE_PROBE_POINT_COUNT)}"
            ),
            (
                "last_bottom_probe_points_in_clip="
                f"{state.get('last_bottom_probe_points_in_clip', 0)}"
            ),
            (
                "last_bottom_probe_top_points_in_clip="
                f"{state.get('last_bottom_probe_top_points_in_clip', 0)}"
            ),
            (
                "last_bottom_probe_middle_points_in_clip="
                f"{state.get('last_bottom_probe_middle_points_in_clip', 0)}"
            ),
            (
                "last_bottom_probe_bottom_points_in_clip="
                f"{state.get('last_bottom_probe_bottom_points_in_clip', 0)}"
            ),
            (
                "bottom_probe_no_segment_count="
                f"{state.get('bottom_probe_no_segment_count', 0)}"
            ),
            (
                "bottom_probe_full_clip_count="
                f"{state.get('bottom_probe_full_clip_count', 0)}"
            ),
            (
                "bottom_probe_partial_clip_count="
                f"{state.get('bottom_probe_partial_clip_count', 0)}"
            ),
            (
                "bottom_probe_excluded_count="
                f"{state.get('bottom_probe_excluded_count', 0)}"
            ),
            (
                "resize_draw_count="
                f"{state.get('resize_draw_count', 0)}"
            ),
            (
                "resize_bottom_full_clip_count="
                f"{state.get('resize_bottom_full_clip_count', 0)}"
            ),
            (
                "resize_bottom_partial_clip_count="
                f"{state.get('resize_bottom_partial_clip_count', 0)}"
            ),
            (
                "resize_bottom_excluded_count="
                f"{state.get('resize_bottom_excluded_count', 0)}"
            ),
            (
                "resize_bottom_incomplete_streak="
                f"{state.get('resize_bottom_incomplete_streak', 0)}"
            ),
            (
                "resize_bottom_incomplete_max_streak="
                f"{state.get('resize_bottom_incomplete_max_streak', 0)}"
            ),
            (
                "last_draw_cache_miss_count="
                f"{state.get('last_draw_cache_miss_count', 0)}"
            ),
            (
                "draw_cache_miss_frame_count="
                f"{state.get('draw_cache_miss_frame_count', 0)}"
            ),
            (
                "draw_cache_miss_row_total="
                f"{state.get('draw_cache_miss_row_total', 0)}"
            ),
            (
                "resize_draw_cache_miss_frame_count="
                f"{state.get('resize_draw_cache_miss_frame_count', 0)}"
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
                f"copy_source_generation={state.get('copy_source_generation', 0)}",
                f"copy_snapshot_available={state.get('copy_snapshot') is not None}",
                f"copy_snapshot_pending={state.get('copy_snapshot_source_id') is not None}",
                f"copy_snapshot_invalidation_count={state.get('copy_snapshot_invalidation_count', 0)}",
                f"copy_snapshot_prepare_request_count={state.get('copy_snapshot_prepare_request_count', 0)}",
                f"copy_snapshot_prepare_count={state.get('copy_snapshot_prepare_count', 0)}",
                f"copy_snapshot_prepare_failure_count={state.get('copy_snapshot_prepare_failure_count', 0)}",
                f"copy_snapshot_prepare_discard_count={state.get('copy_snapshot_prepare_discard_count', 0)}",
                f"copy_snapshot_oversize_count={state.get('copy_snapshot_oversize_count', 0)}",
                f"copy_snapshot_last_estimated_character_count={state.get('copy_snapshot_last_estimated_character_count', 0)}",
                f"copy_snapshot_hit_count={state.get('copy_snapshot_hit_count', 0)}",
                f"copy_snapshot_miss_count={state.get('copy_snapshot_miss_count', 0)}",
                f"copy_snapshot_same_click_seed_count={state.get('copy_snapshot_same_click_seed_count', 0)}",
                f"copy_coordinate_certificate_hit_count={state.get('copy_coordinate_certificate_hit_count', 0)}",
                f"copy_coordinate_certificate_miss_count={state.get('copy_coordinate_certificate_miss_count', 0)}",
                f"copy_transaction_count={state.get('copy_transaction_count', 0)}",
                f"copy_transaction_attempt_count={state.get('copy_transaction_attempt_count', 0)}",
                f"copy_transaction_retry_count={state.get('copy_transaction_retry_count', 0)}",
                f"copy_transaction_failure_count={state.get('copy_transaction_failure_count', 0)}",
                f"copy_transaction_last_status={state.get('copy_transaction_last_status', 'not_run')}",
                f"copy_transaction_last_path={state.get('copy_transaction_last_path', 'none')}",
                f"copy_transaction_last_invalidation_reason={state.get('copy_transaction_last_invalidation_reason', 'none')}",
                f"copy_transaction_last_ms={state.get('copy_transaction_last_total_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_transaction_average_ms={state.get('copy_transaction_total_ns', 0) / max(state.get('copy_transaction_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_transaction_max_ms={state.get('copy_transaction_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_transaction_last_coordinate_ms={state.get('copy_transaction_last_coordinate_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_transaction_last_extract_ms={state.get('copy_transaction_last_extract_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_ack_last_ms={state.get('copy_ack_last_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_ack_average_ms={state.get('copy_ack_total_ns', 0) / max(state.get('copy_count', 0), 1) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_ack_max_ms={state.get('copy_ack_max_ns', 0) / NANOSECONDS_PER_MILLISECOND:.6f}",
                f"copy_clipboard_set_count={state.get('copy_clipboard_set_count', 0)}",
                f"copy_clipboard_store_pending={state.get('copy_clipboard_store_pending', False)}",
                f"copy_clipboard_store_request_count={state.get('copy_clipboard_store_request_count', 0)}",
                f"copy_clipboard_store_success_count={state.get('copy_clipboard_store_success_count', 0)}",
                f"copy_clipboard_store_failure_count={state.get('copy_clipboard_store_failure_count', 0)}",
                f"copy_clipboard_store_last_error={state.get('copy_clipboard_store_last_error', '')}",
                f"copy_confirmation_show_count={state.get('copy_confirmation_show_count', 0)}",
                f"copy_confirmation_clear_count={state.get('copy_confirmation_clear_count', 0)}",
                f"copy_confirmation_draw_count={state.get('copy_confirmation_draw_count', 0)}",
                f"copy_confirmation_failure_count={state.get('copy_confirmation_failure_count', 0)}",
                f"copy_confirmation_last_error={state.get('copy_confirmation_last_error', '')}",
            ],
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
                "current_overscan_segment_count="
                f"{diagnostic_overscan_segment_count}"
            ),
            (
                "current_lowest_display_row="
                f"{diagnostic_lowest_display_row}"
            ),
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
            f"x={overlay_clip['x']}",
            f"y={overlay_clip['y']}",
            f"width={overlay_clip['width']}",
            f"height={overlay_clip['height']}",
            f"base_grid_height={geometry['height']}",
            (
                "overlay_clip_extension_height="
                f"{overlay_clip['extension_height']}"
            ),
            (
                "overlay_allocated_content_bottom="
                f"{overlay_clip['allocated_content_bottom']}"
            ),
            (
                "overlay_overscan_limit_bottom="
                f"{overlay_clip['overscan_limit_bottom']}"
            ),
            f"remaining_width={geometry['remaining_width']}",
            f"remaining_height={geometry['remaining_height']}",
            f"right_remainder={geometry['right_remainder']}",
            f"bottom_remainder={geometry['bottom_remainder']}",
        ]

        LOG_PATH.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
