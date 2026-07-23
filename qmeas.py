"""
qmeas.py  –  qmeas v2 (dockable-panel GUI).

Dockable panels (wx.aui), v2 palette from qmeas_theme.py. Devices &
Commands tree and Tasks grid are both real, loaded from .dev files on
disk (no demo device). Double-click a write-type command to append it
to the Tasks grid (native drag-and-drop was tried and repeatedly
crashed wx on Windows — see DevicesPanel docstring — dropped in favor
of this); double-click a device to load and edit its VISA settings
(DeviceEditorPanel.load_existing_device). Real VISA I/O exists for:
constant-value row execution via Start/TaskRunnerThread (v1 manual:
'constant' means no data acquisition — queries do NOT run after a
plain write, only a Steps>=1 sweep does), a REAL device's Start/Final/
Steps sweep (Steps>=1) with per-step read-back, and control_counter as
the virtual (no real device) version of the same engine — see
TaskRunnerThread._run_timed_sweep, which handles both. Real-device
writes (constant or per-step) use ONE of three mutually exclusive
Methods, set per command (CommandEditorDialog, fields[13-16] — see
_get_command_method): 'none' (default) jumps directly, unchanged from
before any of this existed; 'ramp' breaks the move into 1-unit-of-time
sub-steps at a set rate (_write_with_rate_limit); 'verify' jumps
directly, then polls a linked read once a second until its value
exactly equals a configured string (_verify_equals — failed reads are
retried up to the device's own timeout before failing the row; the
match wait is capped at a PER-COMMAND timeout, fields[17], defaulting
to _VERIFY_MATCH_TIMEOUT_S when absent, with 0 meaning no cap at all —
a 14T->0T move at 0.1T/min is 140 minutes of correct behavior; and a
verify failure of EITHER kind now aborts the whole run through the
central onhold path rather than skipping the row and marching on).
'verify'
is what matters for a superconducting magnet, whose controller keeps
ramping internally long after the write call returns — you don't
software-step it (that's what its own internal ramp is for), you wait
for it to report a status like 'HOLD'. Any
active device with a write command literally named 'onhold' gets it
sent immediately on abort (qmeas v1.1 precedent) — stopping this
software's writes doesn't stop a magnet controller's own in-progress
ramp. Nested rows (depth>0, strictly increasing down a chain) now
execute as genuine nested for-loops — see TaskRunnerThread.
_run_nested_sweep. control_userprompt shows a modal 'Continue'/'Abort'
popup and blocks until answered; control_stop acts exactly like a
manual Stop click (same onhold-on-abort handling). A second virtual
device, 'script' (off by default, unlike 'control'), lists every .py
file in custom_dir() and runs one via a plain exec() when double-
clicked into Tasks — no sandboxing, no validation, an exception just
fails that row like any other write failure (requirement: 'qmeas doesnt check
anything. It executes... It is the users responsibility to ensure
operation'). Plus Test Once/Query Once/Test Connection. Linked rows
(same-depth rows boxed together via the Link context menu item)
execute as followers of the box's top row (the 'mother'/anchor): at
every step of the mother's sweep — after the mother has fully reached
that step's target, ramp/verify included — each follower evaluates the
expression in its Constant/Start cell with [%] standing for the
mother's commanded setpoint (e.g. '[%]*5', 'sin([%])', 'exp(-[%]^2)')
and writes the result, top-to-bottom; only then does Integration Time
run once, followed by the active reads. Every follower references the
MOTHER — never a preceding follower. v1 restriction: linking and
nesting don't combine (link boxes are depth-0 only).

    Devices & Commands pane  -> device tree + per-device command aliases
                                 (was: v1 self.tree_devices)
    Tasks pane                -> task/command grid (was: v1 self.grid)
    Log pane                  -> log output (was: v1 self.AddToLog)
    Graph                     -> separate top-level window (not docked,
                                 by design — meant to live on a second
                                 monitor during a run), toggled from
                                 the View menu like the docked panes.

The fully working v1 application (all measurement/device/VISA logic) is
preserved unchanged in qmeas_v1_legacy.py.
"""

import faulthandler
faulthandler.enable()   # print a native stack trace on segfault/access-violation
                        # instead of the process silently vanishing

from pathlib import Path
import http.client
import json
import math
import re
import socket
import threading
import time
import wx
import wx.aui as aui
import wx.grid

# Optional: only GraphWindow needs this. Import failures here must
# never take down device control/task execution — if matplotlib isn't
# installed, GraphWindow falls back to a plain message instead of
# crashing the whole app at startup.
try:
    import matplotlib
    matplotlib.use('WXAgg')
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg
    from matplotlib.backends.backend_wx import NavigationToolbar2Wx

    class _GraphToolbar(NavigationToolbar2Wx):
        """Same as NavigationToolbar2Wx, except Home also re-enables
        autoscale on every current axes. Needed because GraphWindow
        rebuilds the figure from scratch (figure.clear() + new
        add_subplot()) on every live data update — plain Home only
        restores the ORIGINAL view on the axes that exist right now,
        it doesn't know those axes are about to be thrown away and
        rebuilt the next time new data arrives. Without this, a manual
        zoom (which matplotlib marks by calling set_xlim/set_ylim,
        which as a documented side effect turns autoscale OFF for that
        axis) would stay 'stuck' — GraphWindow._capture_view checks
        exactly this autoscale flag to decide whether to preserve the
        current view across a redraw, so Home needs to explicitly flip
        it back on to mean 'go back to auto-scaling live data',  not
        just 'jump back to the first view I ever had'."""
        def home(self, *args):
            super().home(*args)
            for ax in self.canvas.figure.axes:
                ax.set_autoscale_on(True)

    _HAS_MATPLOTLIB = True
except Exception:
    _HAS_MATPLOTLIB = False

from qmeas_theme import (
    COLOR_APP_BG, COLOR_PANEL_BG, COLOR_BORDER, COLOR_ACTIVE_ROW, COLOR_PASSED_OVER,
    COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_ACCENT,
    COLOR_STATUS_IDLE, COLOR_LED_OFF, COLOR_STATUS_ERROR,
    COLOR_AUI_CAPTION_ACTIVE, COLOR_AUI_CAPTION_ACTIVE_TEXT,
    COLOR_AUI_CAPTION_INACTIVE, COLOR_AUI_CAPTION_INACTIVE_TEXT,
    COLOR_AUI_SASH, COLOR_AUI_BORDER,
    PAD, PAD_SMALL, PAD_LARGE,
)
from qmeas_settings import load_settings, save_settings
from qmeas_utils import get_resource_manager, dev_dir, custom_dir, img, app_root, open_file_cross_platform, open_text_editor

APP_NAME    = 'qmeas'
APP_VERSION = '2.0'

LED_DIAMETER = 12

COLOR_CELL_INVALID = wx.Colour(244, 200, 196)   # light red — grid-cell background for
                                                # inconsistent/unparseable sweep fields
                                                # (TasksPanel._validate_row_fields). A
                                                # VISUAL guard only: execution is
                                                # deliberately untouched (requirement: 'I dont
                                                # want too much meddling with the
                                                # execution. Everything must happen in
                                                # the grid.')


def _make_led_bitmap(fill_color, diameter=LED_DIAMETER, bg_color=COLOR_PANEL_BG):
    """Render a small filled-circle bitmap for use as a TreeCtrl item icon
    (device LED indicator). Opaque background matching the tree's panel
    color — simpler and more portable than fighting per-platform alpha
    handling for a 12px icon."""
    bmp = wx.Bitmap(diameter, diameter)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(bg_color))
    dc.Clear()
    dc.SetBrush(wx.Brush(fill_color))
    dc.SetPen(wx.Pen(COLOR_BORDER))
    dc.DrawEllipse(1, 1, diameter - 2, diameter - 2)
    dc.SelectObject(wx.NullBitmap)
    return bmp


def _make_arrow_bitmap(direction, color=COLOR_ACCENT, size=LED_DIAMETER,
                       bg_color=COLOR_PANEL_BG):
    """Render a small triangle bitmap indicating command direction:
    direction='in'  -> points left  (query/read: data comes back from device)
    direction='out' -> points right (write: command goes out to device)
    """
    bmp = wx.Bitmap(size, size)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(bg_color))
    dc.Clear()
    dc.SetBrush(wx.Brush(color))
    dc.SetPen(wx.Pen(color))
    m, mid = 2, size // 2
    if direction == 'out':
        points = [wx.Point(m, m), wx.Point(m, size - m), wx.Point(size - m, mid)]
    else:
        points = [wx.Point(size - m, m), wx.Point(size - m, size - m), wx.Point(m, mid)]
    dc.DrawPolygon(points)
    dc.SelectObject(wx.NullBitmap)
    return bmp


class LedCellRenderer(wx.grid.GridCellRenderer):
    """Draws a green/grey LED (same bitmaps as the Devices tree) instead
    of a native checkbox, for the Tasks grid's 'On' column. Cell value is
    '1' (on) or '0'/'' (off). Toggling is click-to-toggle, handled by
    TasksPanel — this class only draws."""

    def __init__(self):
        super().__init__()
        self._bmp_on  = _make_led_bitmap(COLOR_STATUS_IDLE)
        self._bmp_off = _make_led_bitmap(COLOR_LED_OFF)

    def Draw(self, grid, attr, dc, rect, row, col, isSelected):
        dc.SetClippingRegion(rect)
        bg = grid.GetSelectionBackground() if isSelected else attr.GetBackgroundColour()
        dc.SetBrush(wx.Brush(bg))
        dc.SetPen(wx.Pen(bg))
        dc.DrawRectangle(rect)
        bmp = self._bmp_on if grid.GetCellValue(row, col) == '1' else self._bmp_off
        bw, bh = bmp.GetSize()
        dc.DrawBitmap(bmp, rect.x + (rect.width - bw) // 2,
                          rect.y + (rect.height - bh) // 2, True)
        dc.DestroyClippingRegion()

    def GetBestSize(self, grid, attr, dc, row, col):
        return wx.Size(LED_DIAMETER + 8, LED_DIAMETER + 8)

    def Clone(self):
        return LedCellRenderer()


class CommandCellRenderer(wx.grid.GridCellRenderer):
    """Draws the Command column's text, indented per the row's nesting
    depth (read from the hidden 'structure' column: 'depth:linked'),
    with thin vertical guide lines per indent level — same visual
    language as a code editor's indent guides.

    Linked rows get a black box outline — but spanning the WHOLE chain
    (this row plus the row(s) it's linked to, up to MAX_LINK_CHAIN),
    drawn as one continuous box rather than a separate box per row. Each
    row in the chain draws only the border segments that belong to its
    position: the top row draws top+left+right, middle rows draw only
    left+right, the bottom row draws left+right+bottom — so the grid's
    own row-separator lines don't visually cut the box into pieces."""

    def __init__(self, struct_col: int, indent_px: int):
        super().__init__()
        self._struct_col = struct_col
        self._indent_px = indent_px

    def _read_struct(self, grid, row):
        raw = grid.GetCellValue(row, self._struct_col)
        depth_str, _, linked_str = raw.partition(':')
        try:
            depth = int(depth_str)
        except ValueError:
            depth = 0
        return depth, linked_str == 'linked'

    def _chain_segment(self, grid, row):
        """Returns None (not in any chain) or one of 'top'/'middle'/'bottom'
        describing which border segments this row should draw. A row is
        in a chain if it's linked to the row above, OR the row below is
        linked to it (making this row the chain's anchor)."""
        depth, linked = self._read_struct(grid, row)
        n = grid.GetNumberRows()
        below_depth, below_linked = self._read_struct(grid, row + 1) if row + 1 < n else (None, False)
        is_anchor_top = below_linked and below_depth == depth   # next row links to this one
        if not linked and not is_anchor_top:
            return None
        has_above = linked   # this row links to the row above -> not the top of the box
        has_below = is_anchor_top or (linked and below_linked and below_depth == depth)
        if not has_above and has_below:
            return 'top'
        if has_above and has_below:
            return 'middle'
        return 'bottom'   # has_above and not has_below (or solo edge case)

    def Draw(self, grid, attr, dc, rect, row, col, isSelected):
        dc.SetClippingRegion(rect)
        bg = grid.GetSelectionBackground() if isSelected else attr.GetBackgroundColour()
        dc.SetBrush(wx.Brush(bg))
        dc.SetPen(wx.Pen(bg))
        dc.DrawRectangle(rect)

        depth, linked = self._read_struct(grid, row)
        indent = depth * self._indent_px

        if depth > 0:
            dc.SetPen(wx.Pen(COLOR_BORDER, 1))
            for level in range(depth):
                x = rect.x + 4 + level * self._indent_px
                dc.DrawLine(x, rect.y, x, rect.y + rect.height)

        dc.SetTextForeground(grid.GetSelectionForeground() if isSelected
                             else attr.GetTextColour())
        dc.SetFont(attr.GetFont())
        text = grid.GetCellValue(row, col)
        tw, th = dc.GetTextExtent(text)
        dc.DrawText(text, rect.x + indent + 6, rect.y + (rect.height - th) // 2)

        segment = self._chain_segment(grid, row)
        if segment is not None:
            dc.SetPen(wx.Pen(wx.BLACK, 1))
            x0, y0 = rect.x + 1, rect.y + 1
            x1, y1 = rect.x + rect.width - 2, rect.y + rect.height - 2
            dc.DrawLine(x0, y0, x0, y1)          # left
            dc.DrawLine(x1, y0, x1, y1)          # right
            if segment in ('top',):
                dc.DrawLine(x0, y0, x1, y0)       # top border
            if segment in ('bottom',):
                dc.DrawLine(x0, y1, x1, y1)       # bottom border

        dc.DestroyClippingRegion()

    def GetBestSize(self, grid, attr, dc, row, col):
        return wx.Size(180, 20)

    def Clone(self):
        return CommandCellRenderer(self._struct_col, self._indent_px)


class _DropIndicator(wx.Window):
    """Thin horizontal line shown over the grid while dragging a row.
    Ported from v1's sub_grid.py (same proven mechanism)."""

    def __init__(self, parent: wx.Window):
        super().__init__(parent, style=wx.TRANSPARENT_WINDOW | wx.NO_BORDER)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Hide()

    def _on_paint(self, _evt):
        dc = wx.PaintDC(self)
        dc.SetPen(wx.Pen(COLOR_ACCENT, 2))
        w, h = self.GetSize()
        dc.DrawLine(0, h // 2, w, h // 2)

    def place(self, y_grid: int):
        pw, _ = self.GetParent().GetSize()
        self.SetSize(0, y_grid - 1, pw, 4)
        self.Show()
        self.Raise()

    def hide(self):
        self.Hide()


_INTTIME_RE = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([sSmMhH]?)\s*$')

# Shared between command names and device aliases — both get used inside
# the same "{device}_{command}" alias construction, so both need the
# same restriction. Lowercase a-z and 0-9 only: no spaces, no
# underscores, no uppercase, no punctuation. An earlier version only
# checked for whitespace, which let everything else (uppercase,
# underscores, symbols) slip through — this is the actual full fix.
_NAME_RE = re.compile(r'^[a-z0-9]+$')

_ALLOWED_NAME_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789'


def _bind_name_filter(ctrl: wx.TextCtrl):
    """Live filtering, not validate-then-reject: as the value changes
    (typing OR pasting — EVT_TEXT fires either way), strip anything not
    in _ALLOWED_NAME_CHARS and lowercase the rest, immediately. Matches
    v1's actual sanitization rule (OnEnterNewDeviceName: strip+lowercase)
    but applied live rather than only once on dialog OK.

    Uses ChangeValue(), not SetValue() — SetValue() re-fires EVT_TEXT,
    which would recurse; ChangeValue() updates the control silently."""
    def on_text(event):
        raw = ctrl.GetValue()
        clean = ''.join(c for c in raw.lower() if c in _ALLOWED_NAME_CHARS)
        if clean != raw:
            pos = ctrl.GetInsertionPoint()
            shift = len(raw) - len(clean)
            ctrl.ChangeValue(clean)
            ctrl.SetInsertionPoint(max(0, min(pos - shift, len(clean))))
        event.Skip()
    ctrl.Bind(wx.EVT_TEXT, on_text)


class NameEntryDialog(wx.Dialog):
    """Single live-filtered name field + OK/Cancel — used for Rename
    Device, so the same live filtering applies there too, not just in
    the Add dialogs."""

    def __init__(self, parent, title, prompt, initial_value=''):
        super().__init__(parent, title=title, size=wx.Size(340, 150))
        label = wx.StaticText(self, label=prompt)
        self.text_ctrl = wx.TextCtrl(self, value=initial_value)
        _bind_name_filter(self.text_ctrl)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(label, 0, wx.ALL, self.FromDIP(PAD_LARGE))
        main.Add(self.text_ctrl, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(PAD_LARGE))
        main.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        self.SetSizerAndFit(main)

    def get_value(self) -> str:
        return self.text_ctrl.GetValue()

# Matches TasksPanel.COL_* order exactly — used for human-readable JSON
# save/load, so a saved file's field names remain meaningful even if
# someone opens it outside qmeas.
TASK_COLUMN_NAMES = ['on', 'struct', 'command', 'start', 'final',
                     'steps', 'inttime', 'cond', 'comment', 'completed']


def _normalize_integration_time(val: str) -> str:
    """'5' -> '5s' (bare number defaults to seconds). '2m'/'1H' -> '2m'/'1h'
    (explicit s/m/h kept, case-normalized). BLANK stays blank — deleting
    a time constant must actually clear the cell (requirement: 'You always fill
    with 0s. It needs to be possible to clear it entirely'); the sweep
    engines treat blank as 0.0 anyway, so nothing downstream changes.
    Non-blank garbage (wrong suffix, multiple values) -> '0s' — but note
    the only caller that normalizes on edit (_on_cell_changed) preserves
    raw garbage un-normalized on non-while rows so validation can paint
    it, so this fallback is effectively the while-restore path's only."""
    if not val.strip():
        return ''
    m = _INTTIME_RE.match(val)
    if not m:
        return '0s'
    number, suffix = m.group(1), m.group(2).lower()
    return f'{number}{suffix or "s"}'


class PlaceholderPanel(wx.Panel):
    """Empty content pane with a centered muted label. Stand-in for a
    pane's real content until that pane is implemented."""

    def __init__(self, parent, label):
        super().__init__(parent)
        self.SetBackgroundColour(COLOR_PANEL_BG)

        text = wx.StaticText(self, label=label, style=wx.ALIGN_CENTER)
        text.SetForegroundColour(COLOR_TEXT_MUTED)
        font = text.GetFont()
        font.PointSize += 1
        text.SetFont(font)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddStretchSpacer()
        sizer.Add(text, 0, wx.ALIGN_CENTER | wx.ALL, self.FromDIP(PAD_LARGE))
        sizer.AddStretchSpacer()
        self.SetSizer(sizer)


# =========================================================================
# Command editor — add one command to a device
# =========================================================================

def _read_device_settings(stem, device_name):
    """Read {device_name}.txt (written by DeviceEditorPanel): address,
    write/read terminator selections, timeout, and serial baud/databits
    if present. Same file, same format, read back for 'Query Once'."""
    path = stem / f'{device_name}.txt'
    lines = path.read_text().splitlines()
    addr = lines[0]
    wterm_sel = int(lines[1]) if len(lines) > 1 else 0
    rterm_sel = int(lines[2]) if len(lines) > 2 else 0
    timeout = float(lines[3]) if len(lines) > 3 else 5000.0
    baud_sel = int(lines[4]) if len(lines) > 4 else 2
    bits_sel = int(lines[5]) if len(lines) > 5 else 3
    return addr, wterm_sel, rterm_sel, timeout, baud_sel, bits_sel


_LAST_VISA_TIME = {}   # device address -> time.time() of its last VISA op's completion —
                       # see _throttle_visa_wait/_throttle_visa_mark
_MIN_VISA_INTERVAL = 0.1   # seconds — requirement: 'I always had to incorporate an at least 100ms
                          # delay between sending commands to the same device'
_CHAIN_SEGMENT_DELAY = 0.2   # seconds, between adjacent && write segments — requested:
                            # 250ms in one message and 200ms in a fuller follow-up describing
                            # the same fix; using the more detailed message's number (200ms).
                            # Flag if you actually meant 250 — one-line change either way.

_SOCKET_WRITE_DRAIN_S = 0.1   # seconds — after a ::SOCKET write, wait up to this long for a
                              # response and read it before closing. The Mercury iPS answers
                              # EVERY command, including SETs (a 'STAT:...' confirmation);
                              # closing a TCP socket with unread data in its receive buffer
                              # sends RST instead of FIN, and hammering the instrument's
                              # (single-client) TCP server with RSTs is the standing best
                              # explanation for the intermittent WinError 10054 on the very
                              # next connection (verify poll #1, right after a chained write).
                              # Trade-off: a device that DOESN'T echo writes (QDAC-II,
                              # Keithley SCPI) eats this as pure added latency per socket
                              # write, so keep it short — a LAN response arrives in
                              # milliseconds. Set to 0 to effectively disable (non-blocking
                              # peek: still drains a response that's already arrived).
_VERIFY_READ_RETRY_INTERVAL_S = 1.0   # seconds between retries of a FAILED verify read
_ACTIVE_READ_RETRY_DELAY_S = 0.15   # seconds before the single retry of a failed ACTIVE
                                    # (logged) query in _execute_active_queries. Long enough
                                    # for a transient socket reset to clear (the observed
                                    # WinError 10054 bursts recover within one poll cycle),
                                    # short enough not to distort per-step timing visibly.
_VERIFY_MATCH_TIMEOUT_S = 600.0   # seconds — verify's value-match wait is no longer
                                  # unbounded. after a diagnosed 'stall' that was
                                  # verify polling a magnet that never reported HOLD:
                                  # 'Ignore my "I'd rather have it hang"' — so it now fails
                                  # loudly (with the last read value in the message, which
                                  # tells you WHY it never matched) instead of waiting
                                  # forever. 600s default: generous for stepped field sweeps
                                  # (observed logs show ~3min/step worst case) but finite.
                                  # NO LONGER a hard cap: this is the DEFAULT, used when a
                                  # command's own verify-timeout field (fields[17]) is absent
                                  # — which is every command line written before that field
                                  # existed, so all pre-existing device files keep exactly
                                  # this behavior. Per-command override via the Command
                                  # Editor ('Verify timeout'); 0 there means no timeout at
                                  # all (poll until match or abort — safe because the verify
                                  # loop checks the abort flag every poll, and the user's
                                  # 14T->0T at 0.1T/min = 140min move is a legitimate,
                                  # correctly-behaving wait that no fixed cap can cover).


def _get_verify_timeout_s(stem, device_name: str, command_name: str) -> float:
    """Per-command verify match timeout in seconds, from the write
    command's OWN line, appended field 17 (right after the verify
    cluster method/linked/rate/equals at 13-16). Returns:
      - _VERIFY_MATCH_TIMEOUT_S (600) if the field is absent, empty,
        'free', or unparseable — i.e. every command line saved before
        this field existed behaves exactly as before, no file
        migration needed on any machine (the Command Editor appends
        the field automatically whenever a command is next edited and
        saved there);
      - 0.0 for an explicit '0' — NO timeout, poll until match or
        abort (for controller-paced moves that legitimately take
        hours: a 14T->0T ramp at 0.1T/min is 140 minutes of correct
        behavior);
      - the parsed positive value otherwise.
    Negative values are treated as the default (a negative timeout has
    no meaning here); garbage never crashes a run over a malformed
    field."""
    try:
        fields = _find_command_fields(stem, device_name, command_name)
        raw = fields[17].strip() if fields is not None and len(fields) > 17 else ''
        if not raw or raw == 'free':
            return _VERIFY_MATCH_TIMEOUT_S
        val = float(raw)
        return val if val >= 0 else _VERIFY_MATCH_TIMEOUT_S
    except Exception:
        return _VERIFY_MATCH_TIMEOUT_S


def _get_verify_variance(stem, device_name: str, command_name: str) -> float:
    """Per-command verify tolerance from the write command's appended
    field 18 (requirement: 'we add a variance field e.g 0.01. So equal to 1.0
    means actually, whenever you are within 0.99 and 1.01 you are
    within "equal"'). Returns:
      - 0.0 if the field is absent, empty, 'free', unparseable, or
        negative — and 0.0 means _verify_equals takes the EXACT
        string-equality branch, the literal same comparison as before
        this field existed. Every pre-existing command line therefore
        behaves identically ('Default would still be variance=0'),
        same no-migration convention as the timeout at field 17;
      - the parsed positive value otherwise: match when
        |read - target| <= variance, both parsed as floats.
    Never raises — a malformed field must not kill a run."""
    try:
        fields = _find_command_fields(stem, device_name, command_name)
        raw = fields[18].strip() if fields is not None and len(fields) > 18 else ''
        if not raw or raw == 'free':
            return 0.0
        val = float(raw)
        return val if val > 0 else 0.0
    except Exception:
        return 0.0


def _resolve_equals_str(equals_str: str, value_str) -> str:
    """[%] inside verify's 'equals' field -> the value being written by
    THIS row/step — so a sweep can verify against its own moving
    setpoint (requirement: 'would I be able to write [%] in the equal field to
    dynamically check if I reach the setpoint?'). The substituted value
    is exactly the one handed to the command string's own [%] at the
    same call site — same source, same step; not an independent
    variable. No [%] in the field (every command file written before
    this existed, e.g. the stableflag 'true' check) -> returned
    untouched, so the substitution is a strict no-op for all existing
    behavior. value_str=None (no per-step value in scope) also returns
    untouched rather than inventing a substitution."""
    if equals_str and '[%]' in equals_str and value_str is not None:
        return equals_str.replace('[%]', str(value_str))
    return equals_str


# --- Virtual while-loop (control device) helpers -------------------------
# A while-command is a user-defined command on the virtual 'control'
# device (alongside the 4 fixed pause/userprompt/stop/counter): it polls
# a chosen query on ANY device once per Integration Time, comparing the
# value against a threshold, and exits when the comparison is true —
# 'repeat until verify condition is met' (requested). Stored as a wf-mode
# line in control_commands.txt with 5 fields APPENDED after the
# established 0-18 layout (17=verify timeout and 18=tolerance already
# exist — the original spec's 17-21 numbering predates them):
#   19: linked device name   (the exit-condition query's device — NEW:
#                             nothing else stores a device name separately,
#                             every existing linked-read is same-device)
#   20: linked query/command name
#   21: operator — one of _WHILE_OPERATORS
#   22: threshold (raw string; numeric REQUIRED for </> — enforced at
#       save time in WhileEditorDialog, requirement: 'prevent them from saving')
#   23: timeout (raw, _parse_duration_seconds units; '0' = infinite —
#       mandatory in the dialog, no blank default)
# Same appended-field convention as 17/18: bounds-guarded readers, old
# files unaffected, no migration. Recognized at execution time by FIELD
# PRESENCE (not name matching — the name is user-chosen): device ==
# control, name not one of the reserved 4, fields 19-23 populated.

_WHILE_OPERATORS = ('<', '>', '=', '!=')

# 'readvalues[0]' -> element 0 of a split-mode (multi-value) read.
# Command names are lowercase letters+digits only (_bind_name_filter),
# so a bracket can never appear in a legacy linked-read name — the
# suffix is unambiguous and backward-safe. Used in a write command's
# verify/ramp linked read (field 14) and a while definition's watched
# read (field 20); parsed off by _split_element_suffix at every point
# a linked read is actually executed (_query_for_verify,
# _query_current_value) and wherever the BARE name is needed to match
# active-query specs (the exclude-from-data sets).
_ELEMENT_SUFFIX_RE = re.compile(r'^(.+?)\[(\d+)\]$')


def _split_element_suffix(linked_read: str):
    """'readvalues[0]' -> ('readvalues', 0); 'status' -> ('status', None).
    Whitespace-tolerant on the outside; the digits must be a plain
    non-negative integer (the regex guarantees it)."""
    m = _ELEMENT_SUFFIX_RE.match((linked_read or '').strip())
    if not m:
        return (linked_read or '').strip(), None
    return m.group(1), int(m.group(2))


def _extract_element(value, linked_read: str):
    """Apply a linked-read name's element suffix to an already-executed
    read's value: 'readvalues[1]' + ['a','b'] -> 'b'. Bare name +
    scalar passes through. Every mismatch is a loud ValueError — a
    misconfiguration must never be indistinguishable from a legitimate
    wait: suffix on a non-split read; element out of range; and a
    split (list) result with NO suffix, whose error includes the fix.
    Shared by _query_for_verify, _query_current_value, and the while
    loop's read-once-record-and-compare path in _run_while_loop."""
    bare, elem = _split_element_suffix(linked_read)
    if elem is not None:
        if not isinstance(value, list):
            raise ValueError(
                f"linked read '{linked_read}' selects element [{elem}] but "
                f"'{bare}' returned a single value ({value!r}) — the read is "
                f"not split-mode; use '{bare}' without the suffix")
        if elem >= len(value):
            raise ValueError(
                f"linked read '{linked_read}': '{bare}' returned {len(value)} "
                f"values — element [{elem}] does not exist (valid: 0..{len(value) - 1})")
        return value[elem]
    if isinstance(value, list):
        raise ValueError(f"linked read '{bare}' returned {len(value)} values — "
                         f"a single value is needed; select one element "
                         f"(e.g. '{bare}[0]')")
    return value


def _parse_while_definition(fields):
    """Extract a while-definition from a command line's fields, or None
    if this line is NOT a while-command (fields 19-23 absent/blank/
    invalid operator). None means 'not a while', not 'a broken while' —
    a non-reserved control command WITHOUT these fields is a legacy
    dead entry (created through the old generic Add Command path, which
    never worked on control) and is failed with a clear message in
    run()'s dispatch instead of falling through to the generic write
    path's confusing 'control.txt not found' error."""
    if fields is None or len(fields) <= 23:
        return None
    linked_device = fields[19].strip()
    linked_read = fields[20].strip()
    operator = fields[21].strip()
    threshold = fields[22]          # NOT stripped here — comparison strips
    timeout_raw = fields[23].strip()
    if not linked_device or not linked_read or operator not in _WHILE_OPERATORS:
        return None
    return {'linked_device': linked_device, 'linked_read': linked_read,
            'operator': operator, 'threshold': threshold,
            'timeout_raw': timeout_raw}


def _get_while_definition(stem, command_name: str):
    """While-definition for a command on the control device, or None —
    the single lookup used everywhere a 'is this a while-command?'
    decision is made (run() dispatch, tree context menu routing,
    Simulate, nested-chain rejection). Never raises: a missing/
    unreadable commands file is 'not a while'."""
    try:
        fields = _find_command_fields(stem, STANDARD_DEVICE_NAME, command_name)
    except Exception:
        return None
    return _parse_while_definition(fields)


def _while_condition_met(value, operator: str, threshold: str) -> bool:
    """One poll's exit-condition evaluation. =/!=: exact STRING
    comparison, same semantics as _verify_equals ('10.0' does not
    match '10' — verify parity, deliberate). </>: numeric — the
    threshold was validated as a float at save time, but the READ can
    still come back non-numeric at runtime (wrong linked query, device
    hiccup): float() raising ValueError here is ON PURPOSE and fails
    the row loudly in _run_while_loop — a configuration mistake must
    not be indistinguishable from a legitimate long wait."""
    sval = str(value).strip()
    if operator == '=':
        return sval == threshold.strip()
    if operator == '!=':
        return sval != threshold.strip()
    num = float(sval)   # ValueError propagates on purpose — see docstring
    thr = float(threshold.strip())
    return num < thr if operator == '<' else num > thr


def _get_all_query_commands(stem) -> list:
    """(device_name, command_name) for every query-type ('q' mode)
    command across EVERY device in the loaded device list — populates
    WhileEditorDialog's exit-condition dropdown. Unlike ramp/verify's
    _get_read_commands_for_device (scoped to one device, because a
    write's own verify naturally checks that same instrument), a
    while-condition can watch any instrument. Definition-time list:
    NOT filtered by device on/off state — that's a separate,
    execution-time check in _run_while_loop. Virtual devices (control,
    script) have no query commands and contribute nothing."""
    specs = []
    devfile = stem.with_suffix('.dev')
    try:
        device_names = [ln.strip() for ln in devfile.read_text().splitlines() if ln.strip()]
    except Exception:
        return specs
    for device_name in device_names:
        if device_name in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME):
            continue
        try:
            cmdfile = stem / f'{device_name}_commands.txt'
            lines = [ln for ln in cmdfile.read_text().splitlines() if ln.strip()]
        except Exception:
            continue
        for ln in lines:
            fields = ln.split('|')
            if len(fields) > 2 and fields[2] == 'q':
                specs.append((device_name, fields[1]))
    return specs


def _throttle_visa_wait(addr: str):
    """Wait if needed before issuing a new VISA op to addr — see
    _throttle_visa_mark for why the 'last time' is recorded AFTER the
    operation finishes, not before, and _MIN_VISA_INTERVAL's own
    comment for why this exists at all. Plain time.sleep (not abort-
    aware) is fine here — at most _MIN_VISA_INTERVAL, far too short to
    matter for how quickly an abort is noticed."""
    now = time.time()
    last = _LAST_VISA_TIME.get(addr, 0.0)
    wait_needed = _MIN_VISA_INTERVAL - (now - last)
    if wait_needed > 0:
        time.sleep(wait_needed)


def _throttle_visa_mark(addr: str):
    """Record completion time for addr — called AFTER the actual
    socket/VISA operation finishes, success OR failure (via
    try/finally in _visa_write/_visa_query), not before. This was a
    real bug in the original single _throttle_visa: it recorded 'now'
    BEFORE doing the connect/send/recv/close, so the next call's wait
    was measured from when the PREVIOUS operation started trying, not
    from when its connection was actually torn down — if that
    operation itself took any real time, the next call would end up
    with less actual gap since the last close() than _MIN_VISA_INTERVAL
    was supposed to guarantee. Marking failures too (not just
    successes): a failed connection attempt still occupied the
    device's stack for some amount of time and plausibly still needs
    the same settling window before the next attempt."""
    _LAST_VISA_TIME[addr] = time.time()


_HTTP_DEFAULT_CONTENT_TYPE = 'application/json'


def _is_http_addr(addr: str) -> bool:
    """True for the pseudo-VISA HTTP device address format
    'HTTP::host::port::/path::content-type' (path and content-type
    optional). startswith, not substring — 'HTTP' can't appear as a
    prefix in any real VISA address (GPIB0::, ASRL, TCPIP0::, USB0::),
    so this can never misclassify an existing device."""
    return addr.strip().upper().startswith('HTTP::')


def _parse_http_addr(addr: str):
    """Split 'HTTP::host::port::/path::content-type' into its parts.
    Path defaults to '/', content-type to _HTTP_DEFAULT_CONTENT_TYPE —
    both may be omitted from the address entirely (HTTP::host::port is
    valid). Raises ValueError on a malformed address (missing host/
    port, non-numeric port) — callers surface that as an ordinary
    communication error."""
    parts = [p.strip() for p in addr.strip().split('::')]
    if len(parts) < 3 or not parts[1]:
        raise ValueError(f'malformed HTTP address {addr!r} — expected '
                         f'HTTP::host::port[::/path[::content-type]]')
    host = parts[1]
    port = int(parts[2])   # ValueError on garbage — intended
    path = parts[3] if len(parts) > 3 and parts[3] else '/'
    if not path.startswith('/'):
        path = '/' + path
    ctype = parts[4] if len(parts) > 4 and parts[4] else _HTTP_DEFAULT_CONTENT_TYPE
    return host, port, path, ctype


_HTTP_RESPONSE_TIMEOUT_S = 30.0   # How long a REACHABLE HTTP server may take to answer
                                  # after the connection is up. Split from the device
                                  # timeout (which now means only 'is it reachable?')
                                  # after a real incident: the Kiutra's RPC is
                                  # SYNCHRONOUS — 'start' does the work server-side
                                  # (tears down the previous control sequence, engages
                                  # the new one) and only THEN answers, so the answer
                                  # time includes thinking time, not just network time.
                                  # At a field step that teardown exceeded the 5s device
                                  # timeout and the row died on a command that likely
                                  # executed fine. SCPI-style socket devices never hit
                                  # this (they acknowledge in ms and do the work
                                  # afterward — that's why the same 5s is fine there).
                                  # Hard-coded, no user-facing field (requirement: 'hard-code
                                  # 30s or 1 min (no extra field for the user)... If
                                  # there is no connection, I will know after 5s'):
                                  # 30s is 3-6x the worst teardown seen, and a genuinely
                                  # hung-but-reachable server still fails a verify read
                                  # after ONE slow poll (the retry budget, = device
                                  # timeout, is spent by then) and aborts-and-holds.


def _http_post(addr, body, timeout_ms) -> str:
    """POST body to the endpoint encoded in an HTTP:: device address
    and return the response body as a stripped string. One connection
    per request (open, send, read, close) — no persistent socket to
    leave dirty, so the whole RST/WinError-10054 class of problem the
    raw-socket drain exists for cannot occur here. Write terminators
    are NOT appended: HTTP framing (Content-Length) replaces line
    terminators entirely.

    TWO timeouts with distinct meanings (they were one, which forced
    the device timeout to also cover server-side processing time —
    see _HTTP_RESPONSE_TIMEOUT_S):
      - connect: the device timeout from {device}.txt — 'is the
        server reachable at all?' A dead/unplugged device is detected
        this fast.
      - response: _HTTP_RESPONSE_TIMEOUT_S — 'how long may a live
        server work before answering?' Covers synchronous RPC calls
        that do the work inside the request/response cycle.
    Raises RuntimeError with the failed operation, endpoint and the
    timeout that fired spelled out — the previous bare 'timed out'
    (socket.timeout's str) cost a whole elimination-diagnosis to
    attribute. HTTP status >= 400 raises too (preserving the urllib
    behavior this replaced), body included since JSON-RPC servers put
    the actual error detail there. NOTE vs urllib: redirects are not
    followed (no lab RPC server redirects; a 3xx would fall through
    as an unexpected-status error, which is the right loudness)."""
    host, port, path, ctype = _parse_http_addr(addr)
    connect_timeout = max(0.001, timeout_ms / 1000.0)
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    try:
        try:
            conn.connect()
        except Exception as e:
            raise RuntimeError(
                f'could not connect to {host}:{port} within {connect_timeout:g}s '
                f'(device timeout) — device unreachable: {e}') from e
        # Connection is up — from here on, waiting is about the
        # server's processing, not reachability.
        conn.sock.settimeout(_HTTP_RESPONSE_TIMEOUT_S)
        try:
            conn.request('POST', path, body=body.encode('utf-8'),
                         headers={'Content-Type': ctype})
            resp = conn.getresponse()
            data = resp.read()
        except TimeoutError as e:   # socket.timeout is an alias of TimeoutError
            raise RuntimeError(
                f'HTTP POST to http://{host}:{port}{path} got no answer within '
                f'{_HTTP_RESPONSE_TIMEOUT_S:g}s (response timeout) — the server is '
                f'reachable but did not answer in time; for synchronous RPC calls '
                f'this can mean the operation itself is slow, not that the device '
                f'is dead') from e
        if resp.status >= 300:
            detail = data.decode('utf-8', errors='replace').strip()
            raise RuntimeError(
                f'HTTP {resp.status} {resp.reason} from http://{host}:{port}{path}'
                + (f' — {detail[:300]}' if detail else ''))
        return data.decode('utf-8', errors='replace').strip()
    finally:
        conn.close()


def _check_http_write_error(response_body: str):
    """Inspect a write's HTTP response body and raise if it encodes an
    application-level failure — added after a real bug: qmeas's write
    path never returns a value to the caller (see _visa_write's
    docstring — 'No response is RETURNED'), so an HTTP write that gets
    a 200 OK with a JSON-RPC-level error INSIDE the body previously
    looked identical to a real success. The concrete case that exposed
    this: sample_magnet.stop() takes zero arguments (the API doc's own
    signature — 'target' is documented specifically as start()'s
    'legacy notation for (setpoint, ramp)', not a generic call()
    envelope field), so a stop command built by copying start()'s
    shape and just dropping setpoint/ramp — but keeping "target":null —
    plausibly raises 'unexpected keyword argument' server-side. That
    response is still HTTP 200 (JSON-RPC errors are normally embedded
    in the body, not signaled via HTTP status), so the write reported
    success while doing nothing.

    Deliberately conservative — only raises for TWO specific, well-
    defined shapes, so a non-Kiutra HTTP device using neither
    convention is completely unaffected (never raises, exactly as
    before this function existed):
      1. Standard JSON-RPC 2.0 top-level {"error": {...}} — protocol-
         level failure (bad method/params).
      2. Kiutra's own envelope, {"result": {"MessageCode": N, ...}}
         with N != 0 — application-level failure. StatusMessage (or
         Message) is included in the raised error so the ACTUAL server
         error is visible instead of just 'something went wrong'.
    Anything else — not JSON, JSON without either shape, MessageCode
    present and 0 — passes through silently. Never raises on its own
    inability to parse; only on a POSITIVELY IDENTIFIED error shape."""
    try:
        obj = json.loads(response_body)
    except ValueError:
        return
    if not isinstance(obj, dict):
        return
    if 'error' in obj and obj['error']:
        err = obj['error']
        msg = err.get('message', err) if isinstance(err, dict) else err
        raise RuntimeError(f'JSON-RPC error: {msg}')
    result = obj.get('result')
    if isinstance(result, dict) and result.get('MessageCode', 0) not in (0, None):
        detail = result.get('StatusMessage') or result.get('Message') or result
        raise RuntimeError(f'device reported MessageCode={result["MessageCode"]}: {detail}')


def _visa_query(addr, query_str, wterm_sel, rterm_sel, timeout_ms, baud_sel, bits_sel) -> str:
    """Send query_str to addr and return the raw response. Raises on
    failure — caller shows the error. Same two code paths as
    DeviceEditorPanel._on_test_connection (socket vs VISA resource).
    _throttle_visa_wait first, _throttle_visa_mark in a finally at the
    very end (success OR failure) — see their own docstrings for why
    completion time, not start time, is what gets recorded."""
    _throttle_visa_wait(addr)
    try:
        if _is_http_addr(addr):
            # HTTP device (e.g. a JSON-RPC server): the query string IS
            # the POST body, the response body IS the raw response —
            # extraction (incl. the JSON-key mode) happens downstream in
            # _apply_extraction, same as for any other device type.
            return _http_post(addr, query_str, timeout_ms)
        if '::SOCKET' in addr:
            parts = addr.split('::')
            host, port = parts[1], int(parts[2])
            wterm = _TERM_MAP.get(wterm_sel, '\n')
            # The device's configured READ terminator, previously unused
            # on this branch (single recv(4096) and hope). Empty when no
            # terminator is configured -> the loop below degrades to
            # exactly the old single-recv behavior.
            rterm_b = _TERM_MAP.get(rterm_sel, '').encode('ascii')
            cmd = f'{query_str}{wterm}'.encode('utf-8')
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout_ms / 1000.0)
            try:
                s.connect((host, port))
                s.sendall(cmd)
                # First recv: a timeout HERE still raises (no response at
                # all is a genuine failure, unchanged semantics).
                buf = s.recv(4096)
                # Read until the reply actually ends (configured read
                # terminator seen). The old single recv(4096) silently
                # TRUNCATED a reply that arrived split across TCP
                # segments — and, worse, left the tail unread in the
                # receive buffer, so close() sent RST instead of FIN.
                # RST-poisoning the instrument's single-client TCP
                # server is the standing best explanation (see
                # _SOCKET_WRITE_DRAIN_S, the same fix on the write
                # path) for the intermittent WinError 10054 on the
                # NEXT connection — which is exactly the observed
                # pattern: with three magnet-axis devices on one
                # address polled back-to-back, the failures landed on
                # the SECOND device of the burst, right after the
                # first one's close. A continuation timeout with data
                # already in hand returns what we have (matches the
                # old behavior for a device whose replies don't carry
                # the configured terminator) rather than raising.
                while rterm_b and buf and not buf.endswith(rterm_b):
                    try:
                        chunk = s.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break   # peer closed cleanly — reply is complete
                    buf += chunk
                # Non-blocking drain of any residue that's ALREADY
                # arrived (settimeout(0): recv raises immediately when
                # the buffer is empty — zero added latency in the
                # common case, unlike the write path's timed drain).
                # Guarantees the buffer is empty at close -> FIN, not
                # RST.
                try:
                    s.settimeout(0)
                    while True:
                        extra = s.recv(4096)
                        if not extra:
                            break
                        buf += extra
                except Exception:
                    pass
            finally:
                # Always close, including on connect/send/first-recv
                # failure — the old code leaked the socket on any
                # exception, leaving teardown to the garbage collector,
                # which on a single-client instrument server just adds
                # more connection churn at the worst possible moment.
                try:
                    s.close()
                except Exception:
                    pass
            return buf.decode('ascii', errors='replace').strip()
        rm = get_resource_manager()
        if rm is None:
            raise RuntimeError('No VISA resource manager available.')
        dev = rm.open_resource(addr)
        try:
            dev.timeout = int(timeout_ms)
            wterm_str = _TERM_MAP.get(wterm_sel, '')
            rterm_str = _TERM_MAP.get(rterm_sel, '')
            if wterm_str:
                try: dev.write_termination = wterm_str
                except Exception: pass
            if rterm_str:
                try: dev.read_termination = rterm_str
                except Exception: pass
            if _is_serial_addr(addr):
                try: dev.baud_rate = int(BAUD_RATES[baud_sel])
                except Exception: pass
                try: dev.data_bits = int(DATA_BITS[bits_sel])
                except Exception: pass
            return dev.query(query_str)
        finally:
            try: dev.close()
            except Exception: pass
    finally:
        _throttle_visa_mark(addr)


def _format_number(value: float, numtype_sel: int, precision: int) -> str:
    """Format value per the command's stored number-format selection
    (0=float exponential, 1=float, 2=integer). Shared by
    CommandEditorDialog._on_test_once and the Tasks execution engine —
    one formatting rule, not two copies that could drift apart."""
    if numtype_sel == 0:
        return f'{value:.{precision}e}'
    elif numtype_sel == 1:
        return f'{value:.{precision}f}'
    return str(int(round(value)))


def _find_command_fields(stem, device_name: str, command_name: str):
    """Look up one command's full field list in {device}_commands.txt.
    Returns None if the device's command file or the specific command
    can't be found — caller decides how to handle that."""
    cmdfile = stem / f'{device_name}_commands.txt'
    lines = [ln for ln in cmdfile.read_text().splitlines() if ln.strip()]
    for ln in lines:
        fields = ln.split('|')
        if fields[1] == command_name:
            return fields
    return None


def _get_read_commands_for_device(stem, device_name: str) -> list:
    """Command names (not full lines) for every query/read-type
    command ('q' mode) defined on this device — populates the
    linked-read dropdown in CommandEditorDialog's Method row (ramp or
    verify, whichever is selected), and is checked against again at
    execution time (_get_command_method).
    Returns [] if the device's commands file doesn't exist yet or has
    no read commands — a brand new device, or one with only write
    commands defined so far."""
    cmdfile = stem / f'{device_name}_commands.txt'
    try:
        lines = [ln for ln in cmdfile.read_text().splitlines() if ln.strip()]
    except Exception:
        return []
    names = []
    for ln in lines:
        fields = ln.split('|')
        if len(fields) > 2 and fields[2] == 'q':
            names.append(fields[1])
    return names


def _get_all_command_names_for_device(stem, device_name: str) -> list:
    """Every command name defined on this device, write or query alike
    — unlike _get_read_commands_for_device (query-only, for linked-read
    dropdowns), this is for the duplicate-name check in
    CommandEditorDialog._on_ok: a write command and a query command
    sharing the same name would collide just as much as two commands
    of the same mode would, since the alias is '{device}_{command}'
    regardless of mode. Returns [] if the device's commands file
    doesn't exist yet — a brand new device with nothing defined."""
    cmdfile = stem / f'{device_name}_commands.txt'
    try:
        lines = [ln for ln in cmdfile.read_text().splitlines() if ln.strip()]
    except Exception:
        return []
    return [ln.split('|')[1] for ln in lines if len(ln.split('|')) > 1]


def _get_command_method(stem, device_name: str, command_name: str):
    """Returns (method, linked_read, rate, equals_str) for a write
    command — fields[13-16], appended beyond v1's original 13-field
    format. method is 'none' (default — jump directly, unchanged from
    before any of this existed), 'ramp', or 'verify'. user feedback corrected an
    earlier design here: ramp and verify used to be independent
    switches (either, both, or neither); now they're mutually
    exclusive alternatives selected by ONE dropdown in the editor —
    'There is NO overlap between defining a rate and the verifying.'
    linked_read is shared between the two modes (same underlying
    question, 'what does this device currently report') but only one
    of rate/equals_str is ever meaningful at a time, depending on
    method.

    Raises ValueError if method is 'ramp' with rate<=0 or no linked
    read, or 'verify' with no linked read or an empty equals string —
    loud, not a silent fallback to 'none': the user configured a
    safety mechanism; silently not applying it while everything else
    (logs, UI) looks like it's active would be worse than refusing to
    run. CommandEditorDialog's own save-time validation should prevent
    these combinations from ever being written — this is the defensive
    re-check for a hand-edited or pre-validation file."""
    fields = _find_command_fields(stem, device_name, command_name)
    if fields is None or len(fields) <= 13:
        return 'none', None, 0.0, ''
    method = fields[13] if fields[13] in ('ramp', 'verify') else 'none'
    if method == 'none':
        return 'none', None, 0.0, ''

    linked = fields[14] if len(fields) > 14 else 'none'
    if not linked or linked == 'none' or not linked.strip():
        raise ValueError(f"method '{method}' is configured but no current-value read is "
                         f"linked — cannot {method} without knowing what the device currently "
                         f"reports")

    if method == 'ramp':
        try:
            rate = float(fields[15]) if len(fields) > 15 else 0.0
        except (ValueError, IndexError):
            rate = 0.0
        if rate <= 0:
            raise ValueError("method is 'ramp' but the rate is 0 or missing — "
                             "either set a positive rate or change the method to 'none'")
        return 'ramp', linked, rate, ''

    # method == 'verify'
    equals_str = fields[16] if len(fields) > 16 else ''
    if not equals_str.strip():
        raise ValueError("method is 'verify' but no 'equals' value is set — cannot verify "
                         "without knowing what the read should report")
    return 'verify', linked, 0.0, equals_str


# --- Query LED tri-state (on/verify/off) persistence -----------------
# A query command's tree LED has three states: green ('on', queried and
# recorded), red ('verify', queried but its own on/off has no bearing on
# get_active_query_specs — same effective exclusion as gray, purely a
# visual reminder that this query is a verify target elsewhere and
# deliberately not meant to be saved), gray ('off', not queried at all).
# Persisted in field[0] of the command's line in _commands.txt — the
# SAME field CommandEditorDialog has always written 'True'/'False' into,
# but which nothing has ever read until now (grepped: zero readers).
#
# Field[0] on-disk encoding, chosen to survive every file ever written
# without a silent behavior change:
#   'True'  -> on      (existing convention, unchanged)
#   'FALSE' (all-caps) -> verify   (requirement: 'a second False called FALSE')
#   'OFF'               -> off     (persisted gray)
#   anything else, INCLUDING THE EXISTING LEGACY 'False' -> on
# The legacy-'False'-means-on mapping is deliberate, not an oversight:
# CommandEditorDialog has unconditionally written 'False' into every
# query line ever saved, regardless of the tree LED's actual state
# (which has only ever lived in memory) — so 'False' carries no real
# signal in any file that predates this feature. Treating it as 'off'
# would silently gray out (and stop saving) every currently-active
# query in every device list, the first time qmeas is reopened. 'FALSE'
# and 'OFF' are new, unambiguous, and cannot appear in any existing file.
_QUERY_LED_NEXT = {'on': 'verify', 'verify': 'off', 'off': 'on'}
_QUERY_LED_TO_FIELD0 = {'on': 'True', 'verify': 'FALSE', 'off': 'OFF'}


def _query_led_state_from_field0(raw: str) -> str:
    raw = (raw or '').strip()
    if raw == 'FALSE':
        return 'verify'
    if raw == 'OFF':
        return 'off'
    return 'on'   # 'True', legacy 'False', missing, or malformed


def _parse_duration_seconds(text: str) -> float:
    """Parse a duration: bare number = seconds ('1', '1.5'), or with an
    explicit unit suffix — 's' seconds ('1s'), 'm' minutes ('2.5m' ->
    150s), 'h' hours ('3h' -> 10800s). 'ms' milliseconds ('500ms' ->
    0.5s) added beyond what was specified — doesn't conflict with
    s/m/h and covers sub-second settle times, flagging it since it's
    an addition, not asked for. Case-insensitive, tolerates
    surrounding whitespace. Checks 'ms' before bare 's' (both end in
    's'), so order matters.

    Deliberately the ONLY numeric field with unit handling — requirement:
    other fields (Constant/Start, Final, Steps) are native device
    units with no consistent unit to guess ('I won't enter 1.5V or
    0.5mA'), so they stay plain-number-only, no suffix stripping.

    Raises ValueError for anything that isn't a recognized number or
    number+unit, ON PURPOSE: this is the field that silently broke
    for three rounds of debugging when '1s' hit a bare float('1s')
    call, raised ValueError internally, and was silently caught and
    defaulted to 0.0 — a wrong wait that LOOKED like it ran, logged
    nothing wrong, and took a screenshot of the literal cell content
    to finally diagnose. See _run_timed_sweep and the pause branch in
    TaskRunnerThread.run() for where a parse failure now fails the row
    loudly instead of repeating that."""
    s = text.strip()
    s_lower = s.lower()
    if s_lower.endswith('ms'):
        return float(s[:-2].strip()) / 1000.0
    if s_lower.endswith('h'):
        return float(s[:-1].strip()) * 3600.0
    if s_lower.endswith('m'):
        return float(s[:-1].strip()) * 60.0
    if s_lower.endswith('s'):
        return float(s[:-1].strip())
    return float(s)


def _linspace(start: float, final: float, steps: int) -> list:
    """Convention (confirmed explicitly, worked example: Start=12,
    Final=13, Steps=10 -> increment 0.1, 11 points, 12.0..13.0):
    Steps counts INTERVALS, not points. Total points = Steps + 1.
    increment = (final - start) / steps. steps<=0 -> [start]."""
    if steps <= 0:
        return [start]
    increment = (final - start) / steps
    return [start + i * increment for i in range(steps + 1)]


def _abbreviate(items: list, max_show: int = 2) -> list:
    """First max_show items, '...', last item — if there's enough of
    them to bother abbreviating. requirement: 'you don't have to show all'."""
    if len(items) <= max_show + 1:
        return items
    return items[:max_show] + ['...'] + [items[-1]]


def _resolve_final_string(stem, device_name: str, command_name: str, value_str: str) -> str:
    """Given a command's definition and a raw value (from Constant/
    Start), return the exact string that would be sent — substituting
    and formatting per the command's stored number-format field if it
    has a '[%]' placeholder. Shared by real execution (_on_start_stop)
    and Simulate: one implementation, so the preview can't silently say
    something different from what actually gets sent."""
    fields = _find_command_fields(stem, device_name, command_name)
    if fields is None:
        raise RuntimeError('command definition not found')
    cmdstr = fields[4]
    if '[%]' not in cmdstr:
        return cmdstr
    if not value_str.strip():
        raise RuntimeError('no value given in Constant/Start')
    value = float(value_str)
    field5 = fields[5] if len(fields) > 5 else 'None'
    if field5 != 'None' and '=' in field5:
        prefix, _, precision_str = field5.partition('=')
        numtype_sel = {'-1': 0, '-2': 1, '-3': 2}.get(prefix, 1)
        precision = int(precision_str)
    else:
        numtype_sel, precision = 1, 6
    return cmdstr.replace('[%]', _format_number(value, numtype_sel, precision))


def _visa_write(addr, write_str, wterm_sel, rterm_sel, timeout_ms, baud_sel, bits_sel) -> None:
    """Send write_str to addr. No response is RETURNED to the caller —
    but on the socket path, any response the device sends IS read and
    discarded before closing (see the drain block below): the Mercury
    iPS answers every command including SETs, and closing a TCP socket
    with unread data in its receive buffer sends RST instead of FIN —
    the standing best explanation for the intermittent WinError 10054
    on the NEXT connection to the same instrument. Same two code paths
    as _visa_query. _throttle_visa_wait first, _throttle_visa_mark in
    a finally at the very end — see their own docstrings for why."""
    _throttle_visa_wait(addr)
    try:
        if _is_http_addr(addr):
            # HTTP device: POST the (already value-substituted) command
            # string as the request body. The server's response body is
            # inspected for an application-level error (see
            # _check_http_write_error's docstring for the exact bug this
            # closes) and then discarded — nothing is left unread on the
            # wire, so no drain step is needed (the response is read in
            # full inside _http_post before the connection closes). An
            # HTTP error STATUS
            # raises from within _http_post already; a 200-with-embedded-
            # error body raises here.
            body = _http_post(addr, write_str, timeout_ms)
            _check_http_write_error(body)
            return
        if '::SOCKET' in addr:
            parts = addr.split('::')
            host, port = parts[1], int(parts[2])
            wterm = _TERM_MAP.get(wterm_sel, '\n')
            cmd = f'{write_str}{wterm}'.encode('utf-8')
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout_ms / 1000.0)
            s.connect((host, port))
            s.sendall(cmd)
            # Drain: read (and discard) whatever the device sends in
            # response, so the socket closes clean (FIN) instead of
            # with unread data (RST). Costs up to _SOCKET_WRITE_DRAIN_S
            # of latency per write on devices that never echo writes
            # (QDAC-II, Keithley SCPI) — see the constant's own comment.
            # Every exception swallowed deliberately: a device that
            # sends nothing (timeout) or closes first is FINE here; the
            # write itself already succeeded, which is all the caller
            # is being promised.
            try:
                s.settimeout(_SOCKET_WRITE_DRAIN_S)
                s.recv(4096)
            except Exception:
                pass
            s.close()
            return
        rm = get_resource_manager()
        if rm is None:
            raise RuntimeError('No VISA resource manager available.')
        dev = rm.open_resource(addr)
        try:
            dev.timeout = int(timeout_ms)
            wterm_str = _TERM_MAP.get(wterm_sel, '')
            rterm_str = _TERM_MAP.get(rterm_sel, '')
            if wterm_str:
                try: dev.write_termination = wterm_str
                except Exception: pass
            if rterm_str:
                try: dev.read_termination = rterm_str
                except Exception: pass
            if _is_serial_addr(addr):
                try: dev.baud_rate = int(BAUD_RATES[baud_sel])
                except Exception: pass
                try: dev.data_bits = int(DATA_BITS[bits_sel])
                except Exception: pass
            dev.write(write_str)
        finally:
            try: dev.close()
            except Exception: pass
    finally:
        _throttle_visa_mark(addr)


def _split_chained_command(cmdstr: str) -> list:
    """Split a (already value-substituted) command string on '&&' into
    an ordered list of ('write', text) / ('pause', seconds) segments —
    qmeas v1.1 convention, reincorporated: 'Addon commands can be
    chained using &&... A timed delay is also possible:
    :SOUR:VOLT [%]&&pause=0.5 (waits 0.5s between commands).' The '[%]'
    placeholder, if present, must be in the FIRST segment only (v1.1
    manual: 'The main command... must always come first') — substitution
    already happened before this runs (_resolve_final_string operates
    on the whole string), so this function only handles the '&&'/
    'pause=' structure. A command string with no '&&' at all just comes
    back as a single ('write', cmdstr) entry — every write goes through
    this, chained or not, rather than needing a separate is-it-chained
    branch anywhere else."""
    segments = []
    for raw_segment in cmdstr.split('&&'):
        seg = raw_segment.strip()
        m = re.match(r'^pause\s*=\s*([\d.]+)\s*$', seg, re.IGNORECASE)
        if m:
            segments.append(('pause', float(m.group(1))))
        else:
            segments.append(('write', seg))
    return segments


# Whitelisted names available inside a linked row's expression — plain
# math plus abs, nothing else (no builtins at all: eval below runs with
# an empty __builtins__, same trust level as the script device's plain
# exec() by request, but there's no reason to expose more than the math
# a follower equation actually needs).
_LINK_FUNCS = {name: getattr(math, name) for name in (
    'sin', 'cos', 'tan', 'asin', 'acos', 'atan',
    'exp', 'log', 'log10', 'sqrt', 'pi', 'e')}
_LINK_FUNCS['abs'] = abs


def _eval_link_expression(expr: str, mother_value: float) -> float:
    """Evaluate a linked (follower) row's Constant/Start expression with
    [%] standing for the mother's commanded setpoint — the user's chosen
    syntax, reusing the placeholder convention from command strings
    ('the value goes here'). '^' is translated to Python's '**' (on
    floats '^' is a TypeError anyway, so nothing meaningful is lost).
    [%] is substituted as a parenthesized variable so precedence
    survives, e.g. '[%]^2' with [%]=-3 is 9, not -9.

    Fails loudly (ValueError) on: empty expression, any evaluation
    error (unknown name, syntax error, math domain error like
    sqrt(-1)), or a non-numeric / non-finite result — consistent with
    the fail-loud rule everywhere else; a silent default here would be
    a wrong voltage on a real gate."""
    text = expr.strip()
    if not text:
        raise ValueError('linked row has no expression in Constant/Start '
                         "(e.g. '[%]*5' — [%] is the mother row's value)")
    text = text.replace('[%]', '(_m_)').replace('^', '**')
    namespace = dict(_LINK_FUNCS)
    namespace['_m_'] = float(mother_value)
    try:
        result = eval(text, {'__builtins__': {}}, namespace)
    except Exception as e:
        raise ValueError(f'link expression {expr!r} failed to evaluate '
                         f'with [%]={mother_value:g}: {e}')
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ValueError(f'link expression {expr!r} did not produce a number '
                         f'(got {result!r})')
    result = float(result)
    if not math.isfinite(result):
        raise ValueError(f'link expression {expr!r} produced a non-finite '
                         f'value ({result!r}) with [%]={mother_value:g}')
    return result


def _parse_range_position(pos_str: str, str_len: int, default: int) -> int:
    """Parse one endpoint of the 'range between i-th and j-th position'
    extraction mode. Accepts a plain 1-indexed integer ('27'), bare
    'end' (the string's own length), or 'end-N' (length minus N, e.g.
    'end-1' drops a trailing unit-suffix character) — case-insensitive,
    whitespace-tolerant around the '-'. This was the actual bug: the
    previous code only ever checked 'is this purely digits', and
    anything else (including 'end-1', 'end-5') fell through to a bare
    len(raw) — silently discarding the '-N' offset entirely, so
    'end-1' behaved exactly like bare 'end'. Returns `default` if the
    string matches neither pattern."""
    s = pos_str.strip().lower()
    if s.lstrip('-').isdigit():
        return int(s)
    if s == 'end':
        return str_len
    m = re.match(r'^end\s*-\s*(\d+)$', s)
    if m:
        return str_len - int(m.group(1))
    return default


def _apply_extraction(raw: str, fields: list):
    """Apply a query command's stored extraction mode (fields[7], 0-5 —
    same options as CommandEditorDialog.qresult_choice) to a raw VISA
    response. Mirrors get_command_line()'s field layout exactly: this
    is 'apply whatever was configured when the command was saved,' not
    a second definition of what the modes mean.

    Returns a single string for every mode except 'split string'
    (mode 1), which returns a list of strings — one query can yield
    several numbers (v1 example: '10.12,-12.3' split on ',' -> two
    values), and that shape is preserved here rather than silently
    collapsed to one."""
    sel_str = fields[7] if len(fields) > 7 else '0'
    sel = int(sel_str) if sel_str.strip().lstrip('-').isdigit() else 0

    if sel == 0:                         # whole string as is
        return raw
    if sel == 1:                         # split string
        delim = fields[8] if len(fields) > 8 and fields[8] else ','
        return [part.strip() for part in raw.split(delim)]
    if sel == 2:                         # range between i-th and j-th position (1-indexed, inclusive)
        start_str = fields[8] if len(fields) > 8 else '1'
        end_str = fields[9] if len(fields) > 9 else 'end'
        start_idx = max(0, _parse_range_position(start_str, len(raw), 1) - 1)
        end_idx = _parse_range_position(end_str, len(raw), len(raw))
        return raw[start_idx:end_idx]
    if sel == 3:                         # remove trailing: cut from this substring onward
        token = fields[8] if len(fields) > 8 else ''
        idx = raw.find(token) if token else -1
        return raw[:idx] if idx >= 0 else raw
    if sel == 4:                         # remove preceding: cut up to and including this substring
        token = fields[8] if len(fields) > 8 else ''
        idx = raw.find(token) if token else -1
        return raw[idx + len(token):] if idx >= 0 else raw
    if sel == 5:                         # remove all non-numerics
        return re.sub(r'[^0-9.\-+eE]', '', raw)
    if sel == 6:                         # JSON key (dotted path)
        # For HTTP/JSON devices: parse the response as JSON and walk a
        # dotted key path (fields[8], default 'result' — the JSON-RPC
        # convention). Integer segments index into JSON arrays, so a
        # batch response like [{"result":...},{"result":...}] is
        # addressed as '0.result', '1.result'. Any failure (not JSON,
        # missing key, index out of range) returns `raw` unchanged —
        # the same never-raise fallback convention modes 3/4 use when
        # their token isn't found; the un-extracted response in the
        # data file / verify error message then tells you what actually
        # came back. Scalars are returned in their JSON spelling
        # ('true', 'null', '0.123') so what you type in a verify
        # 'equals' field matches what Query Once shows on the wire;
        # strings are returned bare (no quotes).
        pathspec = fields[8] if len(fields) > 8 and fields[8] and fields[8] != 'free' else 'result'
        try:
            obj = json.loads(raw)
        except ValueError:
            return raw
        for key in pathspec.split('.'):
            key = key.strip()
            if isinstance(obj, list):
                try:
                    obj = obj[int(key)]
                except (ValueError, IndexError):
                    return raw
            elif isinstance(obj, dict):
                if key not in obj:
                    return raw
                obj = obj[key]
            else:
                return raw
        if isinstance(obj, str):
            return obj
        return json.dumps(obj)
    return raw


def _convert_to_number(value, should_convert: bool):
    """Apply a query command's 'convert to number' flag (fields[5]).
    value is whatever _apply_extraction returned — a str, or a list of
    str for split mode. Never raises: a piece that won't parse as a
    float is left as the original string, same as leaving the box
    unchecked in v1 for a response that isn't purely numeric — silently
    discarding a reading because of one bad conversion would be worse
    than just passing the string through."""
    if not should_convert:
        return value

    def conv(s):
        try:
            return float(s)
        except (TypeError, ValueError):
            return s

    if isinstance(value, list):
        return [conv(v) for v in value]
    return conv(value)


def _execute_query(stem, device_name: str, command_name: str):
    """Send one read/query command's stored query string via VISA and
    return the value after applying its stored extraction (fields[7]/
    [8]/[9]) and convert-to-number (fields[5]) settings — exactly what
    CommandEditorDialog wrote when the command was saved, not a
    re-guess of it. Returns a float, a str, or a list of either (split
    mode). Raises on any failure (command not found, device settings
    missing, VISA/socket error) — caller decides how to log it."""
    fields = _find_command_fields(stem, device_name, command_name)
    if fields is None:
        raise RuntimeError('command definition not found')
    query_str = fields[4]
    addr, wterm, rterm, timeout, baud, bits = _read_device_settings(stem, device_name)
    raw = _visa_query(addr, query_str, wterm, rterm, timeout, baud, bits)
    extracted = _apply_extraction(raw, fields)
    convert_flag = len(fields) > 5 and fields[5] == '1'
    return _convert_to_number(extracted, convert_flag)


class TaskRunnerThread(threading.Thread):
    """Runs the grid row by row in a real background thread — replaces
    an earlier wx.Yield()-based stopgap that was wrong: Yield()
    re-enters the main event loop, so a long wait still risks
    freezing/reentrancy; a genuine background thread can sleep or block
    on VISA I/O without touching the GUI thread's event loop at all.

    NOTHING about which rows exist, which are eligible, or what values
    they hold is captured up front. Every row is fetched live, right
    before it's used, via _fetch_row() — a synchronous round-trip
    (wx.CallAfter + threading.Event) to TasksPanel._fetch_row_state_
    for_thread on the main thread, since wx is not thread-safe and this
    thread must never touch self.panel.grid directly. This is
    deliberate, not an oversight: the requirement is that rows BELOW the currently-
    executing one stay genuinely live — editable, draggable, and
    addable — while the run is in progress, and a pre-built snapshot
    would make such edits silently inert (grid shows the new value,
    thread sends the old one). A live fetch makes it actually true.
    Rows AT OR ABOVE the frontier (see TasksPanel._run_frontier) are
    frozen at the UI level (see run_active/_row_frozen) — this thread
    doesn't need to know about that separately, since _fetch_row_
    state_for_thread advances the frontier to `row` the INSTANT that
    row is fetched, before eligibility is even evaluated.

    A pleasant side effect of doing it this way: insert/delete/drag
    below the frontier need no explicit index-remapping. The loop just
    asks "what's at position N now?" each time it advances — whatever
    currently occupies that slot is what runs there, however it got
    there.

    The scan ends (not aborts — a normal, silent end) the first time
    _fetch_row reports the row doesn't exist, i.e. it has reached
    however many rows the grid CURRENTLY has. If a row gets appended
    after that point, it's simply not part of this run; Start again to
    pick it up. This is an inherent boundary of any live-queue design,
    not a bug — the alternative (waiting indefinitely for maybe-more-
    rows-later) has no principled stopping point.

    query_specs: list of (device_name, command_name) tuples, snapshotted
    once at Start time from DevicesPanel.get_active_query_specs(). NOT
    live, unlike task rows — deliberately: the Devices tree (including
    every read/query LED) stays fully frozen for the whole run (a
    different, unrelated concern — the thread reads device/command
    files from disk on every row, so live-editing THOSE mid-run is a
    live-instrument-comms hazard, not a queueing convenience). Since
    nothing can toggle a query's LED while frozen, a live refetch here
    would always return the same answer as the Start-time snapshot
    anyway.

    Queries do NOT run after a plain constant-value write — v1 manual,
    section 4.1: 'constant: a single value is set to the device WITHOUT
    SUBSEQUENT DATA ACQUISITION.' Only x/nested-x/exp-x sweeps trigger
    reads in v1; a constant write never did. (An earlier version of
    this file queried after every write regardless — wrong, fixed.)
    control_counter and a real device's Steps>=1 sweep BOTH get that
    sweep-with-acquisition behavior now, through one unified engine —
    requirement: 'counter is in a sense just a device that doesnt accept
    commands.' See _run_timed_sweep: every active query runs once per
    step, and the accumulated table gets written to a single
    tab-separated data file at the end (_write_sweep_datafile).

    Real-device writes (constant-value OR each step of a sweep) use ONE
    of three mutually exclusive Methods, set per command (fields[13-16]
    — see _get_command_method, CommandEditorDialog). user feedback corrected an
    earlier design here: ramp and verify used to be independent
    switches (either, both, or neither); now they're alternatives
    picked by one dropdown — 'There is NO overlap between defining a
    rate and the verifying.'

    'none' (default): jump directly, unchanged from before any of this
    existed.

    'ramp': breaks the move into 1-unit-of-time sub-steps, none
    exceeding a configured rate, via _write_with_rate_limit. requirement:
    'this ramp applies generally also BETWEEN the steps from the task
    list' — so this isn't just a one-time approach to a sweep's Start
    value, it's the SAME rate-limited write called for every single
    step-to-step transition, including the plain-constant case (which
    is just a one-step 'sweep' from wherever the device is to the
    target). Write-and-forget: no verification a write actually reached
    its target.

    'verify': jumps directly (no software-side ramping — 'no overlap'
    means a magnet controller's own internal ramp handles the physical
    transit here), then polls a linked read once a second, indefinitely
    (_verify_equals), until its value — compared as a plain string, not
    a numeric tolerance — exactly equals a configured value, e.g. a
    magnet controller reporting 'HOLD' once done ramping. See 'query a
    preselected command... and wait until you are at that value' —
    this is what actually matters for a superconducting magnet: its
    controller keeps ramping internally long after this software's
    write call has returned, so 'proceed once the write succeeded' and
    'proceed once the magnet has actually arrived' are genuinely
    different things. Both failure modes are bounded (see
    _verify_equals): a failing read is retried every second up to the
    device's own configured timeout, and a value that never matches
    fails after _VERIFY_MATCH_TIMEOUT_S — the original poll-forever
    behavior is gone by explicit request after it presented as a
    silent stall.

    Either way, a write failure or a verify read failure at any point
    aborts the row outright — no skip-and-continue, no retry (requirement:
    'abort means abort, you stop dead').

    onhold_devices: list of device names, snapshotted once at Start
    time from DevicesPanel.get_active_onhold_devices() — every ACTIVE
    device with a write command literally named 'onhold' (a reserved
    name, qmeas v1.1 precedent). Sent immediately, write-and-forget, to
    every one of them the moment this thread notices an abort (see the
    end of run()) — because stopping THIS thread's writes does nothing
    to stop a magnet controller's own already-in-progress internal
    ramp. Only on genuine user abort, never on normal completion — a
    past version of this same feature fired it on ordinary completion
    too, which was wrong and got corrected before."""

    PASS_OVER_DELAY = 0.15   # seconds — brief, so the sweep is visible rather than instant
    FETCH_TIMEOUT = 5.0      # seconds — see _fetch_row: a hang here means the main thread
                             # is stuck (e.g. a true native-modal dialog not pumping events),
                             # and this thread should abort cleanly rather than hang forever

    def __init__(self, panel, stem, query_specs=None, onhold_devices=None,
                 active_devices=None):
        super().__init__(daemon=True)
        self.panel = panel
        self.stem = stem
        self.query_specs = query_specs or []
        self.onhold_devices = onhold_devices or []
        # Device names with their tree LED on, snapshotted at Start
        # (DevicesPanel.get_active_device_names) — consumed only by
        # _run_while_loop's pre-poll active check. None (not wired /
        # older caller) deliberately means 'treat every device as
        # active': the check degrades to absent, never blocks a run
        # over missing information.
        self.active_devices = active_devices
        self._abort = threading.Event()
        # Set (with a human-readable reason) by _verify_equals when IT
        # aborts the run — a verify failure means the system is NOT in
        # the state every subsequent row assumes (a magnet may still be
        # ramping), so continuing would collect data at unknown
        # conditions. The central abort block at the end of run() uses
        # this to log an accurate closing line ('aborted automatically:
        # ...' instead of the then-wrong 'aborted by user') — the
        # onhold dispatch itself is the SAME central mechanism either
        # way, deliberately not duplicated.
        self._auto_abort_reason = None

    def request_abort(self):
        self._abort.set()

    def _sleep_checking_abort(self, duration: float):
        end = time.time() + duration
        while time.time() < end and not self._abort.is_set():
            time.sleep(min(0.1, max(0.0, end - time.time())))

    def _query_current_value(self, device_name: str, linked_read: str) -> float:
        """Query a device's 'current output' via its linked read, for
        RAMP specifically — computing a ramp needs a numeric value to
        step from. Requires a scalar, numeric result. A split-mode
        (multi-value) read is usable ONLY with an explicit element
        suffix ('readvalues[0]' — see _split_element_suffix); without
        one, or with a non-numeric result, this raises loudly rather
        than guessing which element/value to use."""
        bare, _ = _split_element_suffix(linked_read)
        value = _extract_element(_execute_query(self.stem, device_name, bare), linked_read)
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"linked read '{linked_read}' returned {value!r}, not a number")

    def _query_for_verify(self, device_name: str, linked_read: str):
        """Query a device's read command for VERIFY's comparison (and
        the while-loop's exit read, which shares this path) — unlike
        _query_current_value (ramp, which needs a numeric value to
        step from), this accepts whatever the read returns: a float
        (a numeric readback) or a string (a status/flag readback, e.g.
        a magnet controller's 'HOLD'). A split-mode (multi-value) read
        is usable ONLY with an explicit element suffix
        ('readvalues[0]' — see _split_element_suffix): 'equals one
        specific thing' doesn't mean anything for a list of values, so
        the user must say WHICH value they mean."""
        bare, _ = _split_element_suffix(linked_read)
        return _extract_element(_execute_query(self.stem, device_name, bare), linked_read)

    def _verify_equals(self, device_name: str, command_name: str,
                       linked_read: str, equals_str: str):
        """Poll linked_read once per second until its value matches
        equals_str, blocking row progression until then — e.g. a
        superconducting magnet controller doing its own slow internal
        ramp: the write call only sends the setpoint; this is what
        waits for the controller to report done.

        Matching depends on the command's verify tolerance (fields[18],
        via _get_verify_variance):
        - tolerance 0 (the default): EXACT string comparison,
          str(value).strip() == equals_str — right for discrete status
          readbacks ('HOLD'); note '10.0' does not match '10'.
        - tolerance > 0: NUMERIC comparison, |read - target| <=
          tolerance, both floated — for numeric setpoints that never
          land on exact digits. A non-numeric read simply doesn't
          match that poll; a non-numeric equals_str falls back to
          exact matching.

        Two failure modes, each bounded:

        1. The READ ITSELF fails (e.g. WinError 10054): retried every
           _VERIFY_READ_RETRY_INTERVAL_S until the retries have spanned
           the device's own configured timeout (the same 5000ms-default
           field from the device editor, read from settings once at
           verify start). Only if EVERY retry in that window fails does
           the row fail — a one-off connection reset no longer aborts a
           field sweep. A successful read resets the retry budget, so
           an intermittent glitch on poll #40 gets the same tolerance
           as one on poll #1.

        2. The read SUCCEEDS but the value never matches: fails after
           the command's own verify-timeout (fields[17]; absent -> the
           600s _VERIFY_MATCH_TIMEOUT_S default, 0 -> no timeout) with
           the last read value in the message — which tells you WHY it
           never matched ('RTOS' = still ramping; something unexpected
           = wrong linked read or a wedged controller). Replaces the
           previous unbounded wait that presented as a silent stall.

        EITHER failure now also aborts the whole run (requirement: 'send a
        _hold (if magnets are involved) and abort the whole thing',
        and 'both' failure modes): see _abort_run_and_hold inside —
        the flag routes through run()'s existing central onhold block,
        so every active magnet-type device gets frozen, and the
        closing log line states device, command, wall-clock time and
        the reason. A verify failure means the system is not in the
        state subsequent rows assume; silently continuing (the old
        behavior: log FAILED, march on) risked collecting data at
        wrong conditions.

        Returns immediately (returning the last known value, not
        raising) if aborted — the caller treats that as an abort, not a
        success; sending any device's onhold command is handled once,
        centrally, in run(), not here."""
        equals_str = equals_str.strip()
        last_value = None
        poll_count = 0
        try:
            retry_budget_s = float(_read_device_settings(self.stem, device_name)[3]) / 1000.0
        except Exception:
            retry_budget_s = 5.0   # device settings unreadable — the read below will
                                   # fail loudly on its own; this just keeps the retry
                                   # machinery sane in the meantime
        # Per-command match timeout (fields[17]): absent -> 600s
        # default (all pre-existing files), 0 -> no timeout at all
        # (poll until match or abort — for controller-paced moves that
        # legitimately take hours). Resolved once here, not per poll.
        match_timeout_s = _get_verify_timeout_s(self.stem, device_name, command_name)
        # Per-command tolerance (fields[18], _get_verify_variance):
        # 0 (all pre-existing files) -> the EXACT string comparison
        # below, the same line of code as always. > 0 -> numeric
        # match |read - target| <= variance; parsed ONCE here — if the
        # target itself isn't numeric, that's a definition error
        # (tolerance on a status string), and every poll would fail to
        # match anyway, so it degrades to exact matching with a note
        # in the failure message rather than inventing a behavior.
        variance = _get_verify_variance(self.stem, device_name, command_name)
        target_num = None
        if variance > 0:
            try:
                target_num = float(equals_str)
            except (TypeError, ValueError):
                variance = 0.0   # non-numeric target: tolerance is meaningless,
                                 # fall back to exact — same outcome (no match)
                                 # either way, but with the code path that has
                                 # always existed

        def _abort_run_and_hold(reason_exc):
            """Both verify failure modes end here (requirement: 'both') —
            match timeout AND read-retry exhaustion. Either way the
            system's actual state is unknown (a magnet may still be
            physically ramping toward a setpoint the rest of the task
            list assumes was reached), so: set the abort flag — the
            row loop exits instead of marching on to rows that would
            collect data at wrong conditions — which ALSO routes
            through run()'s existing central onhold block, freezing
            every active magnet-type device. Record the reason so the
            closing log line says what actually happened (with device,
            command and wall-clock time; the adjacent FAILED entry
            carries the row/step number and full error detail)."""
            self._auto_abort_reason = (
                f'verify failure on {device_name}_{command_name} at '
                f'{time.strftime("%Y-%m-%d %H:%M:%S")} — {reason_exc}')
            self._abort.set()

        verify_start = time.time()
        read_fail_start = None   # time of FIRST failure in the current failure streak
        read_attempts = 0        # attempts within the current failure streak
        wx.CallAfter(self.panel._wait_status_show,
                    f'{device_name}_{command_name} is waiting for {linked_read} to equal '
                    f'{equals_str!r}{f" (±{variance:g})" if variance > 0 else ""}. Please wait!')
        try:
            while True:
                if self._abort.is_set():
                    return last_value
                poll_count += 1
                try:
                    last_value = self._query_for_verify(device_name, linked_read)
                except Exception as e:
                    now = time.time()
                    if read_fail_start is None:
                        read_fail_start = now
                        read_attempts = 0
                    read_attempts += 1
                    if now - read_fail_start >= retry_budget_s:
                        err = RuntimeError(
                            f'verify read failed {read_attempts} times over '
                            f'{now - read_fail_start:.1f}s (retried every '
                            f'{_VERIFY_READ_RETRY_INTERVAL_S:g}s up to the device timeout, '
                            f'{retry_budget_s:g}s) — last error: {e}')
                        _abort_run_and_hold(err)
                        raise err
                    self._sleep_checking_abort(_VERIFY_READ_RETRY_INTERVAL_S)
                    continue   # retry the read — does NOT advance to the
                               # match check with a stale last_value
                read_fail_start = None   # a good read resets the retry budget
                if variance > 0:
                    # Tolerance match: |read - target| <= variance, both
                    # as floats. A read that doesn't parse (device
                    # hiccup, status string) simply doesn't match this
                    # poll — falls through to the SAME timeout/abort
                    # machinery, no new failure path.
                    try:
                        if abs(float(str(last_value).strip()) - target_num) <= variance:
                            return last_value
                    except (TypeError, ValueError):
                        pass
                elif str(last_value).strip() == equals_str:
                    return last_value
                if match_timeout_s > 0 and time.time() - verify_start >= match_timeout_s:
                    tol_note = f' (±{variance:g})' if variance > 0 else ''
                    err = RuntimeError(
                        f'verify timed out after {match_timeout_s:g}s '
                        f'({poll_count} polls): {linked_read} never equaled '
                        f'{equals_str!r}{tol_note} — last read {str(last_value).strip()!r}')
                    _abort_run_and_hold(err)
                    raise err
                self._sleep_checking_abort(1.0)
        finally:
            wx.CallAfter(self.panel._wait_status_hide)

    def _execute_chained_write(self, addr, resolved_cmdstr: str, wterm, rterm, timeout, baud, bits):
        """Drop-in replacement for a raw _visa_write call, everywhere a
        resolved (value-substituted) command string is actually sent —
        qmeas v1.1 precedent, reincorporated: 'Addon commands can be
        chained using &&... :SOUR:VOLT [%]&&pause=0.5 (waits 0.5s
        between commands).' See _split_chained_command for the actual
        splitting; this executes the resulting segments in order, using
        self._sleep_checking_abort (not a plain time.sleep) for
        'pause=N' so an abort mid-chain is still noticed promptly. A
        non-chained command string just comes back as one segment, so
        every call site uses this uniformly rather than needing a
        separate is-it-chained branch.

        Between two DIRECTLY ADJACENT write segments (no explicit
        pause=N already between them — e.g. your actual
        'FSET:[%]&&ACTN:RTOS'), also waits _CHAIN_SEGMENT_DELAY:
        intermittent WinError 10054 ('An existing connection was
        forcibly closed by the remote host') was seen in the lab, and this
        instrument needs real settling time between successive
        commands to the same device — not added on top of an explicit
        pause=N you already specified, since that's a deliberate wait
        you already accounted for."""
        segments = _split_chained_command(resolved_cmdstr)
        for i, (kind, value) in enumerate(segments):
            if kind == 'pause':
                self._sleep_checking_abort(value)
            else:
                _visa_write(addr, value, wterm, rterm, timeout, baud, bits)
                next_is_write = (i + 1 < len(segments) and segments[i + 1][0] == 'write')
                if next_is_write:
                    self._sleep_checking_abort(_CHAIN_SEGMENT_DELAY)

    def _write_with_rate_limit(self, device_name: str, command_name: str,
                               target_value: float, current_value: float, rate_per_sec: float) -> float:
        """Write target_value to device_name_command_name. rate_per_sec
        <= 0 means jump directly (the original, unchanged behavior) —
        otherwise breaks the move into 1-second sub-steps, none
        exceeding rate_per_sec, matching v1's literal 'step size...
        defined as change per second.' requirement: 'this ramp applies
        generally also BETWEEN the steps from the task list' — this is
        called for EVERY transition a real-device row makes, whether
        that's a plain constant set (one call) or one step of a
        multi-step sweep (one call per step, current_value tracked
        forward from whatever was last successfully written) — there
        is no separate 'ramp to start' vs 'ramp between steps' code
        path, they're the same call.

        Write-and-forget: no verification the device actually reached
        each intermediate value — that's write-and-verify, explicitly
        deferred. Returns the value the device should now be at
        (== target_value, unless aborted partway through, in which
        case whatever was last successfully written — requirement: 'abort
        means abort, you stop dead', checked before EVERY sub-step).
        Raises on any write failure, uncaught — the caller fails the
        whole row; a command that just failed to respond is not
        something to keep issuing new setpoints to."""
        addr, wterm, rterm, timeout, baud, bits = _read_device_settings(self.stem, device_name)

        def write_one(value):
            final_str = _resolve_final_string(self.stem, device_name, command_name, str(value))
            self._execute_chained_write(addr, final_str, wterm, rterm, timeout, baud, bits)

        if rate_per_sec <= 0:
            if self._abort.is_set():
                return current_value
            write_one(target_value)
            return target_value

        delta = target_value - current_value
        if delta == 0:
            return target_value
        direction = 1.0 if delta > 0 else -1.0
        n_substeps = math.ceil(abs(delta) / rate_per_sec)
        value = current_value
        # requirement: 'When a device is set to ramping and I start the task
        # list, it appears as if qmeas is frozen because there is no
        # progress. Please add a static text... "Device X is currently
        # ramping to a setpoint. Please wait!"' — a multi-second ramp
        # (especially a plain constant-value one, Steps=0, which has NO
        # sweep progress gauge at all) otherwise gives zero visual
        # feedback that anything is happening.
        wx.CallAfter(self.panel._wait_status_show,
                    f'{device_name}_{command_name} is currently ramping to a setpoint. Please wait!')
        try:
            for i in range(1, n_substeps + 1):
                if self._abort.is_set():
                    return value   # stop dead — wherever we got to is where we stay
                value = current_value + direction * min(rate_per_sec * i, abs(delta))
                write_one(value)
                if i < n_substeps:   # no wait needed after the final sub-step —
                    self._sleep_checking_abort(1.0)   # the caller's own IntTime wait follows
            return value
        finally:
            wx.CallAfter(self.panel._wait_status_hide)

    def _fetch_row(self, row: int):
        """Synchronous live read of row `row`'s current eligibility/
        values, plus advances the frontier — both happen together on
        the main thread inside _fetch_row_state_for_thread. Returns a
        dict with at least 'exists'; see that method for the rest.
        Returns {'exists': False} if the main thread doesn't respond
        within FETCH_TIMEOUT — treated as a reason to stop, not a
        reason to guess or hang."""
        result = {}
        done = threading.Event()
        wx.CallAfter(self.panel._fetch_row_state_for_thread, row, result, done)
        if not done.wait(timeout=self.FETCH_TIMEOUT):
            return {'exists': False, 'timed_out': True}
        return result

    def _gather_nested_chain(self, root_row: int, root_fetched: dict):
        """Starting from root_row (whose own live fetch already says
        has_child), keeps fetching forward — row+1, row+2, ... via the
        SAME live self._fetch_row mechanism as any other row — until a
        row reports has_child=False (the innermost level of the chain).
        Returns (chain_rows, chain_fetched, timed_out) — both lists
        OUTERMOST-first, same length, chain_fetched[i] the fetched dict
        for chain_rows[i]. This is what freezes every row in the chain
        immediately, not just the root: each fetch advances
        self._run_frontier on its own, before _run_nested_sweep does
        anything with the result.

        A row reporting has_child but the next one not existing
        (grid inconsistency — shouldn't happen if the depth structure
        is well-formed, but this doesn't assume that) stops the chain
        there rather than hanging or crashing."""
        chain_rows = [root_row]
        chain_fetched = [root_fetched]
        cur = root_row
        while chain_fetched[-1].get('has_child'):
            cur += 1
            child = self._fetch_row(cur)
            if child.get('timed_out'):
                return chain_rows, chain_fetched, True
            if not child.get('exists'):
                break
            if not child.get('eligible'):
                # Deactivated (or emptied) between the parent's fetch
                # reporting an active child and this fetch — the child
                # row isn't frozen until its own fetch, so this window
                # is real. The chain ends HERE, before the ineligible
                # row: its dict is the minimal ineligible form (no
                # 'alias', no field values) and must never reach
                # _run_nested_sweep, which reads those keys
                # unconditionally. The row itself, and anything deeper,
                # is then reached by the main scan and passed over
                # (ineligible / orphaned-nested), same as if it had
                # been off from the start.
                break
            chain_rows.append(cur)
            chain_fetched.append(child)
        return chain_rows, chain_fetched, False

    def _gather_link_box(self, anchor_row: int, anchor_fetched: dict):
        """Starting from anchor_row (whose own live fetch already says
        has_follower), keeps fetching forward — same live self._fetch_row
        mechanism, same frontier-freezing side effect as
        _gather_nested_chain — for as long as each fetched follower's
        own has_follower says the box continues. Using has_follower as
        the continuation signal (exactly like _gather_nested_chain uses
        has_child) means the first row PAST the box is never fetched
        here — important, because a fetch advances the frozen frontier,
        and over-fetching would freeze the next unrelated row for the
        box's entire (possibly hours-long) execution instead of leaving
        it live-editable like every other not-yet-reached row.

        Returns (box_rows, followers, timed_out): box_rows is EVERY row
        in the box including the anchor (so the caller knows how far to
        advance the scan); followers is [(row, fetched), ...] for only
        the ACTIVE followers (LED on, command present) — an
        inactive/empty follower stays in box_rows (it's structurally
        part of the box) but is passed over visually and never written,
        consistent with the On flag's meaning everywhere else."""
        box_rows = [anchor_row]
        followers = []
        cur = anchor_row
        box_continues = anchor_fetched.get('has_follower')
        while box_continues:
            cur += 1
            fetched = self._fetch_row(cur)
            if fetched.get('timed_out'):
                return box_rows, followers, True
            if not fetched.get('exists') or not fetched.get('linked'):
                break   # defensive — has_follower said linked, trust the live read
            box_rows.append(cur)
            if fetched.get('alias'):
                followers.append((cur, fetched))
            else:
                # LED off or empty command — part of the box, not written
                wx.CallAfter(self.panel._on_row_pass_over, cur)
            box_continues = fetched.get('has_follower')
        return box_rows, followers, False

    def _prepare_followers(self, followers: list, test_value: float) -> list:
        """Upfront validation of every active follower in a link box —
        BEFORE the mother writes anything, same fail-before-touching-
        hardware philosophy as _run_nested_sweep's per-level upfront
        parse. Raises ValueError on the first problem. For each
        follower: rejects control/script rows (nothing numeric to
        write — the GUI already refuses to link these, but a hand-
        edited task file must not sneak one through), requires the
        command definition to exist and contain [%] (a fixed command
        string can't 'follow' anything), test-evaluates the expression
        with the mother's first value (a typo'd expression fails here,
        not at step 17 of a sweep), resolves the command's own
        ramp/verify method, and — for ramp — queries the follower's
        current output once, exactly like a mother sweep does.

        Returns a list of dicts carrying everything
        _write_follower_step needs, including the per-follower
        'current_value' that ramp tracks forward across steps."""
        prepared = []
        for fol_row, fetched in followers:
            alias = fetched['alias']
            device_name, command_name = fetched['device_name'], fetched['command_name']
            expr = fetched['value_str']
            if device_name in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME):
                raise ValueError(f'linked row {fol_row + 1} ({alias}): control/script '
                                 f'commands cannot be linked followers')
            fields = _find_command_fields(self.stem, device_name, command_name)
            if fields is None:
                raise ValueError(f'linked row {fol_row + 1} ({alias}): command '
                                 f'definition not found')
            if '[%]' not in fields[4]:
                raise ValueError(f'linked row {fol_row + 1} ({alias}): command has no '
                                 f'[%] placeholder — a fixed command string cannot follow '
                                 f'the mother value')
            # Fails loudly on a bad expression before any hardware is touched
            _eval_link_expression(expr, test_value)
            method, linked_read, rate, equals_str = _get_command_method(
                self.stem, device_name, command_name)
            current_value = 0.0
            if method == 'ramp':
                current_value = self._query_current_value(device_name, linked_read)
            prepared.append({'row': fol_row, 'alias': alias,
                             'device_name': device_name, 'command_name': command_name,
                             'expr': expr, 'method': method, 'linked_read': linked_read,
                             'rate': rate, 'equals_str': equals_str,
                             'current_value': current_value})
        return prepared

    def _write_follower_step(self, fol: dict, mother_value: float) -> float:
        """One follower write for one mother setpoint: evaluate the
        expression at [%]=mother_value, then move the follower there
        through EXACTLY the machinery a mother sweep step uses for its
        own method — _write_with_rate_limit for ramp (current_value
        tracked forward in fol, open-loop, same as the mother),
        direct _execute_chained_write for none/verify, then
        _verify_equals for verify. Raises on any failure — the caller
        fails the whole box, 'abort means abort'. Returns the computed
        value (for the data file column and the log)."""
        computed = _eval_link_expression(fol['expr'], mother_value)
        if fol['method'] == 'ramp':
            fol['current_value'] = self._write_with_rate_limit(
                fol['device_name'], fol['command_name'],
                computed, fol['current_value'], fol['rate'])
        else:
            final_str = _resolve_final_string(
                self.stem, fol['device_name'], fol['command_name'], str(computed))
            addr, wterm, rterm, timeout, baud, bits = _read_device_settings(
                self.stem, fol['device_name'])
            self._execute_chained_write(addr, final_str, wterm, rterm, timeout, baud, bits)
            if fol['method'] == 'verify' and not self._abort.is_set():
                self._verify_equals(fol['device_name'], fol['command_name'],
                                    fol['linked_read'],
                                    _resolve_equals_str(fol['equals_str'], computed))
        return computed

    def _run_constant_with_followers(self, row: int, fetched: dict, row_start: str,
                                     box_rows: list, followers: list) -> bool:
        """A link box whose mother is a plain constant (no Steps): the
        mother writes its single value — through its own configured
        ramp/verify method, exactly like the plain constant path in
        run() — then every follower writes f(mother value) once, top to
        bottom. No Integration Time, no reads, no data file: v1
        convention, 'constant: A single value is set to the device
        WITHOUT SUBSEQUENT DATA ACQUISITION' — linking doesn't change
        that, it only adds the follower writes.

        Deliberately a near-copy of run()'s inline constant branch
        rather than a refactor of it: that branch is proven on hardware
        and stays untouched; this method exists only for the
        has_follower case."""
        alias = fetched['alias']
        device_name, command_name = fetched['device_name'], fetched['command_name']
        value_str, comment = fetched['value_str'], fetched['comment']
        wx.CallAfter(self.panel._on_chain_start, box_rows)
        try:
            try:
                mother_value = float(value_str)
            except ValueError:
                raise ValueError(f'Constant/Start {value_str!r} is not a number — a '
                                 f'link-box mother needs a numeric value for its '
                                 f'followers to evaluate [%] against')
            # Followers validated BEFORE the mother touches hardware —
            # a typo'd expression must not leave the mother moved and
            # the followers not.
            prepared = self._prepare_followers(followers, test_value=mother_value)

            method, linked_read, rate, equals_str = _get_command_method(
                self.stem, device_name, command_name)
            cmd_fields = _find_command_fields(self.stem, device_name, command_name)
            value_label = f' [{value_str.strip()}]' if cmd_fields and '[%]' in cmd_fields[4] else ''

            if method == 'ramp':
                current_value = self._query_current_value(device_name, linked_read)
                self._write_with_rate_limit(device_name, command_name,
                                            mother_value, current_value, rate)
                method_note = f' (ramped at {rate:g}/s, based on {linked_read})'
            else:
                final_str = _resolve_final_string(self.stem, device_name, command_name, value_str)
                addr, wterm, rterm, timeout, baud, bits = _read_device_settings(self.stem, device_name)
                self._execute_chained_write(addr, final_str, wterm, rterm, timeout, baud, bits)
                method_note = ''

            if method == 'verify' and not self._abort.is_set():
                resolved_equals = _resolve_equals_str(equals_str, value_str)
                self._verify_equals(device_name, command_name, linked_read, resolved_equals)
                method_note = f' (verified {linked_read} == {resolved_equals!r})'

            follower_notes = []
            if not self._abort.is_set():
                for fol in prepared:
                    if self._abort.is_set():
                        break
                    computed = self._write_follower_step(fol, mother_value)
                    follower_notes.append(f'{fol["alias"]} = {fol["expr"]} -> {computed:g}')

            row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
            if self._abort.is_set():
                wx.CallAfter(self.panel._on_chain_failed, box_rows,
                            f'ABORTED: {alias} (link box) — stopped before completing\n'
                            f'{row_start} to {row_finish}\n\n')
                return False

            entry_log = f'Constant set: {alias}{value_label}{method_note}\n'
            for note in follower_notes:
                entry_log += f'Linked: {note}\n'
            entry_log += f'{row_start} to {row_finish}\n'
            if comment:
                entry_log += f'Comment: {comment}\n'
            entry_log += '\n'
            wx.CallAfter(self.panel._on_chain_done, box_rows, entry_log)
            return True
        except Exception as e:
            row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
            wx.CallAfter(self.panel._on_chain_failed, box_rows,
                        f'FAILED: {alias} (link box) — {e}\n{row_start} to {row_finish}\n\n')
            return False

    def _show_userprompt(self, message: str) -> bool:
        """Blocks the calling (background) thread until the user
        responds to a modal dialog shown on the main thread — same
        threading.Event + wx.CallAfter pattern as _fetch_row, but
        deliberately NO timeout: this is a real, open-ended wait for a
        human to actually read and respond to something, not a main-
        thread-responsiveness check. Returns True if Continue was
        pressed, False if Abort was (which also triggers a real abort
        itself, from the main thread, before this returns — see
        TasksPanel._show_userprompt_dialog)."""
        result_holder = {}
        done = threading.Event()
        wx.CallAfter(self.panel._show_userprompt_dialog, message, result_holder, done)
        done.wait()
        return result_holder.get('continue', False)

    def run(self):
        n_executed, n_skipped = 0, 0
        row = 0
        while not self._abort.is_set():
            fetched = self._fetch_row(row)
            if fetched.get('timed_out'):
                wx.CallAfter(self.panel.on_log,
                            f'ABORTED: main thread did not respond while fetching row {row} '
                            f'(waited {self.FETCH_TIMEOUT:g}s) — is a dialog blocking it?\n\n')
                break
            if not fetched.get('exists'):
                break   # reached the end of the grid as it currently stands — see class docstring

            if not fetched['eligible']:
                wx.CallAfter(self.panel._on_row_pass_over, row)
                time.sleep(self.PASS_OVER_DELAY)
                row += 1
                continue

            if fetched.get('depth', 0) > 0:
                # A nested (depth>0) row reached directly by the scan —
                # it should have been consumed already as part of its
                # parent's chain (see the has_child branch below). Seeing
                # it here means it wasn't: a sibling structure (two rows
                # at the same depth under one parent, not a strictly-
                # increasing chain), or a depth>0 row with no valid
                # shallower row above it. Pass over rather than guess at
                # what it should do standalone.
                wx.CallAfter(self.panel._on_row_pass_over, row)
                time.sleep(self.PASS_OVER_DELAY)
                row += 1
                continue

            alias, device_name, command_name = fetched['alias'], fetched['device_name'], fetched['command_name']
            value_str, comment = fetched['value_str'], fetched['comment']
            row_start = time.strftime('%Y-%m-%d %H:%M:%S')

            if device_name == STANDARD_DEVICE_NAME and command_name == 'pause':
                wx.CallAfter(self.panel._on_row_start, row)
                try:
                    duration = _parse_duration_seconds(value_str) if value_str.strip() else 1.0
                    wx.CallAfter(self.panel._pause_ui_start)
                    self._sleep_checking_abort(duration)
                    wx.CallAfter(self.panel._pause_ui_stop)
                    row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                    entry_log = f'Pause: {duration:g}s\n{row_start} to {row_finish}\n'
                    if comment:
                        entry_log += f'Comment: {comment}\n'
                    entry_log += '\n'
                    wx.CallAfter(self.panel._on_row_done, row, entry_log)
                    n_executed += 1
                except Exception as e:
                    # Was: inner try/except silently caught a ValueError
                    # from a bad duration and defaulted to 1.0 — the
                    # SAME class of bug that hid the counter's IntTime
                    # problem for three rounds ('1s' parsing as a plain
                    # float raises ValueError, gets swallowed, wrong
                    # value used, nothing visibly wrong). Removed: a
                    # parse failure now falls through to here, which
                    # already fails the row and logs the real reason —
                    # this outer handler pre-dates that bug and was
                    # always correct, it just never got a chance to run.
                    wx.CallAfter(self.panel._pause_ui_stop)
                    row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                    wx.CallAfter(self.panel._on_row_failed, row,
                                f'FAILED: {alias} — {e}\n{row_start} to {row_finish}\n\n')
                    n_skipped += 1
                row += 1
                continue

            if device_name == STANDARD_DEVICE_NAME and command_name == 'userprompt':
                # requirement: 'incorporate control_userprompt as a popup
                # window that requires the user to press continue.'
                # Message comes from Comment, not Constant/Start —
                # _apply_control_field_locks locks Start/Final/Steps/
                # IntTime empty for this command ('takes no value at
                # all'), leaving Comment as the only editable free-text
                # field actually available on this row.
                wx.CallAfter(self.panel._on_row_start, row)
                message = comment.strip() if comment.strip() else 'Continue?'
                continued = self._show_userprompt(message)
                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                if continued:
                    entry_log = (f'User prompt: {alias} shown {message!r} — user pressed Continue\n'
                                f'{row_start} to {row_finish}\n\n')
                    wx.CallAfter(self.panel._on_row_done, row, entry_log)
                    n_executed += 1
                else:
                    # Abort already triggered self._abort — see
                    # _show_userprompt/TasksPanel._show_userprompt_
                    # dialog — the outer loop will detect it and log
                    # 'Run aborted by user' on its own on the next
                    # iteration; this just marks THIS row's own outcome.
                    wx.CallAfter(self.panel._on_row_failed, row,
                                f'ABORTED: {alias} — user pressed Abort at the prompt\n'
                                f'{row_start} to {row_finish}\n\n')
                    n_skipped += 1
                row += 1
                continue

            if device_name == STANDARD_DEVICE_NAME and command_name == 'stop':
                # requirement: 'incorporate the control_stop that acts as
                # pressing the stop button' — taken literally: the
                # exact same request_abort() a real Stop click makes,
                # so it also gets the exact same onhold-on-abort
                # handling at the end of run(), not a lesser 'just stop
                # processing more rows' behavior.
                wx.CallAfter(self.panel._on_row_start, row)
                self.request_abort()
                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                entry_log = f'Stop: {alias} — acted as pressing Stop\n{row_start} to {row_finish}\n'
                if comment:
                    entry_log += f'Comment: {comment}\n'
                entry_log += '\n'
                wx.CallAfter(self.panel._on_row_done, row, entry_log)
                n_executed += 1
                row += 1
                continue

            if device_name == STANDARD_DEVICE_NAME and command_name == 'counter' and not fetched['has_child']:
                # A counter can be a link-box mother too (followers =
                # f(counter value), i.e. time/index-programmed outputs)
                # — same gathering as the real-device sweep branch below.
                box_rows, followers = [row], []
                if fetched.get('has_follower'):
                    box_rows, followers, timed_out = self._gather_link_box(row, fetched)
                    if timed_out:
                        wx.CallAfter(self.panel.on_log,
                                    f'ABORTED: main thread did not respond while fetching a '
                                    f'linked row (waited {self.FETCH_TIMEOUT:g}s) — is a dialog '
                                    f'blocking it?\n\n')
                        break
                # The UI block is anchor + ACTIVE followers only — an
                # inactive follower stays passed-over (set during
                # gathering) and must not be highlighted or completion-
                # marked with the box; the scan still advances over the
                # WHOLE box below (len(box_rows), not len(block)).
                block = [row] + [r for r, _f in followers]
                if len(block) > 1:
                    wx.CallAfter(self.panel._on_chain_start, block)
                else:
                    wx.CallAfter(self.panel._on_row_start, row)
                did_execute = self._run_timed_sweep(row, fetched, row_start, is_counter=True,
                                                    followers=followers,
                                                    block_rows=block if len(block) > 1 else None)
                if did_execute:
                    n_executed += 1
                else:
                    n_skipped += 1
                row += len(box_rows)
                continue

            if device_name == STANDARD_DEVICE_NAME and command_name not in _RESERVED_CONTROL_COMMAND_NAMES:
                # A user-defined command on the virtual control device:
                # a while-command (fields 19-23 — recognized inside
                # _run_while_loop by FIELD PRESENCE via
                # _get_while_definition, NOT by name: the name is
                # user-chosen, unlike the 4 fixed commands dispatched
                # by exact string match above). A non-reserved control
                # command WITHOUT those fields is a legacy dead entry
                # from the old generic Add Command path (which never
                # worked here — control is virtual, no control.txt) and
                # gets a clear failure in there too, instead of falling
                # through to the generic write path's baffling
                # file-not-found error. The 4 reserved names never reach
                # this: pause/userprompt/stop are consumed above;
                # counter is either consumed above or (has_child) falls
                # through to the nested-chain path below, deliberately
                # NOT caught here.
                wx.CallAfter(self.panel._on_row_start, row)
                did_execute = self._run_while_loop(row, fetched, row_start)
                if did_execute:
                    n_executed += 1
                else:
                    n_skipped += 1
                row += 1
                continue

            if device_name == SCRIPT_DEVICE_NAME:
                # requirement: 'qmeas doesnt check anything. It executes. If
                # there is an error it just passes over it. It is the
                # users responsibility to ensure operation.' No
                # sandboxing, no restricted namespace, no validation of
                # the file's contents — a plain exec() of the script's
                # own text, in a fresh namespace (no qmeas internals
                # exposed; the script is a standalone Python program,
                # not a qmeas plugin, per 'no parameters allowed other
                # than comment' — there's nothing here for it to receive
                # even if it wanted to). Caught by the SAME try/except
                # every other row already uses — an exception is logged
                # as an ordinary row failure and the run continues,
                # exactly like any other write failure; this is not a
                # special, more-forgiving error path, just the existing
                # one.
                #
                # Blocking, uninterruptible: exec() runs to completion
                # (or raises) with no abort checkpoints inside it — Stop
                # does not interrupt a running script, only takes effect
                # once it returns. If the script itself drives hardware
                # and the user needs an emergency stop while it's
                # running, that has to come from the script itself (or
                # the hardware's own front panel) — qmeas has no way in.
                wx.CallAfter(self.panel._on_row_start, row)
                script_path = custom_dir() / f'{command_name}.py'
                wx.CallAfter(self.panel._wait_status_show, f'{alias} is running. Please wait!')
                try:
                    source = script_path.read_text()
                    exec(source, {'__name__': '__main__', '__file__': str(script_path)})
                    row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                    entry_log = f'Script: {alias} — ran {script_path.name}\n{row_start} to {row_finish}\n'
                    if comment:
                        entry_log += f'Comment: {comment}\n'
                    entry_log += '\n'
                    wx.CallAfter(self.panel._on_row_done, row, entry_log)
                    n_executed += 1
                except Exception as e:
                    row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                    wx.CallAfter(self.panel._on_row_failed, row,
                                f'FAILED: {alias} — {e}\n{row_start} to {row_finish}\n\n')
                    n_skipped += 1
                finally:
                    wx.CallAfter(self.panel._wait_status_hide)
                row += 1
                continue

            steps_str = fetched['steps_str']
            try:
                steps_check = int(steps_str) if steps_str.strip() else 0
            except ValueError:
                steps_check = 0

            if steps_check >= 1 and not fetched['has_child']:
                # A real device's Start/Final/Steps sweep — same unified
                # engine as counter, now with actual VISA writes (and,
                # if the command has a ramp rate configured, rate-
                # limited ones — see _run_timed_sweep). If linked
                # follower rows sit directly below, gather the whole box
                # first (freezing every row in it, same mechanism as a
                # nested chain) and hand the followers to the sweep.
                box_rows, followers = [row], []
                if fetched.get('has_follower'):
                    box_rows, followers, timed_out = self._gather_link_box(row, fetched)
                    if timed_out:
                        wx.CallAfter(self.panel.on_log,
                                    f'ABORTED: main thread did not respond while fetching a '
                                    f'linked row (waited {self.FETCH_TIMEOUT:g}s) — is a dialog '
                                    f'blocking it?\n\n')
                        break
                # UI block = anchor + ACTIVE followers only (an inactive
                # follower stays passed-over, never completion-marked);
                # scan advance covers the whole box regardless.
                block = [row] + [r for r, _f in followers]
                if len(block) > 1:
                    wx.CallAfter(self.panel._on_chain_start, block)
                else:
                    wx.CallAfter(self.panel._on_row_start, row)
                did_execute = self._run_timed_sweep(row, fetched, row_start, is_counter=False,
                                                    followers=followers,
                                                    block_rows=block if len(block) > 1 else None)
                if did_execute:
                    n_executed += 1
                else:
                    n_skipped += 1
                row += len(box_rows)
                continue

            if fetched['has_child']:
                # Nested chain root — requirement: 'one loop counts... and then
                # the other loop increments... just like chained for-
                # loops.' Gather the WHOLE descending-depth chain (this
                # row's own live fetch already told us it has a child;
                # keep fetching forward — same live self._fetch_row
                # mechanism as any other row — until a row reports no
                # child of its own, i.e. the innermost level) before
                # executing any of it. This is also what freezes every
                # row in the chain for the block's whole duration: each
                # fetch advances self._run_frontier immediately.
                chain_rows, chain_fetched, timed_out = self._gather_nested_chain(row, fetched)
                if timed_out:
                    wx.CallAfter(self.panel.on_log,
                                f'ABORTED: main thread did not respond while fetching a '
                                f'nested row (waited {self.FETCH_TIMEOUT:g}s) — is a dialog '
                                f'blocking it?\n\n')
                    break
                wx.CallAfter(self.panel._on_chain_start, chain_rows)
                did_execute = self._run_nested_sweep(chain_rows, chain_fetched, row_start)
                if did_execute:
                    n_executed += 1
                else:
                    n_skipped += 1
                row += len(chain_rows)
                continue

            if fetched.get('has_follower'):
                # Constant mother with linked followers: mother writes
                # its single value (its own ramp/verify included), then
                # every follower writes f(mother value) once. Kept in
                # its own method (_run_constant_with_followers) so the
                # plain constant path directly below stays byte-for-byte
                # what it was — no shared-branch surgery on a path
                # that's proven on hardware.
                box_rows, followers, timed_out = self._gather_link_box(row, fetched)
                if timed_out:
                    wx.CallAfter(self.panel.on_log,
                                f'ABORTED: main thread did not respond while fetching a '
                                f'linked row (waited {self.FETCH_TIMEOUT:g}s) — is a dialog '
                                f'blocking it?\n\n')
                    break
                # UI block = anchor + ACTIVE followers only, same
                # reasoning as the sweep dispatch above; scan advance
                # covers the whole box.
                block = [row] + [r for r, _f in followers]
                did_execute = self._run_constant_with_followers(
                    row, fetched, row_start, block, followers)
                if did_execute:
                    n_executed += 1
                else:
                    n_skipped += 1
                row += len(box_rows)
                continue

            wx.CallAfter(self.panel._on_row_start, row)
            try:
                method, linked_read, rate, equals_str = _get_command_method(
                    self.stem, device_name, command_name)
                cmd_fields = _find_command_fields(self.stem, device_name, command_name)
                value_label = f' [{value_str.strip()}]' if cmd_fields and '[%]' in cmd_fields[4] else ''

                if method == 'ramp':
                    # requirement: 'add a field to enter a ramp rate... if set
                    # to 0, you simply dont ramp. Any other value you
                    # always ramp' — applies to a plain constant set
                    # too, not just a multi-step sweep: it's a one-step
                    # 'sweep' from wherever the device currently is to
                    # the target, through the exact same
                    # _write_with_rate_limit a real sweep uses per step.
                    current_value = self._query_current_value(device_name, linked_read)
                    target_value = float(value_str)
                    self._write_with_rate_limit(device_name, command_name,
                                                target_value, current_value, rate)
                    method_note = f' (ramped at {rate:g}/s, based on {linked_read})'
                else:
                    final_str = _resolve_final_string(self.stem, device_name, command_name, value_str)
                    addr, wterm, rterm, timeout, baud, bits = _read_device_settings(self.stem, device_name)
                    self._execute_chained_write(addr, final_str, wterm, rterm, timeout, baud, bits)
                    method_note = ''

                if method == 'verify' and not self._abort.is_set():
                    # requirement: 'query a preselected command (as before)
                    # and wait until you are at that value' — mutually
                    # exclusive with ramp (a magnet controller with its
                    # own internal ramp engine: 'no overlap between
                    # defining a rate and the verifying' — you jump
                    # directly to setpoint here, then just wait for the
                    # controller's own ramp to finish. Bounded — see
                    # _verify_equals: read retries + match timeout.
                    resolved_equals = _resolve_equals_str(equals_str, value_str)
                    self._verify_equals(device_name, command_name, linked_read, resolved_equals)
                    method_note = f' (verified {linked_read} == {resolved_equals!r})'

                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')

                if self._abort.is_set():
                    # Aborted during the ramp or the verify wait — not a
                    # success, and not a write/read FAILURE either, so
                    # it gets its own message rather than reusing
                    # 'FAILED'. onhold (if any active device has one) is
                    # sent once, centrally, after this loop exits — see
                    # the end of run().
                    wx.CallAfter(self.panel._on_row_failed, row,
                                f'ABORTED: {alias} — stopped before completing\n'
                                f'{row_start} to {row_finish}\n\n')
                    n_skipped += 1
                else:
                    # v1 manual, section 4.1: 'constant: A single value is
                    # set to the device WITHOUT SUBSEQUENT DATA ACQUISITION.'
                    # Only x/nested-x/exp-x sweeps trigger reads in v1, and
                    # counter/a real sweep (see _run_timed_sweep) is v2's
                    # stand-in for that right now. An earlier version of
                    # this code queried every active read after every
                    # constant write, which was wrong on two counts: it
                    # doesn't match v1's own documented behavior, and it
                    # dumped read data into the chat log with no real data
                    # file behind it. Fixed.
                    entry_log = (f'Constant set: {alias}{value_label}{method_note}\n'
                                f'{row_start} to {row_finish}\n')
                    if comment:
                        entry_log += f'Comment: {comment}\n'
                    entry_log += '\n'
                    wx.CallAfter(self.panel._on_row_done, row, entry_log)
                    n_executed += 1
            except Exception as e:
                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                wx.CallAfter(self.panel._on_row_failed, row,
                            f'FAILED: {alias} — {e}\n{row_start} to {row_finish}\n\n')
                n_skipped += 1
            row += 1

        if self._abort.is_set():
            # qmeas v1.1 precedent (requested): any ACTIVE device with a
            # write command literally named 'onhold' gets it sent
            # immediately on abort — a superconducting magnet's own
            # internal ramp engine keeps going after this software
            # stops issuing setpoints, so stopping THIS thread is not
            # the same as stopping the MAGNET. onhold is the explicit
            # command that does that. Deliberately gated on
            # self._abort.is_set() specifically HERE, at the point
            # where the main loop has already exited because of abort
            # — not in any per-row cleanup, and not for a normal
            # (non-aborted) run finishing — a past version of this
            # exact feature had onhold fire on ordinary task completion
            # too, which is wrong: sending a magnet's hold command
            # every time a measurement finishes normally is not what
            # 'stop means stop' means.
            for device_name in self.onhold_devices:
                try:
                    addr, wterm, rterm, timeout, baud, bits = _read_device_settings(self.stem, device_name)
                    final_str = _resolve_final_string(self.stem, device_name, 'onhold', '')
                    self._execute_chained_write(addr, final_str, wterm, rterm, timeout, baud, bits)
                    wx.CallAfter(self.panel.on_log, f'Abort: sent {device_name}_onhold.\n')
                except Exception as e:
                    wx.CallAfter(self.panel.on_log,
                                f'Abort: FAILED to send {device_name}_onhold: {e}\n')
            if self._auto_abort_reason:
                # Abort came from _verify_equals, not the Stop button —
                # say so accurately (device, command, wall-clock time
                # and reason; the FAILED entry above carries row/step
                # detail). 'by user' here would be plainly wrong.
                wx.CallAfter(self.panel.on_log,
                            f'Run aborted automatically after row {row}: '
                            f'{self._auto_abort_reason}\n\n')
            else:
                wx.CallAfter(self.panel.on_log, f'Run aborted by user after row {row}.\n\n')

        wx.CallAfter(self.panel._on_run_finished, n_executed, n_skipped)

    def _run_timed_sweep(self, row: int, fetched: dict, row_start: str, is_counter: bool,
                         followers: list = None, block_rows: list = None) -> bool:
        """Unified per-step engine for control_counter (virtual, no
        real device write) AND a real device's Start/Final/Steps sweep.
        requirement: 'counter is in a sense just a device that doesnt accept
        commands' — so this is ONE method, not two: the only thing that
        differs is how 'reach this step's target value' is implemented
        (a no-op for counter; a rate-limited real write via
        _write_with_rate_limit otherwise). Everything else —
        interpolating Start->Final via Steps/_linspace, waiting
        Integration Time, reading every active query, accumulating a
        table with dynamically-discovered columns, writing it to a
        tab-separated file at the end, live-pushing progress to the
        Graph window, checking abort before every single sub-step — is
        identical either way.

        Reuses Start/Final/Steps exactly like v1's x/nested-x/exp-x
        sweeps would (via _linspace): the value at step i is the
        interpolated number, 'step number' is the raw index 0..Steps.
        Defaulting when Final is blank: value == Start + step number
        (so Start/Final both blank gives the simplest case, value ==
        step number, matching v1's plain description of 'counter'
        before v2 added the value/step distinction) — flag if different
        defaulting is wanted.

        Runs for the WHOLE duration of this single grid row: the row
        stays the active/frozen row throughout (same as any other
        in-progress row), not one frozen row per iteration — nothing
        about the live-fetch/frontier design needs to change for this.

        REAL DEVICE ONLY: before the loop, reads the command's Method
        (_get_command_method) — 'none'/'ramp'/'verify', mutually
        exclusive. For 'ramp', queries the linked read ONCE to
        establish the actual starting 'current value'
        (_query_current_value) — every value after that is tracked
        forward from what was last successfully WRITTEN (open-loop,
        write-and-forget; not re-queried every step). Every step's
        move to its target then goes through _write_with_rate_limit —
        requirement: 'this ramp applies generally also BETWEEN the steps from
        the task list... if the steps... are
        10V then you would still use a ramp of only 0.1V/s' — so the
        VERY FIRST call (current-value -> Start) and every subsequent
        step-to-step call are the exact same operation, not special-
        cased. For 'verify', each step's write jumps directly (no
        software ramping — mutually exclusive with 'ramp'), then polls
        the linked read (_verify_equals) until it exactly equals a
        configured value (bounded: read retries up to the device
        timeout, match wait capped at _VERIFY_MATCH_TIMEOUT_S). A write
        failure OR an exhausted verify at any point aborts the row
        outright (requirement: 'abort means abort, you stop dead') — no
        skip-and-continue, no
        retry; whatever was collected before the failure is still
        written to the data file, not discarded.

        Per iteration (all kinds): wait Integration Time (settle, same
        convention as v1 — parsed via _parse_duration_seconds, the ONLY
        field here with unit handling: '1', '1s', '2.5m', '3h' all
        valid; Start/Final/Steps stay plain numbers, no unit guessing,
        by request — 'I won't enter 1.5V or 0.5mA'), then read every
        active query (_execute_active_queries). Checks abort between
        every step and on every wait.

        Every field here that ISN'T blank but fails to parse fails the
        row loudly (via _on_row_failed) instead of silently
        substituting a default — that silent-default behavior is
        exactly what hid the Integration Time bug for three rounds:
        '1s' raised ValueError inside float('1s'), was caught, and
        silently became wait_s=0.0. Blank still means a sensible
        default (0 for Start/Steps, Start+Steps for Final, 0 for
        IntTime) — only a NON-blank, unparseable value is now an error.

        Columns (headers) are determined from the FIRST actual read,
        not decided in advance: a query's arity (does it return one
        value or several, e.g. a lock-in's X+Y) is only knowable once
        it's actually been queried. A query returning multiple values
        gets exploded into real separate columns ('alias[0]',
        'alias[1]', ...) — space-joining them into one string instead
        ('0.000219 -3.3e-05') never parses as a number again, so that
        whole channel would be silently NaN everywhere downstream. If a
        LATER read of the same query unexpectedly returns a different
        number of values (a genuine device inconsistency), that one row
        is padded/truncated to the established width rather than
        derailing every column after it.

        Reported PROGRESS, not just a final result: every iteration
        also pushes a throttled (at most 2/sec — see LIVE_PUSH_INTERVAL)
        live update to the Graph window via _on_sweep_progress, not
        only once at the very end via _on_sweep_done.

        At the end (Steps completed, aborted, OR a write failure —
        partial data is still written, not discarded), dumps everything
        to one tab-separated .dat file: header row, then one row per
        iteration.

        LINK BOX (followers/block_rows non-None — see _gather_link_box):
        per step, after THIS row (the mother) has fully reached its
        step target (ramp/verify included), each active follower
        evaluates its Constant/Start expression at [%]=target_value and
        writes the result through its OWN command's method
        (_write_follower_step), top to bottom — spec: mother moves,
        followers set, ALL of them, and only then Integration Time.
        Every follower references the MOTHER's setpoint, never a
        preceding follower. Follower validation happens upfront
        (_prepare_followers), before the mother writes anything. Each
        follower's computed value gets its own data file column
        ('{alias}_value'), right after the mother's. A follower failure
        fails the whole box, same as a mother write failure. With
        followers=None/block_rows=None (the default) every branch below
        behaves exactly as before this feature existed."""
        alias = fetched['alias']
        device_name, command_name = fetched['device_name'], fetched['command_name']
        value_str, final_str = fetched['value_str'], fetched['final_str']
        steps_str, inttime_str, comment = fetched['steps_str'], fetched['inttime_str'], fetched['comment']

        def _finish_failed(entry_log):
            # A link box highlights/locks all its rows via
            # _on_chain_start (see run()'s dispatch) — so completion
            # must release all of them too, via the chain variants.
            if block_rows:
                wx.CallAfter(self.panel._on_chain_failed, block_rows, entry_log)
            else:
                wx.CallAfter(self.panel._on_row_failed, row, entry_log)

        def _finish_done(entry_log):
            if block_rows:
                wx.CallAfter(self.panel._on_chain_done, block_rows, entry_log)
            else:
                wx.CallAfter(self.panel._on_row_done, row, entry_log)

        def fail(field_name, raw_value, hint=''):
            row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
            msg = f'FAILED: {alias} — {field_name} {raw_value!r} is not a valid number'
            if hint:
                msg += f' ({hint})'
            _finish_failed(f'{msg}\n{row_start} to {row_finish}\n\n')

        try:
            steps_n = int(steps_str) if steps_str.strip() else 0
        except ValueError:
            fail('Steps', steps_str)
            return False
        try:
            start_val = float(value_str) if value_str.strip() else 0.0
        except ValueError:
            fail('Constant/Start', value_str)
            return False
        try:
            final_val = float(final_str) if final_str.strip() else start_val + steps_n
        except ValueError:
            fail('Final', final_str)
            return False
        try:
            wait_s = _parse_duration_seconds(inttime_str) if inttime_str.strip() else 0.0
        except ValueError:
            fail('Integration Time', inttime_str,
                hint="plain number = seconds, or a suffix like '1s' / '2.5m' / '3h'")
            return False

        method, linked_read, rate, equals_str = 'none', None, 0.0, ''
        current_value = start_val   # meaningless for counter; real device may override below
        if not is_counter:
            try:
                method, linked_read, rate, equals_str = _get_command_method(
                    self.stem, device_name, command_name)
            except ValueError as e:
                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                _finish_failed(f'FAILED: {alias} — {e}\n{row_start} to {row_finish}\n\n')
                return False
            if method == 'ramp':
                try:
                    current_value = self._query_current_value(device_name, linked_read)
                except Exception as e:
                    row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                    _finish_failed(f'FAILED: {alias} — could not determine current output value: {e}\n'
                                   f'{row_start} to {row_finish}\n\n')
                    return False

        # Link-box followers: validate ALL of them before the mother
        # writes anything (see _prepare_followers) — a bad expression,
        # a missing [%] placeholder, or an unreachable ramp read fails
        # the whole box here, with the hardware untouched.
        prepared_followers = []
        if followers:
            try:
                prepared_followers = self._prepare_followers(followers, test_value=start_val)
            except Exception as e:
                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                _finish_failed(f'FAILED: {alias} (link box) — {e}\n{row_start} to {row_finish}\n\n')
                return False

        sweep_values = _linspace(start_val, final_val, steps_n)
        # requirement: 'do not save the value that is used to verify. It is
        # not needed... do not include the results of a string verify
        # read in the data file.' The read still happens as part of
        # verify itself (_verify_equals above) — this only stops it
        # from ALSO landing in the recorded data, which is what
        # happens if the same command's active-query LED is also on
        # (e.g. left on so its status is visible live during the run).
        # Bare names (element suffix stripped): the exclude matches against
        # active-query specs, which are always bare command names.
        exclude_reads = ({(device_name, _split_element_suffix(linked_read)[0])}
                         if (not is_counter and method == 'verify') else set())
        for fol in prepared_followers:
            # Same rule for a verify-method follower: its verify read is
            # part of the write handshake, not recorded data.
            if fol['method'] == 'verify':
                exclude_reads.add((fol['device_name'], _split_element_suffix(fol['linked_read'])[0]))

        wx.CallAfter(self.panel._sweep_ui_start, len(sweep_values))
        data_rows = []
        headers = None   # determined from the FIRST actual read — see docstring
        t0 = time.time()
        aborted_early = False
        step_failed = None
        last_live_push = 0.0
        LIVE_PUSH_INTERVAL = 0.5   # seconds — throttled so a fast sweep
                                   # (short IntTime, or none) doesn't flood
                                   # the main thread with redraw requests
        for step_number, target_value in enumerate(sweep_values):
            if self._abort.is_set():
                aborted_early = True
                break

            wx.CallAfter(self.panel._sweep_ui_step_progress,
                        f'({alias}[{step_number + 1}/{len(sweep_values)}])')

            if not is_counter:
                try:
                    if method == 'ramp':
                        current_value = self._write_with_rate_limit(
                            device_name, command_name, target_value, current_value, rate)
                    else:
                        # method == 'none' or 'verify': jump directly.
                        # For 'verify', the device's own internal ramp
                        # (if any) handles the actual physical transit
                        # — no software-side stepping to do here, requirement:
                        # 'no overlap between defining a rate and the
                        # verifying.'
                        final_str = _resolve_final_string(
                            self.stem, device_name, command_name, str(target_value))
                        addr, wterm, rterm, timeout, baud, bits = _read_device_settings(
                            self.stem, device_name)
                        self._execute_chained_write(addr, final_str, wterm, rterm, timeout, baud, bits)
                except Exception as e:
                    step_failed = e
                    break
                if self._abort.is_set():
                    aborted_early = True
                    break
                if method == 'verify':
                    # requirement: 'query a preselected command (as before)
                    # and wait until you are at that value until you
                    # continue with the next step' — this is what
                    # actually matters for a magnet: the write above
                    # only sends the setpoint, it doesn't know or care
                    # how long the CONTROLLER takes to physically get
                    # there. Bounded — see _verify_equals: read retries
                    # up to the device timeout, match wait capped.
                    try:
                        self._verify_equals(device_name, command_name, linked_read,
                                            _resolve_equals_str(equals_str, str(target_value)))
                    except Exception as e:
                        step_failed = e
                        break
                    if self._abort.is_set():
                        aborted_early = True
                        break

            # Link-box followers: the mother has now fully reached this
            # step's target (ramp/verify above included) — write every
            # follower's f([%]=target_value), top to bottom, and ONLY
            # THEN run Integration Time below (spec: mother moves,
            # linked device sets, ALL linked do that, and only then the
            # integration time). Runs for a counter mother too — that's
            # the point of linking to a counter. A follower failure
            # fails the whole box, same as a mother write failure.
            follower_values = []
            if prepared_followers:
                try:
                    for fol in prepared_followers:
                        follower_values.append(self._write_follower_step(fol, target_value))
                except Exception as e:
                    step_failed = e
                    break
                if self._abort.is_set():
                    aborted_early = True
                    break

            self._sleep_checking_abort(wait_s)
            if self._abort.is_set():
                aborted_early = True
                break
            reads = self._execute_active_queries(exclude_reads)   # [(alias, value), ...]
            elapsed = time.time() - t0
            row_values = [target_value] + follower_values + [step_number, elapsed]
            for _alias, value in reads:
                if isinstance(value, list):
                    row_values.extend(value)   # one column PER sub-value — see docstring
                else:
                    row_values.append(value)

            if headers is None:
                value_header = 'counter_value' if is_counter else 'device_value'
                headers = [value_header] \
                    + [f'{fol["alias"]}_value' for fol in prepared_followers] \
                    + ['step_number', 'elapsed_s']
                for alias_, value in reads:
                    # alias_ is already exactly f'{device}_{command}' (see
                    # _execute_active_queries) — deriving it again via
                    # zip(self.query_specs, reads) would misalign once
                    # exclude_reads makes reads shorter than query_specs
                    # (a verify-linked read removed from the middle, say).
                    if isinstance(value, list):
                        headers.extend(f'{alias_}[{i}]' for i in range(len(value)))
                    else:
                        headers.append(alias_)
            elif len(row_values) != len(headers):
                row_values = (row_values + [float('nan')] * len(headers))[:len(headers)]

            data_rows.append(row_values)
            wx.CallAfter(self.panel._sweep_ui_progress, step_number + 1)

            now = time.time()
            if now - last_live_push >= LIVE_PUSH_INTERVAL or step_number == 0:
                wx.CallAfter(self.panel._on_sweep_progress, list(headers), data_rows[:])
                last_live_push = now
        wx.CallAfter(self.panel._sweep_ui_stop)

        if headers is None:
            value_header = 'counter_value' if is_counter else 'device_value'
            headers = [value_header] \
                + [f'{fol["alias"]}_value' for fol in prepared_followers] \
                + ['step_number', 'elapsed_s'] \
                + [f'{d}_{c}' for d, c in self.query_specs]

        row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
        filepath = None
        try:
            filepath = self._write_sweep_datafile(headers, data_rows)
        except Exception as e:
            wx.CallAfter(self.panel.on_log, f'Sweep data file FAILED to write: {e}\n\n')

        wx.CallAfter(self.panel._on_sweep_done, headers, data_rows, filepath)

        n_done = len(data_rows)
        n_planned = len(sweep_values)
        kind = 'Counter' if is_counter else 'Sweep'

        if step_failed is not None:
            entry_log = (f'FAILED: {alias} — step {len(data_rows)} failed: {step_failed}\n'
                        f'{n_done}/{n_planned} steps completed before the failure\n'
                        f'{row_start} to {row_finish}\n'
                        f'Data file: {filepath if filepath else "(failed to write)"}\n')
            if comment:
                entry_log += f'Comment: {comment}\n'
            entry_log += '\n'
            _finish_failed(entry_log)
            return False

        entry_log = (f'{kind}: {alias} — {n_done}/{n_planned} steps completed'
                    f'{" (aborted)" if aborted_early else ""}\n')
        if not is_counter and method == 'ramp':
            entry_log += f'Ramp rate: {rate:g}/s (based on {linked_read})\n'
        if not is_counter and method == 'verify':
            entry_log += f'Verify: {linked_read} == {equals_str!r}\n'
        for fol in prepared_followers:
            entry_log += f'Linked: {fol["alias"]} = {fol["expr"]} (of {alias})\n'
        entry_log += (f'{row_start} to {row_finish}\n'
                     f'Data file: {filepath if filepath else "(failed to write)"}\n')
        if comment:
            entry_log += f'Comment: {comment}\n'
        entry_log += '\n'

        if aborted_early:
            _finish_failed(entry_log)
            return False
        _finish_done(entry_log)
        return True

    def _run_while_loop(self, row: int, fetched: dict, row_start: str) -> bool:
        """A virtual while-command on the control device: 'repeat until
        verify condition is met' (requested) — polls its configured exit
        query (fields 19-23, see _parse_while_definition) once per
        Integration Time, recording a data row each cycle like a
        counter, and exits successfully when the comparison is true.

        Deliberately its OWN method, not another mode flag on
        _run_timed_sweep — that function stays byte-identical (it's in
        the hardware-tested field-sweep path); the shared machinery
        (_execute_active_queries, _sleep_checking_abort,
        _write_sweep_datafile, the throttled live push) is reused by
        call, not by entanglement.

        Cycle order (requirement: 'more like a counter... the user only
        defines an integration time and that is the time you query all
        devices; the data file then saves the step and the passed
        time'): wait Integration Time -> query the exit read -> query
        every active device -> record step_number/elapsed_s/queries ->
        evaluate -> exit if met. The final, matching cycle IS recorded.
        The exit read itself is EXCLUDED from the recorded data
        (verify parity: 'do not save the value that is used to
        verify') unless that same query is separately LED-active.

        Integration Time: while-specific floor of 0.1s; 0/blank is
        REJECTED here (unlike every other row type, where IntTime 0 is
        legal) — an unbounded loop with no wait would hammer the
        instrument. This minimum is scoped to this method only.

        ONE failure rule, no subtle tiers (requirement: 'timeout=failure',
        confirmed as verify parity): EVERY failure of a while row —
        timeout elapsed, exit-read retries exhausted, a non-numeric
        read against </>, the watched device inactive at start, an
        unparseable field, a missing/legacy definition — ABORTS THE
        WHOLE RUN, onhold included, through the same central mechanism
        as a verify failure. The entire purpose of this row type is to
        gate progression on system state; any failure of it means the
        gate did not do its job, and rows after it would run at
        unknown conditions (a magnet not actually at field). Partial
        data collected before the failure is still written, matching
        every other row type. Stop/abort mid-poll: ordinary abort, NOT
        the auto-abort-reason path — partial data written, run()'s
        central onhold block applies as for any user abort.

        Exit-read failures get verify's exact retry semantics: retried
        each cycle until a CONTIGUOUS failure streak spans the watched
        device's own configured timeout (a one-off WinError 10054
        doesn't kill an overnight wait); a good read resets the
        budget. Active-query failures stay non-fatal error strings
        inline, exactly as everywhere else (_execute_active_queries).

        The wall-clock timeout (field 23, '0' = infinite — mandatory
        and pre-validated in WhileEditorDialog, defensively re-parsed
        here since files are hand-editable) is checked at the top of
        every cycle, BEFORE sleeping into another interval."""
        alias = fetched['alias']
        command_name = fetched['command_name']
        inttime_str, comment = fetched['inttime_str'], fetched['comment']

        def _fail_and_abort(reason: str, filepath=None, n_polls=0):
            """Every while failure ends here — verify's
            _abort_run_and_hold pattern: record the reason for the
            closing log line, set the abort flag (routes through
            run()'s central onhold block), mark the row FAILED."""
            self._auto_abort_reason = (
                f'while failure on {STANDARD_DEVICE_NAME}_{command_name} at '
                f'{time.strftime("%Y-%m-%d %H:%M:%S")} — {reason}')
            self._abort.set()
            row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
            entry_log = f'FAILED: {alias} — {reason}\n'
            if n_polls:
                entry_log += f'{n_polls} polls completed before the failure\n'
            entry_log += f'{row_start} to {row_finish}\n'
            if filepath is not None:
                entry_log += f'Data file: {filepath}\n'
            if comment:
                entry_log += f'Comment: {comment}\n'
            entry_log += '\n'
            wx.CallAfter(self.panel._on_row_failed, row, entry_log)

        definition = _get_while_definition(self.stem, command_name)
        if definition is None:
            # Not a valid while: a legacy dead command created through
            # the old generic Add Command path (which never worked on
            # control — no control.txt to read), or a hand-mangled
            # line. A clear message instead of the generic path's
            # baffling file-not-found error.
            _fail_and_abort(f"'{command_name}' on {STANDARD_DEVICE_NAME} is not a "
                            f"valid while-command (no exit condition defined) — "
                            f"recreate it via right-click {STANDARD_DEVICE_NAME} "
                            f"\u2192 Add Virtual While...")
            return False
        if fetched.get('has_child'):
            # Nesting a while ('for each gate voltage: ramp field, wait
            # for HOLD, measure') is a natural FUTURE extension, spec'd
            # as explicitly out of scope for this first round — rejected
            # rather than silently run standalone with its child orphaned.
            _fail_and_abort("a while row can't have nested rows under it "
                            "(not supported in this version)")
            return False
        if fetched.get('has_follower'):
            # A link-box mother's followers evaluate f([%]=step target)
            # per step — a while has no per-step target value at all,
            # so there is nothing for a follower to reference.
            _fail_and_abort("a while row can't be a link-box mother — it has no "
                            "per-step value for linked rows to reference")
            return False
        linked_device = definition['linked_device']
        linked_read = definition['linked_read']
        operator = definition['operator']
        threshold = definition['threshold']

        try:
            wait_s = _parse_duration_seconds(inttime_str) if inttime_str.strip() else 0.0
        except ValueError:
            _fail_and_abort(f'Integration Time {inttime_str!r} is not a valid number '
                            f"(plain number = seconds, or a suffix like '1s' / '2.5m' / '3h')")
            return False
        if wait_s < 0.1:
            _fail_and_abort(f'Integration Time {inttime_str.strip()!r} — a while row '
                            f'needs the poll interval here, minimum 0.1s (0/blank not '
                            f'allowed for this row type)')
            return False
        try:
            timeout_s = _parse_duration_seconds(definition['timeout_raw'])
            if timeout_s < 0:
                raise ValueError('negative')
        except ValueError:
            _fail_and_abort(f"timeout {definition['timeout_raw']!r} in the while "
                            f"definition is not a valid duration — re-save it via "
                            f"Edit Virtual While...")
            return False
        if operator in ('<', '>'):
            # Save-time validation blocks this, but files are hand-editable.
            try:
                float(threshold.strip())
            except ValueError:
                _fail_and_abort(f"'{operator}' needs a numeric value, but the while "
                                f"definition has {threshold.strip()!r} — re-save it "
                                f"via Edit Virtual While...")
                return False
        if self.active_devices is not None and linked_device not in self.active_devices:
            # Single check at loop start, not per poll — the Devices
            # tree is frozen for the whole run (see _on_left_down's
            # guard), so active/inactive cannot change mid-run.
            _fail_and_abort(f"the exit condition watches '{linked_device}_{linked_read}' "
                            f"but device '{linked_device}' is not active (LED off)")
            return False

        # Exit-read retry budget: the watched device's own configured
        # timeout, same source as verify's (ms -> s, 5s fallback).
        try:
            retry_budget_s = float(_read_device_settings(self.stem, linked_device)[3]) / 1000.0
        except Exception:
            retry_budget_s = 5.0

        # The exit read is executed ONCE per poll and serves double
        # duty: its element (or scalar) drives the exit condition, and
        # its FULL raw value is recorded among the active reads IF the
        # watched query's LED is green (i.e. it's in query_specs) —
        # via _execute_active_queries' preread mechanism, so the
        # instrument is never queried twice in one cycle. Recording is
        # governed by the LED exactly like every other query: green =
        # saved (ALL elements of a split read), red/grey = not saved.
        # (Round 9 briefly excluded the watched query outright, which
        # silently dropped a green primary reading — readvalues — from
        # the data and Graph; round 1's own spec already said 'unless
        # that same query is separately switched on'.)
        bare_exit_read = _split_element_suffix(linked_read)[0]

        wx.CallAfter(self.panel._while_ui_start)
        wx.CallAfter(self.panel._wait_status_show,
                     f'{alias} is waiting for {linked_device}_{linked_read} '
                     f'{operator} {threshold.strip()}. Please wait!')
        data_rows = []
        headers = None   # from the FIRST actual read, same as _run_timed_sweep
        polls = 0
        t0 = time.time()
        read_fail_start = None   # first failure in the current streak
        read_attempts = 0
        last_live_push = 0.0
        LIVE_PUSH_INTERVAL = 0.5
        outcome, fail_reason = None, None
        try:
            while True:
                if self._abort.is_set():
                    outcome = 'aborted'
                    break
                if timeout_s > 0 and time.time() - t0 >= timeout_s:
                    last = ''
                    if data_rows:
                        last = f' after {polls} polls'
                    outcome, fail_reason = 'failed', (
                        f'while timed out after {timeout_s:g}s{last}: '
                        f'{linked_device}_{linked_read} never satisfied '
                        f'{operator} {threshold.strip()!r}')
                    break
                self._sleep_checking_abort(wait_s)
                if self._abort.is_set():
                    outcome = 'aborted'
                    break
                try:
                    raw_exit = _execute_query(self.stem, linked_device, bare_exit_read)
                    exit_value = _extract_element(raw_exit, linked_read)
                except Exception as e:
                    now = time.time()
                    if read_fail_start is None:
                        read_fail_start = now
                        read_attempts = 0
                    read_attempts += 1
                    if now - read_fail_start >= retry_budget_s:
                        outcome, fail_reason = 'failed', (
                            f'exit-condition read failed {read_attempts} times over '
                            f'{now - read_fail_start:.1f}s (retried every poll up to '
                            f"the device timeout, {retry_budget_s:g}s) — last error: {e}")
                        break
                    continue   # retry next cycle; the wall-clock timeout above still ticks
                read_fail_start = None   # a good read resets the retry budget
                polls += 1
                wx.CallAfter(self.panel._sweep_ui_step_progress, f'({alias}: poll {polls})')
                reads = self._execute_active_queries(
                    preread={(linked_device, bare_exit_read): raw_exit})
                elapsed = time.time() - t0
                row_values = [polls - 1, elapsed]
                for _alias, value in reads:
                    if isinstance(value, list):
                        row_values.extend(value)
                    else:
                        row_values.append(value)
                if headers is None:
                    headers = ['step_number', 'elapsed_s']
                    for alias_, value in reads:
                        if isinstance(value, list):
                            headers.extend(f'{alias_}[{i}]' for i in range(len(value)))
                        else:
                            headers.append(alias_)
                elif len(row_values) != len(headers):
                    row_values = (row_values + [float('nan')] * len(headers))[:len(headers)]
                data_rows.append(row_values)
                now = time.time()
                if now - last_live_push >= LIVE_PUSH_INTERVAL or polls == 1:
                    wx.CallAfter(self.panel._on_sweep_progress, list(headers), data_rows[:])
                    last_live_push = now
                try:
                    if _while_condition_met(exit_value, operator, threshold):
                        outcome = 'success'
                        break
                except ValueError:
                    outcome, fail_reason = 'failed', (
                        f'{linked_device}_{linked_read} returned '
                        f'{str(exit_value).strip()!r}, which is not a number — '
                        f"'{operator}' cannot compare it (wrong linked query, or a "
                        f'status string where a numeric readback was expected)')
                    break
        finally:
            wx.CallAfter(self.panel._wait_status_hide)
            wx.CallAfter(self.panel._while_ui_stop)

        if headers is None:
            # Zero completed polls (aborted/failed before the first
            # read): best-effort header over every green query — the
            # watched one included, since recording now follows the
            # LED. (A split read's per-element columns can't be known
            # without a real read; the file has no data rows anyway.)
            headers = ['step_number', 'elapsed_s'] \
                + [f'{d}_{c}' for d, c in self.query_specs]

        row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
        filepath = None
        try:
            filepath = self._write_sweep_datafile(headers, data_rows)
        except Exception as e:
            wx.CallAfter(self.panel.on_log, f'While data file FAILED to write: {e}\n\n')
        wx.CallAfter(self.panel._on_sweep_done, headers, data_rows, filepath)

        if outcome == 'failed':
            _fail_and_abort(fail_reason,
                            filepath=filepath if filepath else '(failed to write)',
                            n_polls=polls)
            return False

        elapsed_total = time.time() - t0
        entry_log = (f'While: {alias} — '
                     f'{"condition met" if outcome == "success" else "aborted"} '
                     f'after {polls} polls ({elapsed_total:.1f}s)\n'
                     f'Exit condition: {linked_device}_{linked_read} '
                     f'{operator} {threshold.strip()!r}\n'
                     f'{row_start} to {row_finish}\n'
                     f'Data file: {filepath if filepath else "(failed to write)"}\n')
        if comment:
            entry_log += f'Comment: {comment}\n'
        entry_log += '\n'
        if outcome == 'aborted':
            wx.CallAfter(self.panel._on_row_failed, row, entry_log)
            return False
        wx.CallAfter(self.panel._on_row_done, row, entry_log)
        return True

    def _run_nested_sweep(self, chain_rows: list, chain_fetched: list, row_start: str) -> bool:
        """Executes a chain of 2+ rows at strictly increasing nesting
        depth as genuine nested for-loops. requirement: 'one loop counts e.g.
        0,1,2,3,4 and then the other loop increments e.g. 0->1 and then
        one loop counts again... just like chained for-loops.'
        chain_rows/chain_fetched are OUTERMOST-first (index 0 is the
        row things are nested under; index -1 is the innermost, the one
        with no child of its own — see _gather_nested_chain).

        Each level's own Start/Final/Steps/IntTime/method is parsed and
        validated UPFRONT, exactly like _run_timed_sweep does for a
        single level (same fail-loud-on-bad-input; same per-level
        ramp/verify/counter handling via _get_command_method) — a
        failure at ANY level aborts the whole nested block before
        anything is written, not partway through. A control row with no
        sweep values of its own (pause/userprompt/stop) can't be a
        chain member and fails loudly rather than being silently
        skipped or misinterpreted.

        Execution is genuinely recursive (_run_nested_level): the
        outermost level loops through its own Start->Final->Steps
        values; for each one, it writes/ramps/verifies its own target,
        waits its OWN Integration Time, then recurses one level deeper
        — which does the exact same thing — all the way down to the
        innermost level, which additionally performs the actual
        measurement (reads every active query, records one data row)
        instead of recursing further. This reproduces classic nested-
        loop semantics exactly: the innermost level completes its
        ENTIRE sweep for every single step of the level above it, and
        so on up the chain.

        Data file / live graph columns, per the user's exact spec: for each
        level, outermost to innermost — '{level}_count', '{level}_value'
        (count before value, matching his order) — then 'elapsed_s' (one
        clock for the whole nested block, started once at the very
        beginning, not reset per level), then one column per active
        query — identical tail to the single-level sweep's own file
        format. Level names: 'outer', then 'inner1', 'inner2', ... for
        each level below that.

        A write/verify/read failure anywhere aborts the WHOLE block
        (requirement: 'abort means abort') — every row in the chain gets
        marked via _on_chain_failed together, not just the level that
        actually failed, so nothing in the chain looks like it silently
        succeeded."""
        level_specs = []
        aliases = [f['alias'] for f in chain_fetched]

        for i, fetched in enumerate(chain_fetched):
            alias = fetched['alias']
            device_name, command_name = fetched['device_name'], fetched['command_name']
            is_counter_level = (device_name == STANDARD_DEVICE_NAME and command_name == 'counter')

            if (device_name == STANDARD_DEVICE_NAME and command_name != 'counter') \
                    or device_name == SCRIPT_DEVICE_NAME:
                # Was: in ('pause', 'userprompt', 'stop') — widened to
                # 'anything on control except counter' so while-commands
                # (and legacy dead generic entries) are rejected as
                # chain members too. Nesting a while is explicitly out
                # of scope this round (see _run_while_loop's has_child
                # rejection for the parent-side counterpart); counter
                # remains the ONLY control command with sweep values.
                what = f"'{command_name}'" if device_name == STANDARD_DEVICE_NAME else 'a script'
                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                wx.CallAfter(self.panel._on_chain_failed, chain_rows,
                            f"FAILED: {alias} — {what} has no sweep values and can't "
                            f"be part of a nested chain\n{row_start} to {row_finish}\n\n")
                return False

            value_str, final_str = fetched['value_str'], fetched['final_str']
            steps_str, inttime_str = fetched['steps_str'], fetched['inttime_str']

            def fail(field_name, raw_value, hint='', _alias=alias):
                row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                msg = f'FAILED: {_alias} — {field_name} {raw_value!r} is not a valid number'
                if hint:
                    msg += f' ({hint})'
                wx.CallAfter(self.panel._on_chain_failed, chain_rows,
                            f'{msg}\n{row_start} to {row_finish}\n\n')

            try:
                steps_n = int(steps_str) if steps_str.strip() else 0
            except ValueError:
                fail('Steps', steps_str)
                return False
            try:
                start_val = float(value_str) if value_str.strip() else 0.0
            except ValueError:
                fail('Constant/Start', value_str)
                return False
            try:
                final_val = float(final_str) if final_str.strip() else start_val + steps_n
            except ValueError:
                fail('Final', final_str)
                return False
            try:
                wait_s = _parse_duration_seconds(inttime_str) if inttime_str.strip() else 0.0
            except ValueError:
                fail('Integration Time', inttime_str,
                    hint="plain number = seconds, or a suffix like '1s' / '2.5m' / '3h'")
                return False

            method, linked_read, rate, equals_str = 'none', None, 0.0, ''
            if not is_counter_level:
                try:
                    method, linked_read, rate, equals_str = _get_command_method(
                        self.stem, device_name, command_name)
                except ValueError as e:
                    row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
                    wx.CallAfter(self.panel._on_chain_failed, chain_rows,
                                f'FAILED: {alias} — {e}\n{row_start} to {row_finish}\n\n')
                    return False

            level_specs.append({
                'alias': alias, 'device_name': device_name, 'command_name': command_name,
                'is_counter': is_counter_level,
                'sweep_values': _linspace(start_val, final_val, steps_n),
                'wait_s': wait_s, 'method': method, 'linked_read': linked_read,
                'rate': rate, 'equals_str': equals_str,
            })

        level_names = ['outer'] + [f'inner{i}' for i in range(1, len(level_specs))]

        # requirement: 'do not save the value that is used to verify... do not
        # include the results of a string verify read in the data
        # file' — combined across every level that uses method='verify'
        # (each level's linked read is on ITS OWN device, so no
        # collisions between levels are possible here).
        exclude_reads = {(spec['device_name'], _split_element_suffix(spec['linked_read'])[0]) for spec in level_specs
                         if not spec['is_counter'] and spec['method'] == 'verify'}

        total_measurements = 1
        for spec in level_specs:
            total_measurements *= max(len(spec['sweep_values']), 1)

        wx.CallAfter(self.panel._sweep_ui_start, total_measurements)

        state = {
            'headers': None, 'data_rows': [], 't0': time.time(),
            'aborted': False, 'failed': None,
            'measurements_done': 0, 'last_live_push': 0.0,
            'level_names': level_names, 'total': total_measurements,
            'exclude_reads': exclude_reads,
            'level_progress': [None] * len(level_specs),   # per-level (alias, step+1, total) — see
                                                            # _run_nested_level's _push_step_progress
        }

        self._run_nested_level(0, level_specs, [], state)

        wx.CallAfter(self.panel._sweep_ui_stop)

        row_finish = time.strftime('%Y-%m-%d %H:%M:%S')
        filepath = None
        try:
            if state['headers'] is not None:
                filepath = self._write_sweep_datafile(state['headers'], state['data_rows'])
        except Exception as e:
            wx.CallAfter(self.panel.on_log, f'Nested sweep data file FAILED to write: {e}\n\n')

        wx.CallAfter(self.panel._on_sweep_done, state['headers'] or [], state['data_rows'], filepath)

        chain_desc = ' -> '.join(aliases)
        n_done = state['measurements_done']

        if state['failed'] is not None:
            entry_log = (f'FAILED: nested chain {chain_desc} — {state["failed"]}\n'
                        f'{n_done}/{total_measurements} measurements completed before the failure\n'
                        f'{row_start} to {row_finish}\n'
                        f'Data file: {filepath if filepath else "(failed to write)"}\n\n')
            wx.CallAfter(self.panel._on_chain_failed, chain_rows, entry_log)
            return False

        entry_log = (f'Nested sweep: {chain_desc} — {n_done}/{total_measurements} measurements'
                    f'{" (aborted)" if state["aborted"] else ""}\n'
                    f'{row_start} to {row_finish}\n'
                    f'Data file: {filepath if filepath else "(failed to write)"}\n\n')

        if state['aborted']:
            wx.CallAfter(self.panel._on_chain_failed, chain_rows, entry_log)
            return False
        wx.CallAfter(self.panel._on_chain_done, chain_rows, entry_log)
        return True

    def _push_step_progress(self, state: dict):
        """Builds and pushes the '(alias[step/total], ...)' display —
        requirement: 'just by looking at the devices, I wouldnt be able to
        tell where I am... add a bracket that shows (x1[current step/
        max steps], x2[current step/max step]...etc).' One entry per
        level that has actually started at least one step so far,
        outermost first — a level deeper than however far execution has
        currently reached has no meaningful 'current step' yet, so it's
        skipped rather than shown as step 0 (which would misleadingly
        look like it's already running)."""
        parts = [f'{alias}[{step}/{total}]' for entry in state['level_progress']
                if entry is not None for alias, step, total in [entry]]
        wx.CallAfter(self.panel._sweep_ui_step_progress, '(' + ', '.join(parts) + ')')

    def _run_nested_level(self, level_idx: int, level_specs: list, path: list, state: dict):
        """Recursive core of _run_nested_sweep. path is a list of
        (step_number, value) tuples for every ancestor level (0..
        level_idx-1), outermost first. Loops level_specs[level_idx]'s
        own sweep_values; for each one, writes/ramps/verifies (unless
        this level is control_counter, which writes nothing — 'counter
        is in a sense just a device that doesnt accept commands'),
        waits this level's OWN Integration Time, then either recurses
        into level_idx+1 or — if this is the innermost level (no
        further entries in level_specs) — records one measurement via
        _record_nested_measurement.

        Mutates state in place rather than returning a value: abort/
        failure needs to unwind every currently-active recursive call
        immediately, and checking state['aborted']/state['failed']
        after each recursive call (rather than threading a return value
        through every level) is what makes that unwind simple regardless
        of how deep the chain is."""
        spec = level_specs[level_idx]
        is_last_level = (level_idx == len(level_specs) - 1)
        current_value = spec['sweep_values'][0] if spec['sweep_values'] else 0.0

        if not spec['is_counter'] and spec['method'] == 'ramp':
            try:
                current_value = self._query_current_value(spec['device_name'], spec['linked_read'])
            except Exception as e:
                state['failed'] = e
                return

        for step_number, target_value in enumerate(spec['sweep_values']):
            if self._abort.is_set():
                state['aborted'] = True
                return

            state['level_progress'][level_idx] = (spec['alias'], step_number + 1, len(spec['sweep_values']))
            for deeper in range(level_idx + 1, len(level_specs)):
                state['level_progress'][deeper] = None   # hasn't started yet for this new step
            self._push_step_progress(state)

            if not spec['is_counter']:
                try:
                    if spec['method'] == 'ramp':
                        current_value = self._write_with_rate_limit(
                            spec['device_name'], spec['command_name'],
                            target_value, current_value, spec['rate'])
                    else:
                        final_str = _resolve_final_string(
                            self.stem, spec['device_name'], spec['command_name'], str(target_value))
                        addr, wterm, rterm, timeout, baud, bits = _read_device_settings(
                            self.stem, spec['device_name'])
                        self._execute_chained_write(addr, final_str, wterm, rterm, timeout, baud, bits)
                except Exception as e:
                    state['failed'] = e
                    return
                if self._abort.is_set():
                    state['aborted'] = True
                    return
                if spec['method'] == 'verify':
                    try:
                        self._verify_equals(spec['device_name'], spec['command_name'],
                                            spec['linked_read'],
                                            _resolve_equals_str(spec['equals_str'], str(target_value)))
                    except Exception as e:
                        state['failed'] = e
                        return
                    if self._abort.is_set():
                        state['aborted'] = True
                        return

            self._sleep_checking_abort(spec['wait_s'])
            if self._abort.is_set():
                state['aborted'] = True
                return

            new_path = path + [(step_number, target_value)]

            if is_last_level:
                self._record_nested_measurement(new_path, state)
            else:
                self._run_nested_level(level_idx + 1, level_specs, new_path, state)

            if state['failed'] is not None or state['aborted']:
                return

    def _record_nested_measurement(self, path: list, state: dict):
        """Innermost-level measurement for one specific combination of
        every ancestor level's current step — reads every active
        query and appends one row: {level}_count, {level}_value for
        each level outermost-first (count before value, by request),
        then elapsed_s (state['t0'], set once at the very start of the
        whole nested block), then the read columns — same dynamic-
        arity column discovery as the single-level sweep (a multi-value
        read explodes into real 'alias[i]' columns from its first
        actual reading, not a space-joined string)."""
        reads = self._execute_active_queries(state.get('exclude_reads'))
        elapsed = time.time() - state['t0']
        row_values = []
        for step_number, value in path:
            row_values.extend([step_number, value])
        row_values.append(elapsed)
        for _alias, value in reads:
            if isinstance(value, list):
                row_values.extend(value)
            else:
                row_values.append(value)

        if state['headers'] is None:
            headers = []
            for name in state['level_names']:
                headers.extend([f'{name}_count', f'{name}_value'])
            headers.append('elapsed_s')
            for alias_, value in reads:
                # alias_ is already exactly f'{device}_{command}' — see
                # the single-level sweep's identical fix for why this
                # doesn't re-derive it via zip(self.query_specs, reads),
                # which breaks alignment once exclude_reads makes reads
                # shorter than query_specs.
                if isinstance(value, list):
                    headers.extend(f'{alias_}[{i}]' for i in range(len(value)))
                else:
                    headers.append(alias_)
            state['headers'] = headers
        elif len(row_values) != len(state['headers']):
            row_values = (row_values + [float('nan')] * len(state['headers']))[:len(state['headers'])]

        state['data_rows'].append(row_values)
        state['measurements_done'] += 1
        wx.CallAfter(self.panel._sweep_ui_progress, state['measurements_done'])

        now = time.time()
        if now - state['last_live_push'] >= 0.5 or state['measurements_done'] == 1:
            wx.CallAfter(self.panel._on_sweep_progress, list(state['headers']), state['data_rows'][:])
            state['last_live_push'] = now

    def _write_sweep_datafile(self, headers: list, data_rows: list):
        """One dump at the end, not incremental writes — matches what
        was asked for. Trade-off worth knowing: an app crash mid-run
        loses everything collected so far, since nothing hits disk until
        this call. Fine for now; say if you want incremental appends
        instead. Filename matches v1's date_time.dat convention, plus
        the existing file_suffix setting (already meant for exactly
        this — 'appended to every file from the same measurement run').
        Used by both control_counter and a real device's Steps>=1
        sweep — the file format doesn't care which produced it."""
        settings = load_settings()
        data_dir = Path(settings['data_path'])
        data_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        suffix = settings.get('file_suffix', '')
        filepath = data_dir / f'{timestamp}{suffix}.dat'
        with open(filepath, 'w', encoding='ascii', errors='replace') as f:
            f.write('\t'.join(headers) + '\n')
            for row_values in data_rows:
                f.write('\t'.join(str(v) for v in row_values) + '\n')
        return filepath

    def _execute_active_queries(self, exclude: set = None, preread: dict = None) -> list:
        """Execute every entry in self.query_specs (snapshotted once at
        Start — see class docstring for why this one stays a snapshot)
        and return [(alias, value_or_error_string), ...] in order.
        Shared by the counter's per-step data collection — this is the
        ONLY place queries execute now; plain constant-value writes no
        longer call this (see the fix note in run()). A single query
        failing (device off/disconnected, bad extraction settings,
        timeout) is recorded as an error string inline and does NOT
        raise or abort the counter — one bad reading shouldn't cost the
        rest of the step, let alone the rest of the run.

        exclude: (device_name, command_name) pairs to skip entirely —
        used to omit a command that's ALSO configured as a verify
        Method's linked read from the recorded data (requirement: 'do not
        save the value that is used to verify. It is not needed... do
        not include the results of a string verify read in the data
        file'). The read still HAPPENS as part of verify itself
        (_verify_equals) — this only controls whether it's ADDITIONALLY
        recorded here as if it were ordinary measurement data, which is
        what happens if the same command also happens to have its own
        active-query LED on for visibility during the run.

        A failed read gets exactly ONE retry after
        _ACTIVE_READ_RETRY_DELAY_S before the error string is
        recorded — the observed failures (WinError 10054, a transient
        connection reset from an instrument's single-client TCP
        server) recover well within one poll cycle, so a single retry
        turns a ~2.5% data-gap rate into a negligible one without
        masking anything persistent: a genuinely dead device still
        fails both attempts and still shows up as READ FAILED in the
        data, same format as before. Same philosophy as verify's and
        the while-loop's existing read-retry budgets, scaled down to
        what a logged read warrants (it isn't gating anything)."""
        exclude = exclude or set()
        preread = preread or {}
        results = []
        for device_name, command_name in self.query_specs:
            if (device_name, command_name) in exclude:
                continue
            alias = f'{device_name}_{command_name}'
            if (device_name, command_name) in preread:
                # Already read this cycle by the caller (the while
                # loop's exit read) — record that value instead of
                # querying the instrument a second time. No retry
                # wrapper: the value in hand IS the successful read.
                results.append((alias, preread[(device_name, command_name)]))
                continue
            try:
                value = _execute_query(self.stem, device_name, command_name)
            except Exception:
                time.sleep(_ACTIVE_READ_RETRY_DELAY_S)
                try:
                    value = _execute_query(self.stem, device_name, command_name)
                except Exception as e:
                    value = f'READ FAILED ({e})'
            results.append((alias, value))
        return results


class SimulationPreviewDialog(wx.Dialog):
    """Plain read-only preview of what Simulate computed — unlike
    QueryResponseDialog, nothing here gets selected/fed back into
    anything, just a scrollable view of what would be sent."""

    def __init__(self, parent, text: str):
        super().__init__(parent, title='Simulate — preview', size=wx.Size(520, 400))
        text_ctrl = wx.TextCtrl(self, value=text, style=wx.TE_MULTILINE | wx.TE_READONLY)
        btn_close = wx.Button(self, wx.ID_CANCEL, label='Close')
        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(text_ctrl, 1, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        main.Add(btn_close, 0, wx.ALL | wx.ALIGN_RIGHT, self.FromDIP(PAD_SMALL))
        self.SetSizer(main)


class QueryResponseDialog(wx.Dialog):
    """Shows the raw response from a live 'Query Once'. Select a
    substring with the mouse (native selection in a read-only TextCtrl),
    then 'Use Selection as Range' computes the 1-indexed start/end
    position and hands it back — implements the 'point at what you want'
    workflow instead of guessing positions blind, mapped onto v1's
    existing 'take range between i-th and j-th position' extraction mode
    rather than a new parallel mechanism."""

    def __init__(self, parent, response_text: str):
        super().__init__(parent, title='Query response', size=wx.Size(480, 280))
        self.result_range = None   # (start, end), 1-indexed inclusive, or None

        self.text_ctrl = wx.TextCtrl(self, value=response_text,
                                     style=wx.TE_MULTILINE | wx.TE_READONLY)
        hint = wx.StaticText(self, label='Select the part of the response you want, '
                             'then click "Use Selection as Range".')
        hint.SetForegroundColour(COLOR_TEXT_MUTED)
        hint.Wrap(self.FromDIP(440))

        btn_use = wx.Button(self, label='Use Selection as Range')
        btn_close = wx.Button(self, wx.ID_CANCEL, label='Close')
        btn_use.Bind(wx.EVT_BUTTON, self._on_use_selection)
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_row.Add(btn_use, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        btn_row.Add(btn_close, 0)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(hint, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        main.Add(self.text_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(PAD_SMALL))
        main.Add(btn_row, 0, wx.ALL | wx.ALIGN_RIGHT, self.FromDIP(PAD_SMALL))
        self.SetSizer(main)

    def _on_use_selection(self, event):
        start, end = self.text_ctrl.GetSelection()
        if start == end:
            wx.MessageBox('Select some text first.', 'Nothing selected', wx.OK | wx.ICON_WARNING)
            return
        self.result_range = (start + 1, end)   # 1-indexed, matches v1's convention
        self.EndModal(wx.ID_OK)


class CommandEditorDialog(wx.Dialog):
    """Add one command to a device's _commands.txt. Core file format
    stays v1-compatible (13 pipe-delimited fields, verified against
    v1's actual sub_commandmanager.py encoding) — but four NEW fields
    (13-16) are appended for write commands, beyond v1: method, a
    linked read, ramp rate, and an 'equals' value (see
    _get_command_method). requirement: 'you dont need to change the files;
    the files just contain... data and parameters' — appending is
    backward-compatible, anything reading only the first 13 fields is
    unaffected.

    Write commands with a '[%]' placeholder: number format (float
    exponential / float / integer) + decimal precision. Real v1 feature
    (self.number_choice / self.number_precision), field[5] encoded as
    '{prefix}={precision}' where prefix -1/-2/-3 = exp/float/int.

    Also for write commands with a placeholder: a Method dropdown —
    'none' (default, jump directly, unchanged from before any of this
    existed), 'ramp', or 'verify'. user feedback corrected an earlier two-
    independent-switches design to this single exclusive choice:
    'There is NO overlap between defining a rate and the verifying.'
    'ramp' shows a rate field (units/s, fields[15]) and a dropdown of
    the same device's existing read commands, labeled 'Based on output
    state of' (fields[14]) — 'current-value read' read as confusing
    with the physical unit ampere, by request, hence the rename. 'verify'
    shows the SAME dropdown, relabeled 'Proceed whenever', plus an
    'equals' text field (fields[16]) — polled via
    TaskRunnerThread._verify_equals as an exact string match, NOT a
    numeric tolerance (deliberately: this is for a discrete status
    readback like a magnet controller reporting 'HOLD', not 'close
    enough' to a float), with NO timeout (requirement: 'i'd rather have it
    hang than just proceed'). method='ramp' requires rate>0 and a
    linked read; method='verify' requires a linked read and a non-
    empty 'equals' value. All validated at save time here, and
    defensively re-checked at execution time (_get_command_method) in
    case a file predates this validation or gets hand-edited.

    Query commands: extraction mode (field[7], 0-6 in v1) — 'take whole
    string', 'split string' (field[8]=delimiter), 'take range between
    i-th and j-th position' (field[8]/[9]=start/end), 'remove trailing/
    preceding character' (field[8]=char), 'remove all non-numerics'.
    Mode 6, 'convert using Python script', is NOT included — arbitrary
    code execution at query time is a materially bigger, separate
    feature, not a UI addition.

    'Query Once' sends the actual query string via VISA (reading the
    device's saved .txt settings) and shows the raw response in
    QueryResponseDialog; selecting a substring there computes the
    1-indexed start/end and feeds back into the 'range' mode fields —
    point at what you want instead of guessing positions blind.

    Deliberately NOT ported from v1: compliance min/max, verify/wc
    mode, linked-command checks. Out of scope for 'add one command' —
    v1's file also allows all of that, but nothing here in v2
    currently uses it."""

    def __init__(self, parent, device_name: str, device_stem):
        super().__init__(parent, title=f'Add Command — {device_name}', size=wx.Size(460, 480))
        self.device_name = device_name
        self.device_stem = device_stem   # Path to the .dev file's stem folder, for Query Once
        self._original_name = None   # set by load_from_line when editing — excludes the
                                     # command's own current name from the duplicate check,
                                     # so re-saving under an unchanged name is never flagged
        self._original_field0 = None   # set by load_from_line when editing a QUERY command —
                                       # preserves the persisted LED tri-state (on/verify/off)
                                       # through an edit; get_command_line() only defaults to
                                       # 'True' (on) when this is None, i.e. a brand-new Add.
                                       # Without this, editing an existing query (fixing its
                                       # extraction mode, say) would silently reset a red/gray
                                       # LED back to green on save.

        self.name_ctrl = wx.TextCtrl(self)
        _bind_name_filter(self.name_ctrl)
        self.mode_write = wx.RadioButton(self, label='Write (send a command)', style=wx.RB_GROUP)
        self.mode_query = wx.RadioButton(self, label='Query (read a value back)')
        self.mode_write.SetValue(True)
        self.string_ctrl = wx.TextCtrl(self)
        hint = wx.StaticText(self, label="Use [%] where a numeric parameter goes, e.g. "
                             "':SOUR:VOLT [%]'. Leave it out for a fixed command "
                             "(e.g. ':OUTP ON') or a query (e.g. ':READ?').")
        hint.SetForegroundColour(COLOR_TEXT_MUTED)
        hint.Wrap(self.FromDIP(420))

        # --- Write-mode: number format (shown only for write + placeholder) ---
        self.number_type = wx.Choice(self, choices=['float exponential', 'float', '(signed) integer'])
        self.number_type.SetSelection(1)
        self.number_precision = wx.Choice(self, choices=[str(i) for i in range(13)])
        self.number_precision.SetSelection(6)
        self.numfmt_label1 = wx.StaticText(self, label='Number format:')
        self.numfmt_label2 = wx.StaticText(self, label='Decimal precision:')

        # --- Write-mode: Method (shown only for write + placeholder) ---
        # User feedback corrected the original design here: ramp and verify used
        # to be independent switches; now they're mutually exclusive
        # alternatives picked by ONE dropdown — 'There is NO overlap
        # between defining a rate and the verifying' (a magnet
        # controller with its own internal ramp: you either
        # software-step it yourself, or you jump straight to setpoint
        # and just wait for the controller to report it's done — never
        # both). 'none' is the original, unchanged default behavior.
        self.method_label = wx.StaticText(self, label='Method:')
        self.method_choice = wx.Choice(self, choices=['none', 'ramp', 'verify'])
        self.method_choice.SetSelection(0)
        self.method_choice.Bind(wx.EVT_CHOICE, self._on_method_change)

        self.ramp_rate_ctrl = wx.TextCtrl(self, value='0')
        self.ramp_rate_label = wx.StaticText(self, label='Ramp rate (units/s):')

        read_names = _get_read_commands_for_device(self.device_stem, self.device_name)
        self.linked_read_choice = wx.Choice(
            self, choices=read_names if read_names else ['(no read commands defined yet)'])
        self.linked_read_choice.SetSelection(0)
        self.linked_read_choice.Enable(bool(read_names))
        self.linked_read_choice.Bind(wx.EVT_CHOICE, self._on_linked_read_selected)
        # Which of this device's reads are split-mode (extraction mode
        # 1: one read -> several values)? Known statically from field 7.
        # A split read can serve ramp/verify only via ONE chosen
        # element ('readvalues[0]'), stored as a bracket suffix in
        # field 14 — same convention as the while dialog; see
        # _split_element_suffix.
        self._split_reads = set()
        for rn in read_names:
            try:
                rfields = _find_command_fields(self.device_stem, self.device_name, rn)
                if rfields is not None and len(rfields) > 7 and rfields[7].strip() == '1':
                    self._split_reads.add(rn)
            except Exception:
                pass   # unreadable: treat as non-split
        self.element_ctrl = wx.TextCtrl(self, value='0')
        self.element_label = wx.StaticText(self, label='Element (0 = first value):')
        # Label text swaps between the two meanings below, in
        # _on_method_change — 'current-value read' reads as confusing
        # with the physical unit ampere, by request, so 'ramp' mode calls
        # it 'based on output state of' instead. Placeholder text here
        # gets replaced immediately in __init__ below.
        self.linked_read_label = wx.StaticText(self, label='Based on output state of:')

        self.equals_ctrl = wx.TextCtrl(self, value='')
        self.equals_label = wx.StaticText(self, label='...equals:')
        # Verify match timeout, seconds (fields[17]). '600' default =
        # the historical hard cap, so a freshly opened old command
        # shows the number it was already effectively running with.
        # 0 = no timeout (poll until match or abort) — for controller-
        # paced moves that legitimately take hours (14T -> 0T at
        # 0.1T/min is 140 minutes of CORRECT behavior).
        self.verify_timeout_ctrl = wx.TextCtrl(self, value='600')
        self.verify_timeout_label = wx.StaticText(self, label='Verify timeout (s, 0 = none):')
        # Verify tolerance, fields[18] (requirement: 'we add a variance field
        # e.g 0.01. So equal to 1.0 means... within 0.99 and 1.01 you
        # are within "equal"'). '0' default = exact string match, the
        # behavior every existing command already has. The equals field
        # itself may contain [%], substituted per row/step with the
        # value being written — together these let a sweep verify
        # 'fieldvalue == [%] ± 0.001' against its own moving setpoint.
        self.verify_variance_ctrl = wx.TextCtrl(self, value='0')
        self.verify_variance_label = wx.StaticText(self, label='Tolerance (±, 0 = exact):')

        # --- Query-mode: extraction options ---
        self.qresult_choice = wx.Choice(self, choices=[
            'take whole string as is', 'split string',
            'take range between i-th and j-th position',
            'remove trailing character', 'remove preceding characters',
            'remove all non-numerics', 'JSON key (dotted path)'])
        self.qresult_choice.SetSelection(0)
        self.qresult_label = wx.StaticText(self, label='Extract:')
        self.split_char_ctrl = wx.TextCtrl(self, value=',')
        self.range_start_ctrl = wx.TextCtrl(self, value='1')
        self.range_end_ctrl = wx.TextCtrl(self, value='end')
        self.rem_char_ctrl = wx.TextCtrl(self, value='')
        # JSON key path (extraction mode 6, HTTP/JSON devices):
        # 'result' is the JSON-RPC convention; dotted for nesting,
        # integer segments index arrays ('0.result' = first entry of a
        # batch response).
        self.json_path_ctrl = wx.TextCtrl(self, value='result')
        self.split_label = wx.StaticText(self, label='Split character:')
        self.range_label = wx.StaticText(self, label='Start / end position:')
        self.rem_label = wx.StaticText(self, label='Character(s) to remove:')
        self.json_label = wx.StaticText(self, label='JSON key path:')
        btn_query_once = wx.Button(self, label='Query Once...')
        btn_query_once.Bind(wx.EVT_BUTTON, self._on_query_once)
        self.query_once_btn = btn_query_once
        btn_test_once = wx.Button(self, label='Test Once...')
        btn_test_once.Bind(wx.EVT_BUTTON, self._on_test_once)
        self.test_once_btn = btn_test_once

        once_row = wx.BoxSizer(wx.HORIZONTAL)
        once_row.Add(btn_test_once, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        once_row.Add(btn_query_once, 0)

        range_row = wx.BoxSizer(wx.HORIZONTAL)
        range_row.Add(self.range_start_ctrl, 1, wx.RIGHT, self.FromDIP(PAD_SMALL))
        range_row.Add(self.range_end_ctrl, 1)

        form = wx.FlexGridSizer(0, 2, PAD_SMALL, PAD_SMALL)
        form.AddGrowableCol(1)
        form.Add(wx.StaticText(self, label='Command name:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.name_ctrl, 1, wx.EXPAND)
        form.Add(wx.StaticText(self, label='Command string:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.string_ctrl, 1, wx.EXPAND)
        form.Add(self.numfmt_label1, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.number_type, 1, wx.EXPAND)
        form.Add(self.numfmt_label2, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.number_precision, 1, wx.EXPAND)
        form.Add(self.method_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.method_choice, 1, wx.EXPAND)
        form.Add(self.ramp_rate_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.ramp_rate_ctrl, 1, wx.EXPAND)
        form.Add(self.linked_read_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.linked_read_choice, 1, wx.EXPAND)
        form.Add(self.element_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.element_ctrl, 1, wx.EXPAND)
        form.Add(self.equals_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.equals_ctrl, 1, wx.EXPAND)
        form.Add(self.verify_timeout_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.verify_timeout_ctrl, 1, wx.EXPAND)
        form.Add(self.verify_variance_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.verify_variance_ctrl, 1, wx.EXPAND)
        form.Add(self.qresult_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.qresult_choice, 1, wx.EXPAND)
        form.Add(self.split_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.split_char_ctrl, 1, wx.EXPAND)
        form.Add(self.range_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(range_row, 1, wx.EXPAND)
        form.Add(self.rem_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.rem_char_ctrl, 1, wx.EXPAND)
        form.Add(self.json_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.json_path_ctrl, 1, wx.EXPAND)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(form, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_LARGE))
        main.Add(self.mode_write, 0, wx.LEFT | wx.RIGHT, self.FromDIP(PAD_LARGE))
        main.Add(self.mode_query, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(PAD_LARGE))
        main.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(PAD_LARGE))
        main.Add(once_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(PAD_LARGE))
        main.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        self.SetSizerAndFit(main)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        self.mode_write.Bind(wx.EVT_RADIOBUTTON, self._on_mode_change)
        self.mode_query.Bind(wx.EVT_RADIOBUTTON, self._on_mode_change)
        self.string_ctrl.Bind(wx.EVT_TEXT, self._on_mode_change)
        self.qresult_choice.Bind(wx.EVT_CHOICE, self._on_mode_change)
        self._on_mode_change(None)

    # -----------------------------------------------------------------
    def _on_mode_change(self, event):
        is_write = self.mode_write.GetValue()
        has_placeholder = '[%]' in self.string_ctrl.GetValue()
        show_numfmt = is_write and has_placeholder
        for w in (self.numfmt_label1, self.number_type, self.numfmt_label2, self.number_precision,
                 self.method_label, self.method_choice):
            w.Show(show_numfmt)
        self._on_method_change(None)   # sync ramp/verify sub-fields to the current Method

        show_query = not is_write
        for w in (self.qresult_label, self.qresult_choice, self.query_once_btn):
            w.Show(show_query)
        self.test_once_btn.Show(is_write)

        sel = self.qresult_choice.GetSelection()
        show_split = show_query and sel == 1
        show_range = show_query and sel == 2
        show_rem   = show_query and sel in (3, 4)
        show_json  = show_query and sel == 6
        for w in (self.split_label, self.split_char_ctrl):
            w.Show(show_split)
        for w in (self.range_label, self.range_start_ctrl, self.range_end_ctrl):
            w.Show(show_range)
        for w in (self.rem_label, self.rem_char_ctrl):
            w.Show(show_rem)
        for w in (self.json_label, self.json_path_ctrl):
            w.Show(show_json)
        self.Layout()

    def _on_method_change(self, event):
        """Shows exactly the sub-fields relevant to the selected
        Method — requirement: ramp and verify are mutually exclusive
        alternatives now, not independent switches ('There is NO
        overlap between defining a rate and the verifying'), so at
        most one group of sub-fields is ever visible. Also swaps the
        shared linked-read dropdown's label between its two meanings
        (same underlying field[14] either way): 'Based on output state
        of' for ramp, 'Proceed whenever' for verify — renamed from
        'current-value read' specifically because that read as
        confusing with the physical unit ampere (requested)."""
        numfmt_visible = self.method_choice.IsShown()   # False if this isn't a write+placeholder command at all
        method = self.method_choice.GetStringSelection() if numfmt_visible else 'none'

        show_ramp = numfmt_visible and method == 'ramp'
        show_verify = numfmt_visible and method == 'verify'
        show_read = show_ramp or show_verify

        self.ramp_rate_label.Show(show_ramp)
        self.ramp_rate_ctrl.Show(show_ramp)
        self.linked_read_label.Show(show_read)
        self.linked_read_choice.Show(show_read)
        show_element = (show_read
                        and self.linked_read_choice.GetSelection() != wx.NOT_FOUND
                        and self.linked_read_choice.GetStringSelection() in self._split_reads)
        self.element_label.Show(show_element)
        self.element_ctrl.Show(show_element)
        self.element_ctrl.Enable(show_element)
        self.equals_label.Show(show_verify)
        self.equals_ctrl.Show(show_verify)
        self.verify_timeout_label.Show(show_verify)
        self.verify_timeout_ctrl.Show(show_verify)
        self.verify_variance_label.Show(show_verify)
        self.verify_variance_ctrl.Show(show_verify)

        if show_ramp:
            self.linked_read_label.SetLabel('Based on output state of:')
        elif show_verify:
            self.linked_read_label.SetLabel('Proceed whenever:')
        self.Layout()

    def _on_linked_read_selected(self, event):
        self._on_method_change(None)   # element visibility depends on the selection
        event.Skip()

    def _on_query_once(self, event):
        query_str = self.string_ctrl.GetValue().strip()
        if not query_str:
            wx.MessageBox('Enter the command string first.', 'Nothing to query', wx.OK | wx.ICON_WARNING)
            return
        try:
            addr, wterm, rterm, timeout, baud, bits = _read_device_settings(
                self.device_stem, self.device_name)
        except Exception as e:
            wx.MessageBox(f"Could not read '{self.device_name}' settings:\n{e}\n\n"
                          "Has this device been saved with an assigned VISA address yet?",
                          'Query Once failed', wx.OK | wx.ICON_ERROR)
            return
        try:
            response = _visa_query(addr, query_str, wterm, rterm, timeout, baud, bits)
        except Exception as e:
            wx.MessageBox(f'Query failed:\n{e}', 'Query Once failed', wx.OK | wx.ICON_ERROR)
            return

        with QueryResponseDialog(self, response) as dlg:
            if dlg.ShowModal() == wx.ID_OK and dlg.result_range is not None:
                start, end = dlg.result_range
                self.qresult_choice.SetSelection(2)   # 'take range between i-th and j-th position'
                self.range_start_ctrl.SetValue(str(start))
                self.range_end_ctrl.SetValue(str(end))
                self._on_mode_change(None)

    def _on_test_once(self, event):
        cmdstr = self.string_ctrl.GetValue().strip()
        if not cmdstr:
            wx.MessageBox('Enter the command string first.', 'Nothing to send', wx.OK | wx.ICON_WARNING)
            return

        final_str = cmdstr
        if '[%]' in cmdstr:
            with wx.TextEntryDialog(self, 'Value to substitute for [%]:', 'Test Once') as dlg:
                if dlg.ShowModal() != wx.ID_OK:
                    return
                raw_value = dlg.GetValue().strip()
            try:
                value = float(raw_value)
            except ValueError:
                wx.MessageBox('Enter a valid number.', 'Invalid value', wx.OK | wx.ICON_WARNING)
                return
            # Format using whatever number type/precision is currently
            # selected, so the test reflects what execution would
            # actually send, not a raw Python str(value).
            formatted = _format_number(value, self.number_type.GetSelection(),
                                       self.number_precision.GetSelection())
            final_str = cmdstr.replace('[%]', formatted)

        try:
            addr, wterm, rterm, timeout, baud, bits = _read_device_settings(
                self.device_stem, self.device_name)
        except Exception as e:
            wx.MessageBox(f"Could not read '{self.device_name}' settings:\n{e}\n\n"
                          "Has this device been saved with an assigned VISA address yet?",
                          'Test Once failed', wx.OK | wx.ICON_ERROR)
            return
        try:
            sent_writes = []
            for kind, value in _split_chained_command(final_str):
                if kind == 'pause':
                    time.sleep(value)
                else:
                    _visa_write(addr, value, wterm, rterm, timeout, baud, bits)
                    sent_writes.append(value)
        except Exception as e:
            wx.MessageBox(f'Write failed:\n{e}', 'Test Once failed', wx.OK | wx.ICON_ERROR)
            return

        wx.MessageBox('Sent:\n' + '\n'.join(sent_writes), 'Test Once', wx.OK | wx.ICON_INFORMATION)

    def _on_ok(self, event):
        name = self.name_ctrl.GetValue().strip()
        errors = []
        if not name:
            errors.append('Command name cannot be empty.')
        elif not _NAME_RE.match(name):
            errors.append('Command name may only contain lowercase letters (a-z) and digits (0-9).')
        else:
            # requirement: 'a check when a new command is generated if it
            # already exists. So you wont be able to close the dialog to
            # save the new name, if it already exists' — checked here,
            # before the dialog can ever close with OK, rather than
            # after (the previous behavior for Edit: the dialog would
            # already be gone by the time a collision was caught,
            # forcing a full reopen to fix the name; Add had no check at
            # all — a second command with the same name would silently
            # become permanently unreachable, since _find_command_fields
            # returns only the first matching line). Excludes the
            # command's own original name (_original_name, set by
            # load_from_line when editing) so saving an existing command
            # back under its unchanged name is never flagged.
            existing = _get_all_command_names_for_device(self.device_stem, self.device_name)
            if self._original_name is not None and self._original_name in existing:
                existing = [n for n in existing if n != self._original_name]
            if name in existing:
                errors.append(f"'{name}' already exists for this device — choose a different name.")
        if not self.string_ctrl.GetValue().strip():
            errors.append('Command string cannot be empty.')

        if self.mode_write.GetValue() and '[%]' in self.string_ctrl.GetValue():
            method = self.method_choice.GetStringSelection()
            if method == 'ramp':
                rate_str = self.ramp_rate_ctrl.GetValue().strip()
                try:
                    rate_val = float(rate_str) if rate_str else 0.0
                except ValueError:
                    errors.append('Ramp rate must be a number.')
                    rate_val = None
                if rate_val is not None and rate_val <= 0:
                    errors.append("Method is 'ramp' but the rate is 0 or blank — "
                                  "either set a positive rate or change Method to 'none'.")
                has_selection = (self.linked_read_choice.IsEnabled()
                                 and self.linked_read_choice.GetSelection() != wx.NOT_FOUND)
                if not has_selection:
                    errors.append("Method 'ramp' requires a read to base the ramp on — "
                                  "define a read command for this device first.")
            elif method == 'verify':
                has_selection = (self.linked_read_choice.IsEnabled()
                                 and self.linked_read_choice.GetSelection() != wx.NOT_FOUND)
                if not has_selection:
                    errors.append("Method 'verify' requires a read to proceed on — "
                                  "define a read command for this device first.")
                if not self.equals_ctrl.GetValue().strip():
                    errors.append("Method 'verify' requires an 'equals' value — "
                                  "what should the read report when it's done?")
            if method in ('ramp', 'verify'):
                # Split-mode linked read: an explicit element is
                # required (0 prefixed as the default) — same rule and
                # wording as the while dialog.
                sel_ok = (self.linked_read_choice.IsEnabled()
                          and self.linked_read_choice.GetSelection() != wx.NOT_FOUND)
                if sel_ok and self.linked_read_choice.GetStringSelection() in self._split_reads:
                    elem_raw = self.element_ctrl.GetValue().strip()
                    if not elem_raw:
                        errors.append('Element: required for a multi-value (split) '
                                      'read — 0 = first value.')
                    elif not elem_raw.isdigit():
                        errors.append(f"Element: must be a non-negative integer — "
                                      f"{elem_raw!r} is not one.")

        if errors:
            wx.MessageBox('\n'.join(errors), 'Cannot continue', wx.OK | wx.ICON_WARNING)
            return
        event.Skip()

    def load_from_line(self, line: str):
        """Populate all fields from an existing command line — for
        editing, not just adding. Reverse of get_command_line()."""
        fields = line.split('|')
        name, mode, cmdstr = fields[1], fields[2], fields[4]
        self._original_name = name   # see __init__ — excluded from the duplicate-name check in _on_ok
        self._original_field0 = fields[0] if fields else None   # see __init__ — only consulted
                                                                 # for query mode in get_command_line()
        self.name_ctrl.SetValue(name)
        self.string_ctrl.SetValue(cmdstr)
        if mode == 'wf':
            self.mode_write.SetValue(True)
            self.mode_query.SetValue(False)
            field5 = fields[5] if len(fields) > 5 else 'None'
            if field5 != 'None' and '=' in field5:
                prefix, _, precision = field5.partition('=')
                prefix_map = {'-1': 0, '-2': 1, '-3': 2}
                self.number_type.SetSelection(prefix_map.get(prefix, 1))
                try:
                    self.number_precision.SetSelection(int(precision))
                except Exception:
                    pass
            method = fields[13] if len(fields) > 13 and fields[13] in ('ramp', 'verify') else 'none'
            self.method_choice.SetStringSelection(method)
            if len(fields) > 14 and fields[14] and fields[14] != 'none':
                # fields[14] may carry an element suffix ('readvalues[0]')
                # for a split-mode read — select the BARE name and put
                # the element into its own field.
                bare_read, elem = _split_element_suffix(fields[14])
                idx = self.linked_read_choice.FindString(bare_read)
                if idx != wx.NOT_FOUND:
                    self.linked_read_choice.SetSelection(idx)
                if elem is not None:
                    self.element_ctrl.SetValue(str(elem))
            if len(fields) > 15:
                self.ramp_rate_ctrl.SetValue(fields[15])
            if len(fields) > 16:
                self.equals_ctrl.SetValue(fields[16])
            if len(fields) > 17 and fields[17].strip():
                self.verify_timeout_ctrl.SetValue(fields[17].strip())
            else:
                # Old line without field 17 — show the 600s default the
                # command is already effectively running with, so what
                # the dialog displays and what execution does agree.
                self.verify_timeout_ctrl.SetValue('600')
            if len(fields) > 18 and fields[18].strip():
                self.verify_variance_ctrl.SetValue(fields[18].strip())
            else:
                # Old line without field 18 — 0 = exact match, which is
                # what the command already does.
                self.verify_variance_ctrl.SetValue('0')
        else:
            self.mode_query.SetValue(True)
            self.mode_write.SetValue(False)
            try:
                sel = int(fields[7]) if len(fields) > 7 else 0
            except ValueError:
                sel = 0
            self.qresult_choice.SetSelection(sel)
            if sel == 1 and len(fields) > 8:
                self.split_char_ctrl.SetValue(fields[8])
            elif sel == 2 and len(fields) > 9:
                self.range_start_ctrl.SetValue(fields[8])
                self.range_end_ctrl.SetValue(fields[9])
            elif sel in (3, 4) and len(fields) > 8:
                self.rem_char_ctrl.SetValue(fields[8])
            elif sel == 6 and len(fields) > 8:
                self.json_path_ctrl.SetValue(fields[8])
        self._on_mode_change(None)

    def _linked_read_with_element(self) -> str:
        """The selected linked read's stored name for field 14: bare for
        a single-value read, 'name[N]' for a split-mode read (element
        from the Element field, validated in _on_ok). Same suffix
        convention as the while dialog — see _split_element_suffix."""
        name = self.linked_read_choice.GetStringSelection()
        if name in self._split_reads:
            return f'{name}[{self.element_ctrl.GetValue().strip()}]'
        return name

    def get_command_line(self) -> str:
        name = self.name_ctrl.GetValue().strip()
        cmdstr = self.string_ctrl.GetValue().strip()
        is_write = self.mode_write.GetValue()
        mode = 'wf' if is_write else 'q'
        n_placeholders = cmdstr.count('[%]')
        nargs = n_placeholders if n_placeholders > 0 else (1 if mode == 'q' else 0)

        if is_write:
            if n_placeholders > 0:
                prefix = {0: '-1', 1: '-2', 2: '-3'}[self.number_type.GetSelection()]
                precision = self.number_precision.GetSelection()
                field5 = f'{prefix}={precision}'
            else:
                field5 = 'None'

            method = self.method_choice.GetStringSelection() if n_placeholders > 0 else 'none'

            rate_val = 0.0
            equals_str = ''
            linked_read = 'none'
            verify_timeout = '600'
            verify_variance = '0'
            if method == 'ramp':
                rate_str = self.ramp_rate_ctrl.GetValue().strip() or '0'
                try:
                    rate_val = float(rate_str)
                except ValueError:
                    rate_val = 0.0
                if rate_val < 0:
                    rate_val = 0.0
                if self.linked_read_choice.IsEnabled() and self.linked_read_choice.GetSelection() != wx.NOT_FOUND:
                    linked_read = self._linked_read_with_element()
            elif method == 'verify':
                equals_str = self.equals_ctrl.GetValue().strip()
                if self.linked_read_choice.IsEnabled() and self.linked_read_choice.GetSelection() != wx.NOT_FOUND:
                    linked_read = self._linked_read_with_element()
                # Field 17, appended after the established 0-16 layout —
                # every reader is bounds-guarded, so old qmeas versions
                # ignore it and old FILES (no field 17) get the 600s
                # default in _get_verify_timeout_s: no manual migration
                # of _commands.txt on any machine, ever — a line gains
                # the field only when ITS command is next saved here.
                raw_timeout = self.verify_timeout_ctrl.GetValue().strip() or '600'
                try:
                    t = float(raw_timeout)
                    verify_timeout = f'{t:g}' if t >= 0 else '600'
                except ValueError:
                    verify_timeout = '600'
                # Field 18, tolerance — same appended-field convention
                # as 17: old files lack it -> 0 (exact match) at
                # runtime, old qmeas versions ignore it, a line gains
                # it only when saved here. Sanitized the same way:
                # empty/garbage/negative -> '0'.
                raw_variance = self.verify_variance_ctrl.GetValue().strip() or '0'
                try:
                    v = float(raw_variance)
                    verify_variance = f'{v:g}' if v > 0 else '0'
                except ValueError:
                    verify_variance = '0'

            fields = ['False', name, mode, str(nargs), cmdstr,
                     field5, '0', '0', '0=0=0', 'free', 'free', 'free', 'none=none',
                     method, linked_read, f'{rate_val:g}', equals_str, verify_timeout,
                     verify_variance]
        else:
            sel = self.qresult_choice.GetSelection()
            field8, field9 = 'free', 'free'
            if sel == 1:
                field8 = self.split_char_ctrl.GetValue() or ','
            elif sel == 2:
                field8 = self.range_start_ctrl.GetValue() or '1'
                field9 = self.range_end_ctrl.GetValue() or 'end'
            elif sel in (3, 4):
                field8 = self.rem_char_ctrl.GetValue()
            elif sel == 6:
                field8 = self.json_path_ctrl.GetValue().strip() or 'result'
            # field[3]=save-numeric, field[5]=convert-to-number, field[6]=clear —
            # all default '1' (checked), matching this lab's real 'read' example;
            # not exposed as separate controls, no evidence yet they need to vary.
            # field[0]: preserve the persisted LED tri-state through an
            # edit (self._original_field0, set by load_from_line); a
            # brand-new Add has no original, so it defaults to 'True'
            # (on/green) — unambiguous from creation, now that field[0]
            # carries real meaning (_query_led_state_from_field0).
            field0 = self._original_field0 if self._original_field0 is not None else 'True'
            fields = [field0, name, mode, '1', cmdstr,
                     '1', '1', str(sel), field8, field9, 'free', 'free', 'none=none']
        return '|'.join(fields)


class WhileEditorDialog(wx.Dialog):
    """Define (or edit) a virtual while-command on the control device —
    'repeat until verify condition is met' (requested). Deliberately its OWN
    dialog, not an extension of CommandEditorDialog: the fields are
    entirely different (no write/query toggle, no [%], no extraction
    settings — a while is a poll-and-compare loop, not device I/O).

    Fields, per the user's own description: 'exit loop when ____verify
    command selection (all devices)____ ____pulldown <,>,=,!=____
    ____Value that you enter____, and a timeout field.'

    The exit-condition dropdown lists every query-type command across
    EVERY device in the loaded list (_get_all_query_commands) — unlike
    ramp/verify's same-device-scoped dropdowns — shown as
    '{device}_{command}' to disambiguate identically-named queries on
    different devices. Definition-time list, not filtered by device
    on/off state (that's _run_while_loop's execution-time check).

    Save-time validation (requirement: 'You shouldnt even let the user go that
    far'): name charset (_NAME_RE) + not one of the 4 reserved control
    names + no duplicate on control (own name excluded when editing,
    same pattern as CommandEditorDialog._on_ok); a query must be
    selected; threshold non-empty AND numeric when the operator is
    </> ('if the user defines > or < with a text condition such as
    HOLD, prevent them from saving'); timeout MANDATORY — an explicit
    '0' means infinite, blank is refused (no silent default), duration
    suffixes allowed same as Integration Time ('5m', '1h').

    The poll interval is NOT defined here — it's the task row's own
    Integration Time (per-use, like a counter's), not per-definition."""

    def __init__(self, parent, device_stem):
        super().__init__(parent, title=f'Add Virtual While — {STANDARD_DEVICE_NAME}',
                         size=wx.Size(460, 360))
        self.device_stem = device_stem
        self._original_name = None   # set by load_from_line when editing — excluded
                                     # from the duplicate check, same as CommandEditorDialog
        self._query_specs = _get_all_query_commands(device_stem)
        # Which of those queries are split-mode (extraction mode 1 —
        # one read, several values)? Known statically from field 7 of
        # each command line, no execution needed. Drives the Element
        # field's visibility: a multi-value read can only be watched
        # via ONE chosen element ('readvalues[0]'), stored as a bracket
        # suffix in the definition's linked-read field — see
        # _split_element_suffix.
        self._split_pairs = set()
        for d, c in self._query_specs:
            try:
                cfields = _find_command_fields(device_stem, d, c)
                if cfields is not None and len(cfields) > 7 and cfields[7].strip() == '1':
                    self._split_pairs.add((d, c))
            except Exception:
                pass   # unreadable command file: treat as non-split

        self.name_ctrl = wx.TextCtrl(self)
        _bind_name_filter(self.name_ctrl)
        self.query_choice = wx.Choice(
            self, choices=[f'{d}_{c}' for d, c in self._query_specs])
        self.query_choice.Bind(wx.EVT_CHOICE, self._on_query_selected)
        self.operator_choice = wx.Choice(self, choices=list(_WHILE_OPERATORS))
        self.operator_choice.SetSelection(2)   # '=' — the HOLD-style status
                                               # check this exists for
        self.value_ctrl = wx.TextCtrl(self)
        self.element_ctrl = wx.TextCtrl(self, value='0')   # element index into a
                                                           # split read; '0' default
                                                           # by request
        self.element_label = wx.StaticText(self, label='Element (0 = first value):')
        self.timeout_ctrl = wx.TextCtrl(self)
        hint = wx.StaticText(self, label=(
            "Polls the selected query once per the task row's Integration Time "
            "(minimum 0.1s) and exits when the condition is true. Timeout: 0 = "
            "no timeout; a timeout that elapses first ABORTS the whole run "
            "(onhold included), same as a verify failure. '<' and '>' need a "
            "numeric value; '=' and '!=' compare the exact text."))
        hint.SetForegroundColour(COLOR_TEXT_MUTED)
        hint.Wrap(self.FromDIP(420))

        grid = wx.FlexGridSizer(cols=2, vgap=self.FromDIP(PAD_SMALL),
                                hgap=self.FromDIP(PAD_SMALL))
        grid.AddGrowableCol(1)
        grid.Add(wx.StaticText(self, label='Command name:'), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.name_ctrl, 0, wx.EXPAND)
        grid.Add(wx.StaticText(self, label='Exit loop when:'), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.query_choice, 0, wx.EXPAND)
        grid.Add(self.element_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.element_ctrl, 0, wx.EXPAND)
        grid.Add(wx.StaticText(self, label='Operator:'), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.operator_choice, 0)
        grid.Add(wx.StaticText(self, label='Value:'), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.value_ctrl, 0, wx.EXPAND)
        grid.Add(wx.StaticText(self, label='Timeout (s, 0 = none):'), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.timeout_ctrl, 0, wx.EXPAND)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(grid, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_LARGE))
        main.Add(hint, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(PAD_LARGE))
        main.AddStretchSpacer(1)
        main.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0,
                 wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        self.SetSizer(main)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self._sync_element_visibility()

    def _selected_pair(self):
        sel = self.query_choice.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self._query_specs):
            return None
        return self._query_specs[sel]

    def _sync_element_visibility(self):
        """Element field shown/enabled ONLY when the selected query is
        split-mode — a single-value read has no elements to pick, and
        showing the field anyway would invite writing a suffix the
        runtime would then loudly reject."""
        pair = self._selected_pair()
        is_split = pair in self._split_pairs if pair is not None else False
        self.element_label.Show(is_split)
        self.element_ctrl.Show(is_split)
        self.element_ctrl.Enable(is_split)
        self.Layout()

    def _on_query_selected(self, event):
        self._sync_element_visibility()
        event.Skip()

    def load_from_line(self, line: str):
        """Pre-populate for editing an existing while-command's line —
        same role as CommandEditorDialog.load_from_line. Only called on
        lines already recognized as while-commands (_get_while_definition
        non-None in the tree's menu routing), so fields 19-23 exist.
        A linked read stored with an element suffix ('readvalues[0]')
        is split back into the bare name (for the choice) and the
        element field."""
        fields = line.split('|')
        name = fields[1]
        self.name_ctrl.SetValue(name)
        self._original_name = name
        d = _parse_while_definition(fields)
        if d is None:
            return   # defensive — routing should make this unreachable
        bare_read, elem = _split_element_suffix(d['linked_read'])
        pair = (d['linked_device'], bare_read)
        if pair in self._query_specs:
            self.query_choice.SetSelection(self._query_specs.index(pair))
        if elem is not None:
            self.element_ctrl.SetValue(str(elem))
        if d['operator'] in _WHILE_OPERATORS:
            self.operator_choice.SetSelection(_WHILE_OPERATORS.index(d['operator']))
        self.value_ctrl.SetValue(d['threshold'])
        self.timeout_ctrl.SetValue(d['timeout_raw'])
        self._sync_element_visibility()

    def _on_ok(self, event):
        errors = []
        name = self.name_ctrl.GetValue().strip()
        if not name or not _NAME_RE.match(name):
            errors.append('Command name: lowercase letters and digits only.')
        elif name in _RESERVED_CONTROL_COMMAND_NAMES:
            errors.append(f"'{name}' is a reserved control command name.")
        else:
            existing = _get_all_command_names_for_device(
                self.device_stem, STANDARD_DEVICE_NAME)
            if self._original_name is not None and self._original_name in existing:
                existing = [n for n in existing if n != self._original_name]
            if name in existing:
                errors.append(f"'{name}' already exists on "
                              f"{STANDARD_DEVICE_NAME} — choose a different name.")
        if self.query_choice.GetSelection() == wx.NOT_FOUND:
            errors.append('Select the query to watch (exit loop when).')
        else:
            pair = self._selected_pair()
            if pair in self._split_pairs:
                # Multi-value read: an explicit element is required —
                # 'the user needs to understand what they are doing'
                # (requested) — with 0 prefilled as the default.
                elem_raw = self.element_ctrl.GetValue().strip()
                if not elem_raw:
                    errors.append('Element: required for a multi-value (split) '
                                  'read — 0 = first value.')
                elif not elem_raw.isdigit():
                    errors.append(f"Element: must be a non-negative integer — "
                                  f"{elem_raw!r} is not one.")
        operator = (self.operator_choice.GetStringSelection()
                    if self.operator_choice.GetSelection() != wx.NOT_FOUND else '')
        threshold = self.value_ctrl.GetValue()
        if not threshold.strip():
            errors.append('Value: required.')
        elif operator in ('<', '>'):
            # requirement: 'If the user defines > or < with a text condition
            # such as HOLD, then prevent them from saving the command.'
            try:
                float(threshold.strip())
            except ValueError:
                errors.append(f"Value: '<' and '>' need a number — "
                              f"{threshold.strip()!r} is not one.")
        timeout_raw = self.timeout_ctrl.GetValue().strip()
        if not timeout_raw:
            # Mandatory, no silent default — infinite must be an
            # explicit, conscious '0', consistent with blocking every
            # other ambiguous definition at save time.
            errors.append('Timeout: required — enter 0 for no timeout.')
        else:
            try:
                if _parse_duration_seconds(timeout_raw) < 0:
                    errors.append('Timeout: cannot be negative.')
            except ValueError:
                errors.append(f"Timeout: {timeout_raw!r} is not a valid duration "
                              f"(plain seconds, or a suffix like '5m' / '1h').")
        if errors:
            wx.MessageBox('\n'.join(errors), 'Cannot save', wx.OK | wx.ICON_WARNING)
            return
        event.Skip()   # close with wx.ID_OK

    def get_command_line(self) -> str:
        """Full 0-23 line for control_commands.txt. Fields 0-18 use the
        same fixed placeholders as the existing userprompt/stop lines
        (cmdstr '0' — never sent, there's no VISA I/O here) plus neutral
        method/verify fields; 19-23 carry the while definition — see the
        field map at _parse_while_definition."""
        name = self.name_ctrl.GetValue().strip()
        device, query = self._query_specs[self.query_choice.GetSelection()]
        if (device, query) in self._split_pairs:
            # Multi-value read: store WHICH value as a bracket suffix —
            # 'readvalues[0]' — parsed back by _split_element_suffix at
            # execution (and by load_from_line on edit). Validated as a
            # plain non-negative integer in _on_ok.
            query = f'{query}[{self.element_ctrl.GetValue().strip()}]'
        operator = self.operator_choice.GetStringSelection()
        threshold = self.value_ctrl.GetValue().strip()
        timeout_raw = self.timeout_ctrl.GetValue().strip()
        fields = ['False', name, 'wf', '0', '0',
                  '-1=0', '0', 'free', 'free', 'free', 'free', 'free', 'free',
                  'none', 'none', '0', '', '600', '0',
                  device, query, operator, threshold, timeout_raw]
        return '|'.join(fields)


# =========================================================================
# Devices & Commands pane
# =========================================================================

class DevicesPanel(wx.Panel):
    """Device tree: devices as top-level nodes (each with a clickable
    on/off LED), their command aliases as children, loaded from real
    .dev + {alias}_commands.txt files (dev_dir(), same files
    DeviceEditorPanel writes and v1 reads). Double-click (or Enter) on
    a write-type command appends it to the Tasks grid via the
    on_add_command callback.

    Native drag-and-drop from this tree was tried and repeatedly crashed
    wx on Windows (mouse-capture/OLE reentrancy from
    EVT_TREE_BEGIN_DRAG, then a wxDragImage assertion). Dropped
    entirely — double-click is reliable and costs one extra click.

    LEDs are functional, not cosmetic: a device's on/off LED gates
    whether any of its queries run and whether its onhold fires on
    abort; a QUERY command's LED is tri-state — green (read + saved),
    red (readable by verify/while, NOT saved), grey (off) — cycled by
    click, persisted in field[0] of the command line on app close
    (save_query_led_states), and consumed by get_active_query_specs.
    Merely loading the tree still opens no VISA connection.

    A device whose {alias}_commands.txt fails to read/parse is skipped
    (shown with no children, one message box naming it) rather than
    aborting the whole list load — bounded to that one device, not
    per-line recovery inside the file, which was explicitly rejected
    earlier as unnecessary guarding against files you control yourself."""

    def __init__(self, parent, on_add_command=None, on_device_toggled=None):
        super().__init__(parent)
        self.SetBackgroundColour(COLOR_PANEL_BG)
        self.on_add_command = on_add_command
        self.on_device_toggled = on_device_toggled
        # Wired by QMeasMain._build_panes: on_device_selected populates
        # the Device Editor with an existing device's current settings
        # (single click or double click both call it); on_device_editor_
        # reveal additionally brings that pane into view (double click
        # only — see _on_item_activated/_on_tree_sel_changed).
        self.on_device_selected = None
        self.on_device_editor_reveal = None
        self.on_new_list_created = None   # QMeasMain: close the Device
                                          # Editor so it can't keep
                                          # showing a device from the
                                          # replaced list
        self._loading_tree = False   # guards event handlers against the
                                     # mid-rebuild EVT_TREE_* storm that
                                     # DeleteAllItems fires (a selected
                                     # item's deletion re-enters handlers
                                     # against a half-dead tree — the
                                     # phantom-leftover-rows bug)
        # Wired by QMeasMain._build_panes to TasksPanel.run_active. The
        # running thread re-reads command/device files from disk on
        # every row, so tree edits (and LED toggles, whose effect was
        # snapshotted at Start anyway) are frozen during a run.
        self.is_run_active = None
        self.current_devfile = None

        self.tree = wx.TreeCtrl(self, style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT)

        self._img_list = wx.ImageList(LED_DIAMETER, LED_DIAMETER)
        self._idx_led_off    = self._img_list.Add(_make_led_bitmap(COLOR_LED_OFF))
        self._idx_led_on     = self._img_list.Add(_make_led_bitmap(COLOR_STATUS_IDLE))
        self._idx_led_verify = self._img_list.Add(_make_led_bitmap(COLOR_STATUS_ERROR))
        self._idx_arrow_out = self._img_list.Add(_make_arrow_bitmap('out'))  # write
        self.tree.AssignImageList(self._img_list)

        self.root = self.tree.AddRoot('[no device list loaded]')

        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        self.devfile_choice = wx.Choice(self, choices=self._list_dev_files())
        btn_load = wx.Button(self, label='Load')
        btn_load.Bind(wx.EVT_BUTTON, self._on_load_clicked)
        btn_new_list = wx.Button(self, label='New')
        btn_new_list.Bind(wx.EVT_BUTTON, self._on_new_list)
        toolbar.Add(self.devfile_choice, 1, wx.EXPAND | wx.RIGHT, self.FromDIP(PAD_SMALL))
        toolbar.Add(btn_load, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        toolbar.Add(btn_new_list, 0)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(toolbar, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        sizer.Add(self.tree, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(PAD_SMALL))
        self.SetSizer(sizer)

        self.tree.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.tree.Bind(wx.EVT_TREE_ITEM_ACTIVATED, self._on_item_activated)
        self.tree.Bind(wx.EVT_TREE_ITEM_RIGHT_CLICK, self._on_tree_right_click)
        self.tree.Bind(wx.EVT_TREE_SEL_CHANGED, self._on_tree_sel_changed)

        self._try_autoload_last()

    # -----------------------------------------------------------------
    def _list_dev_files(self):
        try:
            return sorted(p.name for p in dev_dir().glob('*.dev'))
        except Exception:
            return []

    def _try_autoload_last(self):
        settings = load_settings()
        last = settings.get('last_devfile', '')
        if not last:
            return
        path = dev_dir() / last
        if not path.exists():
            settings['last_devfile'] = ''   # stale reference — clean it up rather
            save_settings(settings)          # than retrying it silently forever
            return
        if self.devfile_choice.SetStringSelection(last):
            self.load_devfile(path)

    def _on_new_list(self, event):
        """'New' next to Load: create a device list without needing the
        (possibly hidden) Device Editor — name dialog, duplicate check,
        pre-seeded with control's 4 standard commands and the script
        device (+ custom/example.py on a fresh install), then loaded
        immediately."""
        with wx.TextEntryDialog(self, 'Name for the new device list:',
                                'New device list') as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.GetValue()
        devfile, error = _create_device_list(name)
        if error:
            wx.MessageBox(error, 'New device list', wx.OK | wx.ICON_ERROR)
            return
        self.devfile_choice.Set(self._list_dev_files())
        self.devfile_choice.SetStringSelection(devfile.name)
        self.load_devfile(devfile)   # full Path — load_devfile's contract
                                     # (a bare name resolves against CWD
                                     # and silently fails to refresh)
        if self.on_new_list_created is not None:
            self.on_new_list_created()

    def _on_load_clicked(self, event):
        if self.is_run_active is not None and self.is_run_active():
            wx.MessageBox('Cannot load a device list while a run is in progress.',
                          'Run in progress', wx.OK | wx.ICON_WARNING)
            return
        name = self.devfile_choice.GetStringSelection()
        if not name:
            wx.MessageBox('No device list selected.', 'Load', wx.OK | wx.ICON_WARNING)
            return
        self.load_devfile(dev_dir() / name)

    # -----------------------------------------------------------------
    def _populate_script_children(self, dev_item):
        """(Re)builds the 'script' device's children from whatever .py
        files currently exist in custom_dir(). Deletes any existing
        children first, so this is safe to call again on an already-
        populated item — used both by load_devfile (building the tree
        fresh) and by _on_left_down's LED toggle (refreshing an
        existing item in place). requirement: 'when I add a new file to the
        custom folder I would need to restart qmeas to see it. Can this
        be solved by checking each time you activate the LED?' — yes:
        every other device's commands are already re-read from disk on
        every load_devfile call (no persistent cache to invalidate), so
        'script' re-scanning on toggle is the same recency guarantee
        applied at one more, more convenient point, not a new kind of
        staleness handling. Existing task-list rows referencing a
        script command are entirely unaffected by rebuilding this —
        they store their alias as a plain string in the grid,
        independent of whatever the tree currently shows."""
        self.tree.DeleteChildren(dev_item)
        try:
            py_names = sorted(p.stem for p in custom_dir().glob('*.py') if _NAME_RE.match(p.stem))
        except Exception:
            py_names = []
        for cmd_name in py_names:
            cmd_item = self.tree.AppendItem(dev_item, cmd_name, image=self._idx_arrow_out)
            self.tree.SetItemData(cmd_item, {'kind': 'command', 'device': SCRIPT_DEVICE_NAME,
                                             'command': cmd_name, 'type': 'wf'})

    def load_devfile(self, path):
        """Clears the tree and reloads from path (a .dev file). Each
        device's {alias}_commands.txt is read independently — one bad
        file is skipped (device shown with no children) rather than
        aborting the whole list.

        This is called not just for the initial/explicit Load, but
        also after every tree-mutating action (add/rename/delete
        device or command — see the 5 call sites) to refresh the
        display from disk. requirement: 'once I edit a command, the whole
        device tree is activated automatically, even if some elements
        were collapsed/inactive' — that was this method unconditionally
        rebuilding every device as 'on' and calling ExpandAll(),
        discarding whatever on/off and expand/collapse state existed
        before the edit. Fixed by capturing that state (keyed by
        device/command NAME, not by tree item — the old items are
        about to be destroyed) before clearing the tree, then
        reapplying it by name afterward. A device/command that didn't
        exist before (genuinely new) gets the original defaults: on,
        expanded — everything else keeps whatever the user had set."""
        prior_device_state = {}    # name -> {'on': bool, 'expanded': bool}
        prior_command_state = {}   # (device, command) -> bool (on)
        dev_item, dev_cookie = self.tree.GetFirstChild(self.root)
        while dev_item.IsOk():
            data = self.tree.GetItemData(dev_item)
            if data and data.get('kind') == 'device':
                prior_device_state[data['name']] = {
                    'on': data.get('on', True),
                    'expanded': self.tree.IsExpanded(dev_item),
                }
                cmd_item, cmd_cookie = self.tree.GetFirstChild(dev_item)
                while cmd_item.IsOk():
                    cmd_data = self.tree.GetItemData(cmd_item)
                    if cmd_data and cmd_data.get('kind') == 'command' and 'on' in cmd_data:
                        prior_command_state[(cmd_data['device'], cmd_data['command'])] = cmd_data['on']
                    cmd_item, cmd_cookie = self.tree.GetNextChild(dev_item, cmd_cookie)
            dev_item, dev_cookie = self.tree.GetNextChild(self.root, dev_cookie)

        try:
            device_names = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
        except Exception as e:
            wx.MessageBox(f'Could not read {path.name}:\n{e}', 'Load failed', wx.OK | wx.ICON_ERROR)
            return
        if SCRIPT_DEVICE_NAME not in device_names:
            # Injected rather than requiring every existing .dev file to be
            # migrated — this is what makes 'script' appear in device lists
            # created before this feature existed, not just new ones (see
            # _on_new_devfile, which also writes it explicitly for new lists).
            device_names.append(SCRIPT_DEVICE_NAME)
        # DISPLAY order only (the .dev file keeps its append-chronological
        # line order, staying v1-compatible): 'control' first, 'script'
        # second, real devices alphabetical below them.
        device_names.sort(key=lambda n: (
            0 if n == STANDARD_DEVICE_NAME else
            1 if n == SCRIPT_DEVICE_NAME else 2, n))

        self.current_devfile = path
        # Atomic rebuild: selection cleared FIRST (so DeleteAllItems
        # doesn't fire a selection-changed event mid-deletion), handlers
        # gated by _loading_tree, painting frozen for the duration, and
        # an explicit Refresh at the end — all four against the MSW
        # TreeCtrl artifact where rows of the old tree stay painted
        # below the rebuilt one.
        self._loading_tree = True
        self.tree.Freeze()
        try:
            self.tree.UnselectAll()
            self.tree.DeleteAllItems()
            self.root = self.tree.AddRoot(path.stem)
            self._rebuild_tree_body(path, device_names, prior_device_state, prior_command_state)
        finally:
            self.tree.Thaw()
            self._loading_tree = False
            self.tree.Refresh()

    def _rebuild_tree_body(self, path, device_names, prior_device_state, prior_command_state):
        """The device-population part of load_devfile, factored out so
        the Freeze/guard wrapper above stays readable. Same logic as
        before, byte-for-byte."""
        stem = path.with_suffix('')
        failed = []

        for devicename in device_names:
            prior = prior_device_state.get(devicename)
            # 'control' is always on regardless of any prior capture —
            # it can never actually be toggled off (see _on_left_down),
            # so there's nothing legitimate to preserve for it here.
            # 'script' defaults to OFF specifically when there's no prior
            # state yet (requirement: 'It is turned off by default') — every
            # other device, including a genuinely new one, defaults to on.
            if devicename == STANDARD_DEVICE_NAME:
                is_on = True
            elif prior is not None:
                is_on = prior['on']
            else:
                is_on = devicename != SCRIPT_DEVICE_NAME
            dev_item = self.tree.AppendItem(self.root, devicename,
                                            image=self._idx_led_on if is_on else self._idx_led_off)
            self.tree.SetItemData(dev_item, {'kind': 'device', 'name': devicename, 'on': is_on})

            if devicename == SCRIPT_DEVICE_NAME:
                # No commands file — 'commands' here are just whatever
                # .py files exist in custom_dir() right now, re-scanned
                # fresh on every load_devfile call (same recency as every
                # other device's commands file, which is also re-read from
                # disk every time). requirement: 'qmeas doesnt check anything. It
                # executes' — that's about running them (see run()'s
                # SCRIPT_DEVICE_NAME branch), not about this listing; a
                # filename that isn't a legal command name (_NAME_RE) is
                # just silently not listed, not an error, since 'custom'
                # is a folder the user manages directly and might contain
                # other things (a README, an __pycache__, etc.).
                self._populate_script_children(dev_item)
                if not is_on or (prior is not None and not prior['expanded']):
                    # Expansion follows the LED: an OFF device never
                    # loads expanded (requirement: 'when the LED is off this
                    # shouldnt be the case') — matching what the toggle
                    # itself already does (_on_left_down: deactivate ->
                    # Collapse, reactivate -> Expand). Before this, a
                    # fresh load defaulted every device to expanded
                    # regardless of state, so script — off by default —
                    # showed its children anyway. An ON device keeps
                    # its prior expand/collapse choice as before.
                    self.tree.Collapse(dev_item)
                else:
                    self.tree.Expand(dev_item)
                continue

            cmdfile = stem / f'{devicename}_commands.txt'
            try:
                lines = cmdfile.read_text().splitlines()
            except Exception:
                failed.append(devicename)
                continue
            try:
                for line in lines:
                    if not line.strip():
                        continue
                    fields = line.split('|')
                    cmd_name, cmd_type = fields[1], fields[2]
                    if cmd_type == 'q':
                        # LED is a tri-state: on (green, queried and
                        # recorded) / verify (red, queried but never
                        # recorded — see _query_led_state_from_field0) /
                        # off (gray, not queried). Session state (this
                        # tree, before the reload that triggered this
                        # call) wins if present; otherwise fall back to
                        # what was last persisted to field[0] on close
                        # (see save_query_led_states) rather than
                        # hardcoding on — a genuinely fresh app start
                        # should restore whatever you left it as.
                        if (devicename, cmd_name) in prior_command_state:
                            cmd_state = prior_command_state[(devicename, cmd_name)]
                        else:
                            cmd_state = _query_led_state_from_field0(fields[0] if fields else '')
                        displayed_state = cmd_state if is_on else 'off'
                        icon = {'on': self._idx_led_on, 'verify': self._idx_led_verify,
                               'off': self._idx_led_off}[displayed_state]
                        cmd_item = self.tree.AppendItem(dev_item, cmd_name, image=icon)
                        self.tree.SetItemData(cmd_item, {'kind': 'command', 'device': devicename,
                                                         'command': cmd_name, 'type': cmd_type,
                                                         'on': cmd_state})
                    else:
                        icon = self._idx_arrow_out
                        cmd_item = self.tree.AppendItem(dev_item, cmd_name, image=icon)
                        self.tree.SetItemData(cmd_item, {'kind': 'command', 'device': devicename,
                                                         'command': cmd_name, 'type': cmd_type})
            except Exception:
                failed.append(devicename)

            if not is_on or (prior is not None and not prior['expanded']):
                # Same LED-follows rule as the script branch above: an
                # OFF device never loads expanded; an ON device keeps
                # its prior choice (default for a genuinely new ON
                # device: expanded).
                self.tree.Collapse(dev_item)
            else:
                self.tree.Expand(dev_item)

        if failed:
            wx.MessageBox('Could not read commands for:\n' + '\n'.join(failed),
                          'Some devices skipped', wx.OK | wx.ICON_WARNING)

        # Keep the dropdown in sync regardless of what triggered this load
        # (user clicking Load, or an auto-reload from Device Editor Save).
        self.devfile_choice.Set(self._list_dev_files())
        self.devfile_choice.SetStringSelection(path.name)

        settings = load_settings()
        settings['last_devfile'] = path.name
        save_settings(settings)

    def _refresh_command_leds(self, dev_item):
        """Sync every query-type child's DISPLAYED LED to reflect the
        parent device's own state — requirement: 'when I deactivate a device
        (LED gray) then also all queries in that branch should be
        grayed out.' A query's displayed icon reflects its own stored
        tri-state (on/verify/off) only when the parent device is also
        on; its own stored state is never touched here, so
        reactivating the device later restores each query to whatever
        it was individually set to before, not force-reset to on.
        Purely a display update — get_active_query_specs already
        correctly excludes a query under an inactive device regardless
        of this; this just makes the tree stop showing something as
        active that won't actually run."""
        dev_data = self.tree.GetItemData(dev_item)
        if not dev_data or dev_data.get('kind') != 'device':
            return
        dev_on = dev_data.get('on', True)
        cmd_item, cmd_cookie = self.tree.GetFirstChild(dev_item)
        while cmd_item.IsOk():
            cmd_data = self.tree.GetItemData(cmd_item)
            if cmd_data and cmd_data.get('type') == 'q':
                displayed_state = cmd_data.get('on', 'on') if dev_on else 'off'
                icon = {'on': self._idx_led_on, 'verify': self._idx_led_verify,
                       'off': self._idx_led_off}[displayed_state]
                self.tree.SetItemImage(cmd_item, icon)
            cmd_item, cmd_cookie = self.tree.GetNextChild(dev_item, cmd_cookie)

    def _on_left_down(self, event):
        item, flags = self.tree.HitTest(event.GetPosition())
        if item.IsOk() and (flags & wx.TREE_HITTEST_ONITEMICON):
            if self.is_run_active is not None and self.is_run_active():
                # Frozen during a run: query specs and row eligibility
                # were snapshotted at Start — a toggle here would have
                # NO effect on the running measurement while looking
                # like it does, and turning a device off would mutate
                # grid rows the run addresses by index.
                return
            data = self.tree.GetItemData(item)
            if data and data.get('kind') == 'device':
                if data['name'] == STANDARD_DEVICE_NAME:
                    return   # control is always active — click consumed, no toggle
                data['on'] = not data['on']
                self.tree.SetItemImage(
                    item, self._idx_led_on if data['on'] else self._idx_led_off)
                self._refresh_command_leds(item)
                if data['name'] == SCRIPT_DEVICE_NAME:
                    # requirement: 'when I add a new file to the custom folder
                    # I would need to restart qmeas to see it. Can this
                    # be solved by checking each time you activate the
                    # LED?' — yes, same re-scan _populate_script_children
                    # already does on every load_devfile, just also
                    # triggered here.
                    self._populate_script_children(item)
                if data['on']:
                    self.tree.Expand(item)     # reactivated: show its commands again
                else:
                    self.tree.Collapse(item)   # deactivated: fold away its commands
                if self.on_device_toggled is not None:
                    self.on_device_toggled(data['name'], data['on'])
                return   # consumed: LED click, not selection
            if data and data.get('kind') == 'command' and data.get('type') == 'q':
                # 3-way cycle: on (green, saved) -> verify (red, queried
                # but excluded from data — a visual note-to-self that
                # this one's a verify target elsewhere, not an accident)
                # -> off (gray, not queried) -> on. Persisted on close —
                # see save_query_led_states.
                data['on'] = _QUERY_LED_NEXT[data.get('on', 'on')]
                dev_data = self.tree.GetItemData(self.tree.GetItemParent(item))
                dev_on = dev_data.get('on', True) if dev_data else True
                displayed_state = data['on'] if dev_on else 'off'
                icon = {'on': self._idx_led_on, 'verify': self._idx_led_verify,
                       'off': self._idx_led_off}[displayed_state]
                self.tree.SetItemImage(item, icon)
                return   # consumed: LED click, not selection
        event.Skip()

    def _on_item_activated(self, event):
        item = event.GetItem()
        data = self.tree.GetItemData(item)
        if not data:
            return
        if data.get('kind') == 'device':
            # Double-click a device: load its settings into the Device
            # Editor AND bring that pane into view, even if it's
            # currently hidden/docked away — the single-click path
            # (_on_tree_sel_changed) only loads, since the pane may not
            # be visible and popping it open on every ordinary selection
            # click would be intrusive.
            if data['name'] in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME):
                return   # virtual device, no settings file to edit
            stem = self.current_devfile.with_suffix('')
            if self.on_device_selected is not None:
                self.on_device_selected(data['name'], stem)
            if self.on_device_editor_reveal is not None:
                self.on_device_editor_reveal()
            return
        if data.get('kind') != 'command':
            return
        if data.get('type') != 'wf':
            return   # query/read commands don't go into the task list
        if self.on_add_command is not None:
            self.on_add_command(data['device'], data['command'])

    def _on_tree_sel_changed(self, event):
        """Plain single-click (or arrow-key) selection of a device:
        load its settings into the Device Editor if that callback is
        wired, but don't force the pane into view — see
        _on_item_activated for the double-click version, which does
        both. Only fires for genuine selection changes: the LED-icon
        click path in _on_left_down fully consumes its mouse event
        (returns without event.Skip()), so toggling a device on/off
        never also triggers this."""
        if self._loading_tree or not self.tree:
            # not self.tree: the C++ widget is already destroyed — wx
            # delivers the deletion-induced selection-changed event
            # DURING app/pane teardown, after the tree died (observed:
            # RuntimeError 'wrapped C/C++ object ... has been deleted'
            # at shutdown). A dead proxy is falsy in wxPython.
            return
        item = event.GetItem()
        if not item.IsOk():
            event.Skip()
            return
        data = self.tree.GetItemData(item)
        if (data and data.get('kind') == 'device'
                and data['name'] not in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME)
                and self.on_device_selected is not None):
            self.on_device_selected(data['name'], self.current_devfile.with_suffix(''))
        event.Skip()

    # -----------------------------------------------------------------
    # Right-click on a device: Rename / Delete
    # -----------------------------------------------------------------
    def _on_tree_right_click(self, event):
        if self.is_run_active is not None and self.is_run_active():
            return   # frozen: Add/Edit/Rename/Delete all rewrite files
                     # the running thread reads from disk on every row
        item = event.GetItem()
        data = self.tree.GetItemData(item)
        if not data or self.current_devfile is None:
            return
        # 'script' has no commands file to Add/Edit/Delete against, and is
        # itself not renamable/deletable (a reserved virtual device, same
        # spirit as 'control') — its .py entries are managed directly by the
        # user in custom_dir(), not through qmeas' UI. No menu at all here.
        if (data.get('kind') == 'device' and data['name'] == SCRIPT_DEVICE_NAME) or \
           (data.get('kind') == 'command' and data.get('device') == SCRIPT_DEVICE_NAME):
            return
        # requirement: 'I can even delete the control device, which should not
        # be possible... 1. Control must not be deletable 2. pause,
        # counter, stop and userprompt must be permanent (no deletion,
        # no renaming) 3. you can only add commands, rename and delete
        # additional commands.' The comment two lines up already
        # described 'control' as reserved 'same spirit as script' —
        # that was never actually enforced for control itself until
        # now, only for script. _RESERVED_CONTROL_COMMAND_NAMES is
        # derived from _STANDARD_COMMANDS itself (not a second hand-
        # typed list) so the two can't drift apart.
        # Preparation ONLY for a future while-loop feature (requirement:
        # 'prepare... but do not incorporate') — this makes 'control'
        # safe to keep extending with MORE fixed commands later without
        # touching this guard again: Add Command already works for
        # control (needed for while-commands to have somewhere to go),
        # and any newly-added command is automatically NOT in this
        # frozen set, so it's freely rename/delete-able like requirement
        # 3 asks — no while-loop logic itself is added here.
        if data.get('kind') == 'device' and data['name'] == STANDARD_DEVICE_NAME:
            # 'Add Virtual While...' REPLACES the generic 'Add Command...'
            # here (requirement: 'go ahead as suggested'): a generic command on
            # control was always a dead end — the execution path reads
            # control.txt, which doesn't exist (control is virtual, no
            # VISA address) — so keeping both would leave two visually
            # identical ways to add a control command, only one of which
            # works. Every other device keeps its generic menu below,
            # untouched.
            menu = wx.Menu()
            item_addwhile = menu.Append(wx.ID_ANY, 'Add Virtual While...')
            self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_add_while(it), item_addwhile)
            self.tree.PopupMenu(menu)
            menu.Destroy()
            return
        if (data.get('kind') == 'command' and data.get('device') == STANDARD_DEVICE_NAME
                and data.get('command') in _RESERVED_CONTROL_COMMAND_NAMES):
            return   # pause/userprompt/stop/counter: permanent, no menu at all —
                     # same treatment as script's own entries above
        if data.get('kind') == 'command' and data.get('device') == STANDARD_DEVICE_NAME:
            # A non-reserved control command: a while-command gets Edit
            # routed to WhileEditorDialog — opening the generic
            # CommandEditorDialog on a while line would silently drop
            # fields 19-23 on save (its get_command_line only writes
            # 0-18), turning a working while into a dead entry. Delete
            # stays the generic handler (removing a line needs no
            # dialog). A legacy dead generic command on control (created
            # through the old, since-removed Add Command path — never
            # executable) gets Delete only: the one sensible operation.
            stem = self.current_devfile.with_suffix('')
            is_while = _get_while_definition(stem, data['command']) is not None
            menu = wx.Menu()
            if is_while:
                item_edit = menu.Append(wx.ID_ANY, 'Edit Virtual While...')
                self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_edit_while(it), item_edit)
            item_delete = menu.Append(wx.ID_ANY, 'Delete Command...')
            self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_delete_command(it), item_delete)
            self.tree.PopupMenu(menu)
            menu.Destroy()
            return
        if data.get('kind') == 'device':
            menu = wx.Menu()
            item_addcmd = menu.Append(wx.ID_ANY, 'Add Command...')
            menu.AppendSeparator()
            item_rename = menu.Append(wx.ID_ANY, 'Rename Device...')
            if data.get('name') not in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME):
                item_dup = menu.Append(wx.ID_ANY, 'Duplicate Device')
                self.tree.Bind(wx.EVT_MENU,
                               lambda evt, it=item: self._on_duplicate_device(it),
                               item_dup)
            item_delete = menu.Append(wx.ID_ANY, 'Delete Device...')
            self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_add_command(it), item_addcmd)
            self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_rename_device(it), item_rename)
            self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_delete_device(it), item_delete)
            self.tree.PopupMenu(menu)
            menu.Destroy()
        elif data.get('kind') == 'command':
            menu = wx.Menu()
            item_edit = menu.Append(wx.ID_ANY, 'Edit Command...')
            item_delete = menu.Append(wx.ID_ANY, 'Delete Command...')
            self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_edit_command(it), item_edit)
            self.tree.Bind(wx.EVT_MENU, lambda evt, it=item: self._on_delete_command(it), item_delete)
            self.tree.PopupMenu(menu)
            menu.Destroy()

    def _on_add_command(self, item):
        data = self.tree.GetItemData(item)
        device_name = data['name']
        stem = self.current_devfile.with_suffix('')
        with CommandEditorDialog(self, device_name, stem) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            line = dlg.get_command_line()
        cmdfile = stem / f'{device_name}_commands.txt'
        try:
            with open(cmdfile, 'a') as f:
                f.write(line + '\n')
        except Exception as e:
            wx.MessageBox(f'Could not save command:\n{e}', 'Add Command failed', wx.OK | wx.ICON_ERROR)
            return
        self.load_devfile(self.current_devfile)

    def _on_add_while(self, item):
        """control's 'Add Virtual While...' — same append-a-line flow as
        _on_add_command, with WhileEditorDialog doing all validation
        before it can close with OK."""
        stem = self.current_devfile.with_suffix('')
        with WhileEditorDialog(self, stem) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            line = dlg.get_command_line()
        cmdfile = stem / f'{STANDARD_DEVICE_NAME}_commands.txt'
        try:
            with open(cmdfile, 'a') as f:
                f.write(line + '\n')
        except Exception as e:
            wx.MessageBox(f'Could not save command:\n{e}', 'Add Virtual While failed',
                          wx.OK | wx.ICON_ERROR)
            return
        self.load_devfile(self.current_devfile)

    def _on_edit_while(self, item):
        """Edit an existing while-command — same replace-line-in-place
        flow as _on_edit_command, but through WhileEditorDialog (the
        generic editor would drop fields 19-23 — see the tree menu
        routing). Only reachable for lines _get_while_definition
        recognizes."""
        data = self.tree.GetItemData(item)
        cmd_name = data['command']
        stem = self.current_devfile.with_suffix('')
        cmdfile = stem / f'{STANDARD_DEVICE_NAME}_commands.txt'
        try:
            lines = [ln for ln in cmdfile.read_text().splitlines() if ln.strip()]
        except Exception as e:
            wx.MessageBox(f'Could not read command file:\n{e}', 'Edit failed', wx.OK | wx.ICON_ERROR)
            return
        idx = next((i for i, ln in enumerate(lines) if ln.split('|')[1] == cmd_name), None)
        if idx is None:
            wx.MessageBox(f"Command '{cmd_name}' not found.", 'Edit failed', wx.OK | wx.ICON_ERROR)
            return
        with WhileEditorDialog(self, stem) as dlg:
            dlg.SetTitle(f'Edit Virtual While — {STANDARD_DEVICE_NAME}')
            dlg.load_from_line(lines[idx])
            if dlg.ShowModal() != wx.ID_OK:
                return
            new_line = dlg.get_command_line()
        lines[idx] = new_line
        try:
            cmdfile.write_text('\n'.join(lines) + '\n')
        except Exception as e:
            wx.MessageBox(f'Could not save command file:\n{e}', 'Edit failed', wx.OK | wx.ICON_ERROR)
            return
        self.load_devfile(self.current_devfile)

    def _on_duplicate_device(self, item):
        """Right-click -> Duplicate Device: copies the device (settings
        + all commands) under '<name>copy[N]'. The copy's address is
        BLANK and the copy starts DEACTIVATED — both deliberate, so a
        user who forgets to give the copy its own address gets a loud
        error instead of two devices silently sharing one instrument.
        (The off state is per-session; the blank address is the durable
        guard.)"""
        data = self.tree.GetItemData(item)
        if not data or self.current_devfile is None:
            return
        newname, error = _duplicate_device(self.current_devfile, data['name'])
        if error:
            wx.MessageBox(error, 'Duplicate Device', wx.OK | wx.ICON_ERROR)
            return
        self.load_devfile(self.current_devfile)
        self._set_device_on(newname, False)
        wx.MessageBox(
            f"Duplicated as '{newname}'.\n\n"
            f"The copy has NO address and is deactivated: open it in the "
            f"Device Editor, give it its own address, then activate it.",
            'Duplicate Device', wx.OK | wx.ICON_INFORMATION)

    def _set_device_on(self, name, on):
        """Find a device item by name and set its on/off state + icon —
        the programmatic twin of the LED-click toggle."""
        child, cookie = self.tree.GetFirstChild(self.root)
        while child.IsOk():
            data = self.tree.GetItemData(child)
            if data and data.get('kind') == 'device' and data.get('name') == name:
                data['on'] = on
                self.tree.SetItemImage(
                    child, self._idx_led_on if on else self._idx_led_off)
                return
            child, cookie = self.tree.GetNextChild(self.root, cookie)

    def _on_rename_device(self, item):
        data = self.tree.GetItemData(item)
        old_name = data['name']
        with NameEntryDialog(self, 'Rename Device', f"New name for '{old_name}':",
                             initial_value=old_name) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            new_name = dlg.get_value().strip()
        if not new_name or new_name == old_name:
            return
        if not _NAME_RE.match(new_name):
            wx.MessageBox('Device name may only contain lowercase letters (a-z) and digits (0-9).',
                          'Cannot rename', wx.OK | wx.ICON_WARNING)
            return

        existing = _collect_device_names(self.current_devfile)
        if new_name in existing or new_name in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME):
            wx.MessageBox(f"'{new_name}' already exists in this device list.",
                          'Cannot rename', wx.OK | wx.ICON_WARNING)
            return

        updated = [new_name if n == old_name else n for n in existing]
        try:
            self.current_devfile.write_text('\n'.join(updated) + '\n')
        except Exception as e:
            wx.MessageBox(f'Could not update device list:\n{e}', 'Rename failed', wx.OK | wx.ICON_ERROR)
            return

        stem = self.current_devfile.with_suffix('')
        for suffix in ('.txt', '_commands.txt'):
            src, dst = stem / f'{old_name}{suffix}', stem / f'{new_name}{suffix}'
            try:
                if src.exists():
                    src.rename(dst)
            except Exception as e:
                wx.MessageBox(f'Could not rename {src.name}:\n{e}', 'File error', wx.OK | wx.ICON_WARNING)

        self.load_devfile(self.current_devfile)

    def _on_delete_device(self, item):
        data = self.tree.GetItemData(item)
        name = data['name']
        # Confirmation here specifically (unlike task-grid row delete,
        # which has none): a device's command file is real configured
        # work, not trivially reconstructible UI state.
        if wx.MessageBox(f"Delete device '{name}' and its command file?\n"
                         "This cannot be undone.", 'Delete Device',
                         wx.YES_NO | wx.ICON_WARNING) != wx.YES:
            return

        existing = _collect_device_names(self.current_devfile)
        updated = [n for n in existing if n != name]
        try:
            self.current_devfile.write_text('\n'.join(updated) + '\n' if updated else '')
        except Exception as e:
            wx.MessageBox(f'Could not update device list:\n{e}', 'Delete failed', wx.OK | wx.ICON_ERROR)
            return

        stem = self.current_devfile.with_suffix('')
        for suffix in ('.txt', '_commands.txt'):
            try:
                (stem / f'{name}{suffix}').unlink(missing_ok=True)
            except Exception:
                pass

        self.load_devfile(self.current_devfile)

    # -----------------------------------------------------------------
    # Rename / delete a single command within a device
    # -----------------------------------------------------------------
    def _on_edit_command(self, item):
        """Full edit — name AND definition (mode, string, number format /
        extraction settings) — not just a name-only rename. Opens the
        same CommandEditorDialog used for Add, pre-populated via
        load_from_line(), and replaces the matching line in place."""
        data = self.tree.GetItemData(item)
        device_name, cmd_name = data['device'], data['command']
        stem = self.current_devfile.with_suffix('')
        cmdfile = stem / f'{device_name}_commands.txt'

        try:
            lines = [ln for ln in cmdfile.read_text().splitlines() if ln.strip()]
        except Exception as e:
            wx.MessageBox(f'Could not read command file:\n{e}', 'Edit failed', wx.OK | wx.ICON_ERROR)
            return
        idx = next((i for i, ln in enumerate(lines) if ln.split('|')[1] == cmd_name), None)
        if idx is None:
            wx.MessageBox(f"Command '{cmd_name}' not found.", 'Edit failed', wx.OK | wx.ICON_ERROR)
            return

        with CommandEditorDialog(self, device_name, stem) as dlg:
            dlg.SetTitle(f'Edit Command — {device_name}')
            dlg.load_from_line(lines[idx])
            if dlg.ShowModal() != wx.ID_OK:
                return
            new_line = dlg.get_command_line()

        # No duplicate-name check needed here anymore — CommandEditorDialog's
        # own _on_ok now validates this (excluding the command's own
        # original name) before the dialog can ever close with OK, so a
        # colliding name never makes it this far.
        lines[idx] = new_line
        try:
            cmdfile.write_text('\n'.join(lines) + '\n')
        except Exception as e:
            wx.MessageBox(f'Could not save command file:\n{e}', 'Edit failed', wx.OK | wx.ICON_ERROR)
            return
        self.load_devfile(self.current_devfile)

    def _on_delete_command(self, item):
        data = self.tree.GetItemData(item)
        device_name, cmd_name = data['device'], data['command']
        stem = self.current_devfile.with_suffix('')
        cmdfile = stem / f'{device_name}_commands.txt'

        if wx.MessageBox(f"Delete command '{cmd_name}'?\nThis cannot be undone.",
                         'Delete Command', wx.YES_NO | wx.ICON_WARNING) != wx.YES:
            return

        try:
            lines = [ln for ln in cmdfile.read_text().splitlines() if ln.strip()]
        except Exception as e:
            wx.MessageBox(f'Could not read command file:\n{e}', 'Delete failed', wx.OK | wx.ICON_ERROR)
            return

        updated = [ln for ln in lines if ln.split('|')[1] != cmd_name]
        try:
            cmdfile.write_text('\n'.join(updated) + '\n' if updated else '')
        except Exception as e:
            wx.MessageBox(f'Could not save command file:\n{e}', 'Delete failed', wx.OK | wx.ICON_ERROR)
            return
        self.load_devfile(self.current_devfile)

    # -----------------------------------------------------------------
    def get_active_query_specs(self) -> list:
        """Every read/query command whose own tree LED is GREEN ('on')
        AND whose parent device is on, across the WHOLE loaded device
        list — not just one device. Red ('verify') and gray ('off')
        are both excluded here identically: a red LED still gets
        queried directly by whichever write command uses it as a
        verify target (_query_for_verify, independent of this list
        entirely) — this only controls whether it's ALSO polled every
        step and recorded as if it were ordinary measurement data.
        Called once from TasksPanel._on_start_stop, on the main
        thread, before TaskRunnerThread starts — this touches
        self.tree directly, so it must never be called from the worker
        thread. Deliberately a one-time snapshot, unlike task-row data
        (which TaskRunnerThread now fetches live, row by row — see its
        class docstring): the Devices tree stays fully frozen for the
        whole run, so nothing here could change even if re-polled.
        Returns [] if no device list is loaded."""
        specs = []
        if self.current_devfile is None:
            return specs
        dev_item, dev_cookie = self.tree.GetFirstChild(self.root)
        while dev_item.IsOk():
            dev_data = self.tree.GetItemData(dev_item)
            if dev_data and dev_data.get('kind') == 'device' and dev_data.get('on'):
                cmd_item, cmd_cookie = self.tree.GetFirstChild(dev_item)
                while cmd_item.IsOk():
                    cmd_data = self.tree.GetItemData(cmd_item)
                    if cmd_data and cmd_data.get('type') == 'q' and cmd_data.get('on', 'on') == 'on':
                        specs.append((cmd_data['device'], cmd_data['command']))
                    cmd_item, cmd_cookie = self.tree.GetNextChild(dev_item, cmd_cookie)
            dev_item, dev_cookie = self.tree.GetNextChild(self.root, dev_cookie)
        return specs

    def save_query_led_states(self):
        """Persist every query command's CURRENT tri-state LED
        (on/verify/off) back to field[0] of its line in that device's
        _commands.txt — called once from QMeasMain._on_close, right
        before the frame is destroyed, so whatever you left the LEDs as
        is what you get back next launch (requirement: 'save the state when
        qmeas closes'). NOT called on every click — LED state during a
        session is cheap in-memory bookkeeping (same as it's always
        been); only the final state at shutdown is written to disk.

        Skipped entirely (silently — there is no useful recovery action
        to offer during shutdown) if no list is loaded, or if a run is
        active: the run thread reads command files from disk on every
        row, and rewriting one out from under it would race exactly
        the failure mode _on_save_device already guards against for
        the same reason. One device's file failing to read/write does
        not stop the others — best-effort, not all-or-nothing."""
        if self.current_devfile is None:
            return
        if self.is_run_active is not None and self.is_run_active():
            return
        # device_name -> {command_name: 'on'/'verify'/'off'}
        by_device = {}
        dev_item, dev_cookie = self.tree.GetFirstChild(self.root)
        while dev_item.IsOk():
            dev_data = self.tree.GetItemData(dev_item)
            if dev_data and dev_data.get('kind') == 'device':
                states = {}
                cmd_item, cmd_cookie = self.tree.GetFirstChild(dev_item)
                while cmd_item.IsOk():
                    cmd_data = self.tree.GetItemData(cmd_item)
                    if cmd_data and cmd_data.get('type') == 'q':
                        states[cmd_data['command']] = cmd_data.get('on', 'on')
                    cmd_item, cmd_cookie = self.tree.GetNextChild(dev_item, cmd_cookie)
                if states:
                    by_device[dev_data['name']] = states
            dev_item, dev_cookie = self.tree.GetNextChild(self.root, dev_cookie)

        stem = self.current_devfile.with_suffix('')
        for device_name, states in by_device.items():
            cmdfile = stem / f'{device_name}_commands.txt'
            try:
                original = cmdfile.read_text()
                lines = original.splitlines()
            except Exception:
                continue   # device file unreadable — nothing to update, don't block the others
            changed = False
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                fields = line.split('|')
                if len(fields) > 2 and fields[2] == 'q' and fields[1] in states:
                    new_field0 = _QUERY_LED_TO_FIELD0[states[fields[1]]]
                    if fields[0] != new_field0:
                        fields[0] = new_field0
                        lines[i] = '|'.join(fields)
                        changed = True
            if not changed:
                continue   # avoid a spurious rewrite/mtime touch when nothing actually moved
            try:
                cmdfile.write_text('\n'.join(lines) + '\n')
            except Exception:
                continue   # best-effort — a failed write here shouldn't block the app closing

    def get_active_onhold_devices(self) -> list:
        """Every device that's ACTIVE (tree LED on) and has a write-type
        command literally named 'onhold' — qmeas v1.1 precedent (requested):
        a reserved command name. On abort, TaskRunnerThread sends it
        immediately to every device in this list — write-and-forget, no
        ramp, no verify, since it's an emergency stop, not a
        measurement step. Write commands have no on/off LED of their
        own (unlike queries) — only the PARENT DEVICE's on/off state
        matters here, matching v1.1's own description ('any active
        device whose command alias ends in _onhold'). Same one-time-
        snapshot pattern and same main-thread-only constraint as
        get_active_query_specs. Returns [] if no device list is
        loaded, or none of the loaded devices happen to have one — in
        which case sending onhold on abort is simply a no-op, matching
        v1.1's own 'completely silent, no side effects' behavior for
        that case."""
        devices = []
        if self.current_devfile is None:
            return devices
        dev_item, dev_cookie = self.tree.GetFirstChild(self.root)
        while dev_item.IsOk():
            dev_data = self.tree.GetItemData(dev_item)
            if dev_data and dev_data.get('kind') == 'device' and dev_data.get('on'):
                cmd_item, cmd_cookie = self.tree.GetFirstChild(dev_item)
                while cmd_item.IsOk():
                    cmd_data = self.tree.GetItemData(cmd_item)
                    if (cmd_data and cmd_data.get('type') == 'wf'
                            and cmd_data.get('command') == 'onhold'):
                        devices.append(dev_data['name'])
                        break   # one onhold per device is enough to know it has one
                    cmd_item, cmd_cookie = self.tree.GetNextChild(dev_item, cmd_cookie)
            dev_item, dev_cookie = self.tree.GetNextChild(self.root, dev_cookie)
        return devices

    def get_active_device_names(self) -> set:
        """Names of every device whose tree LED is on — snapshotted once
        at Start (same one-time-snapshot pattern and main-thread-only
        constraint as get_active_query_specs; the Devices tree stays
        frozen for the whole run, so nothing here could change even if
        re-polled). Used by _run_while_loop's pre-poll check: a while's
        exit-condition query must be on an ACTIVE device, or the row
        fails immediately (a query on an off device would just fail
        every poll until the timeout — misleading). query_specs can't
        answer this: a device can be on with no active queries.
        Returns an empty set if no device list is loaded."""
        names = set()
        if self.current_devfile is None:
            return names
        dev_item, dev_cookie = self.tree.GetFirstChild(self.root)
        while dev_item.IsOk():
            dev_data = self.tree.GetItemData(dev_item)
            if dev_data and dev_data.get('kind') == 'device' and dev_data.get('on'):
                names.add(dev_data['name'])
            dev_item, dev_cookie = self.tree.GetNextChild(self.root, dev_cookie)
        return names


# =========================================================================
# Device Editor pane — add/configure a device + VISA address
# =========================================================================

# Same constants as v1's sub_devicemanager.py — command-alias editing
# (CommandEditor's half) is a deliberate follow-up, not in this pass.
TERM_CHARS  = ('', '\r', '\n', '\r\n', '\n\r')
TERM_LABELS = ('none', r'\r', r'\n', r'\r\n', r'\n\r')
BAUD_RATES  = ['2400', '4800', '9600', '14400', '19200', '28800', '38400', '57600', '115200']
DATA_BITS   = ['5', '6', '7', '8', '9']
_TERM_MAP   = {1: '\r', 2: '\n', 3: '\r\n', 4: '\n\r'}


def _collect_device_names(devfile_path) -> list:
    """Shared by DevicesPanel (rename/delete) and DeviceEditorPanel
    (duplicate-alias check) — one .dev file reader, not two copies that
    could drift apart."""
    try:
        return [ln.strip() for ln in devfile_path.read_text().splitlines() if ln.strip()]
    except Exception:
        return []


def _is_serial_addr(addr: str) -> bool:
    return any(k in addr for k in ('ASRL', 'COM', 'com'))


class TCPIPAddressDialog(wx.Dialog):
    """Structured TCPIP entry — the two variants need genuinely different
    syntax and mixing them up by hand-typing is an easy, costly mistake:
    TCPIP...INSTR (standard VISA, VXI-11/hislip) never takes a port —
    it's negotiated by the protocol. TCPIP...SOCKET (raw TCP) always
    needs one, and it's instrument-specific (not a VISA convention).
    This builds the exact string instead of asking the user to remember
    the double-colon syntax."""

    def __init__(self, parent):
        super().__init__(parent, title='Add TCPIP Device', size=wx.Size(380, 230))

        self.ip_ctrl = wx.TextCtrl(self, value='192.168.1.')
        self.mode_instr  = wx.RadioButton(self, label='Standard VISA (TCPIP::INSTR, no port)',
                                          style=wx.RB_GROUP)
        self.mode_socket = wx.RadioButton(self, label='Raw socket (TCPIP::SOCKET, port required)')
        self.mode_socket.SetValue(True)   # SOCKET is the common case in this lab's device files
        self.port_ctrl = wx.TextCtrl(self, value='5025')
        self.mode_instr.Bind(wx.EVT_RADIOBUTTON, self._on_mode_change)
        self.mode_socket.Bind(wx.EVT_RADIOBUTTON, self._on_mode_change)

        form = wx.FlexGridSizer(0, 2, PAD_SMALL, PAD_SMALL)
        form.AddGrowableCol(1)
        form.Add(wx.StaticText(self, label='IP address:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.ip_ctrl, 1, wx.EXPAND)
        form.Add(wx.StaticText(self, label='Port:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.port_ctrl, 1, wx.EXPAND)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(form, 0, wx.EXPAND | wx.ALL, PAD_LARGE)
        main.Add(self.mode_instr, 0, wx.LEFT | wx.RIGHT, PAD_LARGE)
        main.Add(self.mode_socket, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, PAD_LARGE)
        main.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, PAD_SMALL)
        self.SetSizerAndFit(main)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

        self._on_mode_change(None)

    def _on_mode_change(self, event):
        self.port_ctrl.Enable(self.mode_socket.GetValue())

    def _on_ok(self, event):
        ip = self.ip_ctrl.GetValue().strip()
        errors = []
        if not ip:
            errors.append('IP address cannot be empty.')
        if self.mode_socket.GetValue():
            try:
                port = int(self.port_ctrl.GetValue().strip())
                if not (0 < port < 65536):
                    errors.append('Port must be between 1 and 65535.')
            except ValueError:
                errors.append('Port must be a whole number.')
        if errors:
            wx.MessageBox('\n'.join(errors), 'Cannot continue', wx.OK | wx.ICON_WARNING)
            return
        event.Skip()   # validation passed — let the dialog actually close with ID_OK

    def get_address(self) -> str:
        ip = self.ip_ctrl.GetValue().strip()
        if self.mode_socket.GetValue():
            port = self.port_ctrl.GetValue().strip()
            return f'TCPIP0::{ip}::{port}::SOCKET'
        return f'TCPIP0::{ip}::INSTR'


class HTTPAddressDialog(wx.Dialog):
    """Structured entry for an HTTP device — an instrument whose remote
    interface is an HTTP server (e.g. a JSON-RPC API like the Kiutra
    cryostat's) rather than a VISA resource or raw TCP socket. Builds
    the pseudo-VISA address 'HTTP::host::port::/path::content-type'
    that _visa_write/_visa_query dispatch on (see _is_http_addr): each
    write/query POSTs the command string as the request body and takes
    the response body as the raw response. The command strings
    themselves (the protocol dialect — JSON-RPC, REST, whatever) live
    in the Command Editor exactly like SCPI strings do for every other
    device; nothing protocol-specific is configured here beyond the
    Content-Type header."""

    def __init__(self, parent):
        super().__init__(parent, title='Add HTTP Device', size=wx.Size(430, 260))

        self.host_ctrl = wx.TextCtrl(self, value='192.168.1.')
        self.port_ctrl = wx.TextCtrl(self, value='80')
        self.path_ctrl = wx.TextCtrl(self, value='/')
        self.ctype_ctrl = wx.TextCtrl(self, value=_HTTP_DEFAULT_CONTENT_TYPE)

        form = wx.FlexGridSizer(0, 2, PAD_SMALL, PAD_SMALL)
        form.AddGrowableCol(1)
        form.Add(wx.StaticText(self, label='Host / IP address:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.host_ctrl, 1, wx.EXPAND)
        form.Add(wx.StaticText(self, label='Port:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.port_ctrl, 1, wx.EXPAND)
        form.Add(wx.StaticText(self, label='Path:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.path_ctrl, 1, wx.EXPAND)
        form.Add(wx.StaticText(self, label='Content-Type:'), 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(self.ctype_ctrl, 1, wx.EXPAND)

        hint = wx.StaticText(self, label='Example — Kiutra JSON-RPC: port 1006, '
                                         'path /, Content-Type application/json-rpc')
        hint.SetForegroundColour(COLOR_TEXT_MUTED)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(form, 0, wx.EXPAND | wx.ALL, PAD_LARGE)
        main.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, PAD_LARGE)
        main.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, PAD_SMALL)
        self.SetSizerAndFit(main)
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)

    def _on_ok(self, event):
        errors = []
        if not self.host_ctrl.GetValue().strip():
            errors.append('Host cannot be empty.')
        try:
            port = int(self.port_ctrl.GetValue().strip())
            if not (0 < port < 65536):
                errors.append('Port must be between 1 and 65535.')
        except ValueError:
            errors.append('Port must be a whole number.')
        # '::' inside path or content-type would corrupt the '::'-
        # separated address string this dialog builds.
        if '::' in self.path_ctrl.GetValue() or '::' in self.ctype_ctrl.GetValue():
            errors.append("Path and Content-Type must not contain '::'.")
        if errors:
            wx.MessageBox('\n'.join(errors), 'Cannot continue', wx.OK | wx.ICON_WARNING)
            return
        event.Skip()   # validation passed — let the dialog close with ID_OK

    def get_address(self) -> str:
        host = self.host_ctrl.GetValue().strip()
        port = self.port_ctrl.GetValue().strip()
        path = self.path_ctrl.GetValue().strip() or '/'
        if not path.startswith('/'):
            path = '/' + path
        ctype = self.ctype_ctrl.GetValue().strip() or _HTTP_DEFAULT_CONTENT_TYPE
        return f'HTTP::{host}::{port}::{path}::{ctype}'


STANDARD_DEVICE_NAME = 'control'
# Exact field values ported from v1's _base_commands(): userprompt, stop,
# pause, counter. ('simulate' was briefly added here too — wrong: the request
# meant the Simulate toolbar button, not a control-flow command. Removed.)

SCRIPT_DEVICE_NAME = 'script'
# A second virtual device, unlike 'control' in almost every respect except
# being non-VISA. Its 'commands' are not stored anywhere (no
# script_commands.txt) — they're every .py file in custom_dir(), read live
# each time the tree is built (see load_devfile). Off by default (requirement:
# 'It is turned off by default') — unlike 'control', which can never be
# toggled off at all, 'script' is an ordinary toggleable device, just one
# whose absence-of-prior-state default is False instead of the usual True.
# Injected into every loaded device list whether or not it's literally a
# line in the .dev file (see load_devfile) — this is what makes it appear
# in .dev files that existed before this feature did, without needing to
# migrate them.
_STANDARD_COMMANDS = [
    'True|userprompt|wf|0|0|-1=0|0|free|free|free|free|free|free',
    'True|stop|wf|0|0|-1=0|0|free|free|free|free|free|free',
    'True|pause|wf|1|[%]|-1=0|0|free|free|free|free|free|free',
    'True|counter|wf|2|[%]|-1=12|0|free|-2=6|free|free|free|free',
]
# Derived, not a second hand-typed list — the two can never drift
# apart. Used ONLY to gray out Rename/Delete on these 4 in the tree
# (_on_tree_right_click); run()'s own dispatch still matches these
# names literally and is untouched by this set's existence.
_RESERVED_CONTROL_COMMAND_NAMES = frozenset(ln.split('|')[1] for ln in _STANDARD_COMMANDS)


_EXAMPLE_SCRIPT = """\
# example.py — a qmeas script-device example.
#
# Scripts are plain Python files in the custom/ folder next to
# qmeas.py; each file appears as a command under the 'script' device
# and runs via exec() when its task row executes. Nothing is injected:
# use ordinary Python. Output goes to the console qmeas was started
# from. A script that raises aborts the run (with magnet onhold).

print("Hello from the qmeas example script!")
"""


def _create_device_list(name: str):
    """Create a new device list '<name>.dev' pre-seeded with the
    virtual 'control' device (all 4 standard commands) and the
    'script' device (plus custom/example.py, created once globally if
    no scripts exist yet). Returns (devfile_path, None) on success or
    (None, user-facing error string). NEVER overwrites: an existing
    list of that name is an error — the Device Editor's old handler
    silently clobbered existing lists, which this replaces."""
    name = (name or '').strip()
    if not name:
        return None, 'Enter a name for the new device list.'
    if not re.fullmatch(r'[A-Za-z0-9_\-]+', name):
        return None, ('Device list names may contain letters, digits, '
                      'underscore and dash only.')
    devfile = dev_dir() / f'{name}.dev'
    stem = devfile.with_suffix('')
    if devfile.exists() or stem.exists():
        return None, (f"A device list called '{name}' already exists — "
                      f"choose a different name.")
    try:
        dev_dir().mkdir(parents=True, exist_ok=True)
        stem.mkdir(parents=True)
        devfile.write_text(f'{STANDARD_DEVICE_NAME}\n{SCRIPT_DEVICE_NAME}\n')
        _seed_standard_device(stem)
        custom = custom_dir()
        custom.mkdir(parents=True, exist_ok=True)
        if not any(custom.glob('*.py')):
            (custom / 'example.py').write_text(_EXAMPLE_SCRIPT)
    except Exception as e:
        return None, f'Could not create device list:\n{e}'
    return devfile, None


def _duplicate_device(devfile, name):
    """Duplicate device `name` inside the list `devfile`: copies its
    settings and commands files under a unique '<name>copy[N]' name and
    appends it to the .dev. SAFETY BY DESIGN: the copy's ADDRESS is
    BLANKED — two devices sharing one address is a silent-crosstalk
    hazard, so a forgotten reconfiguration must fail loudly (connection
    error) instead of talking to the original's instrument. Returns
    (newname, None) or (None, error_text)."""
    stem = devfile.with_suffix('')
    existing = set(_collect_device_names(devfile))
    candidate = f'{name}copy'
    n = 2
    while (candidate in existing
           or (stem / f'{candidate}.txt').exists()
           or (stem / f'{candidate}_commands.txt').exists()):
        candidate = f'{name}copy{n}'
        n += 1
    settings_path = stem / f'{name}.txt'
    try:
        lines = settings_path.read_text().splitlines()
    except OSError as e:
        return None, f'Could not read the device settings:\n{e}'
    if lines:
        lines[0] = ''   # blank the address (see docstring)
    try:
        (stem / f'{candidate}.txt').write_text('\n'.join(lines) + '\n')
        commands_path = stem / f'{name}_commands.txt'
        if commands_path.exists():
            (stem / f'{candidate}_commands.txt').write_text(
                commands_path.read_text())
        with devfile.open('a') as f:
            f.write(candidate + '\n')
    except OSError as e:
        return None, f'Could not write the duplicate:\n{e}'
    return candidate, None


def _seed_standard_device(stem):
    """Add the virtual 'control' device + its 4 standard commands to a
    newly-created device list. No {control}.txt settings file — it's
    virtual, no real VISA address to assign.

    All 4 now have real execution semantics — TaskRunnerThread.run()
    special-cases device==control/command in ('pause', 'userprompt',
    'stop', 'counter') before anything else in the row is even looked
    up: an abort-checked sleep for pause; a modal 'press Continue'
    popup for userprompt (TaskRunnerThread._show_userprompt /
    TasksPanel._show_userprompt_dialog — its own Abort option triggers
    a real abort via the exact same path a Stop-button click takes);
    an immediate real abort for stop (request_abort(), same onhold-on-
    abort handling as a manual Stop); a virtual timed sweep with real
    data-file output for counter (_run_timed_sweep). None of the 4 can
    be a nested-chain member (_run_nested_sweep rejects pause/
    userprompt/stop explicitly — no sweep values to loop over; counter
    can be, since it does have Start/Final/Steps)."""
    cmdfile = stem / f'{STANDARD_DEVICE_NAME}_commands.txt'
    cmdfile.write_text('\n'.join(_STANDARD_COMMANDS) + '\n')


class DeviceEditorPanel(wx.Panel):
    """Add a new device (discover VISA resources, assign one to a name,
    save to a .dev file + per-device settings/commands files), OR edit
    an existing one's settings in place (VISA address/termination/
    timeout/serial — not alias; renaming stays in the tree's own
    'Rename Device...'). Which mode is active is tracked by
    self.editing_device (None = adding new) — see load_existing_device,
    _on_new_device, and the _save_new_device/_save_existing_device
    split in _on_save_device. File schema and validation for the 'add
    new' path ported verbatim from v1's sub_devicemanager.py — .dev
    files and .txt device settings stay interchangeable between v1 and
    v2 this way.

    VISA scan is synchronous (blocking), matching v1 exactly — v1 does
    the same list_resources() + open_resource() test on the main thread
    with no threading, and evidently that's fine in practice (GPIB/USB/
    LAN scans are sub-second to a couple seconds for a handful of
    instruments). Not adding threading complexity for a problem v1
    itself doesn't bother solving.
    """

    def __init__(self, parent, on_device_saved=None):
        super().__init__(parent)
        self.SetBackgroundColour(COLOR_PANEL_BG)
        self.rm = None
        self.selected_addr = None
        self.on_device_saved = on_device_saved
        self.get_current_devfile = None   # wired by QMeasMain._build_panes
        # Wired by QMeasMain._build_panes to TasksPanel.run_active —
        # Save Device (re)writes .dev/settings files the running thread
        # reads from disk per row, so it's frozen during a run.
        self.is_run_active = None
        # None = adding a brand new device (original behavior).
        # (device_name, stem) = editing that EXISTING device's settings
        # in place — set by load_existing_device, cleared by
        # _on_new_device. See _on_save_device for the resulting branch.
        self.editing_device = None

        self.mode_label = wx.StaticText(self, label='Add a new device')
        self.mode_label.SetForegroundColour(COLOR_TEXT_MUTED)

        # --- VISA resource list ---
        self.list_visa = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_visa.InsertColumn(0, 'Discovered VISA resources', width=self.FromDIP(320))
        self.list_visa.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_assign_address)

        btn_scan   = wx.Button(self, label='Scan')
        btn_tcpip  = wx.Button(self, label='Add TCPIP...')
        btn_http   = wx.Button(self, label='Add HTTP...')
        btn_manual = wx.Button(self, label='Manual address...')
        btn_scan.Bind(wx.EVT_BUTTON, self._on_scan)
        btn_tcpip.Bind(wx.EVT_BUTTON, self._on_add_tcpip)
        btn_http.Bind(wx.EVT_BUTTON, self._on_add_http)
        btn_manual.Bind(wx.EVT_BUTTON, self._on_manual_address)
        scan_row = wx.BoxSizer(wx.HORIZONTAL)
        scan_row.Add(btn_scan, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        scan_row.Add(btn_tcpip, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        scan_row.Add(btn_http, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        scan_row.Add(btn_manual, 0)

        # --- Device identity ---
        self.alias_ctrl = wx.TextCtrl(self)
        _bind_name_filter(self.alias_ctrl)
        self.addr_label = wx.StaticText(self, label='[no VISA assigned]')

        # --- Termination / timeout ---
        self.write_term = wx.Choice(self, choices=list(TERM_LABELS))
        self.write_term.SetSelection(0)
        self.read_term = wx.Choice(self, choices=list(TERM_LABELS))
        self.read_term.SetSelection(0)
        self.timeout_ctrl = wx.TextCtrl(self, value='5000')

        # --- Serial-only fields (shown conditionally) ---
        self.serial_baud = wx.Choice(self, choices=BAUD_RATES)
        self.serial_baud.SetSelection(2)
        self.serial_bits = wx.Choice(self, choices=DATA_BITS)
        self.serial_bits.SetSelection(3)
        self.serial_widgets = [self.serial_baud, self.serial_bits]
        self._serial_labels = []

        # --- .dev file target ---
        self.devfile_choice = wx.Choice(self, choices=self._list_dev_files())
        self.btn_new_devfile = wx.Button(self, label='New list...')
        self.btn_new_devfile.Bind(wx.EVT_BUTTON, self._on_new_devfile)
        devfile_row = wx.BoxSizer(wx.HORIZONTAL)
        devfile_row.Add(self.devfile_choice, 1, wx.EXPAND | wx.RIGHT, self.FromDIP(PAD_SMALL))
        devfile_row.Add(self.btn_new_devfile, 0)

        btn_test = wx.Button(self, label='Test Connection')
        self.btn_save = wx.Button(self, label='Save Device')
        btn_new_device = wx.Button(self, label='New Device')
        btn_test.Bind(wx.EVT_BUTTON, self._on_test_connection)
        self.btn_save.Bind(wx.EVT_BUTTON, self._on_save_device)
        btn_new_device.Bind(wx.EVT_BUTTON, self._on_new_device)
        action_row = wx.BoxSizer(wx.HORIZONTAL)
        action_row.Add(btn_test, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        action_row.Add(self.btn_save, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        action_row.Add(btn_new_device, 0)

        form = wx.FlexGridSizer(0, 2, self.FromDIP(PAD_SMALL), self.FromDIP(PAD_SMALL))
        form.AddGrowableCol(1)

        def add_row(label_text, widget):
            lbl = wx.StaticText(self, label=label_text)
            form.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(widget, 1, wx.EXPAND)
            return lbl

        add_row('Device alias:', self.alias_ctrl)
        add_row('VISA address:', self.addr_label)
        add_row('Write termination:', self.write_term)
        add_row('Read termination:', self.read_term)
        add_row('Timeout (ms):', self.timeout_ctrl)
        self._serial_labels.append(add_row('Serial baud rate:', self.serial_baud))
        self._serial_labels.append(add_row('Serial data bits:', self.serial_bits))
        devfile_label = add_row('Device list (.dev) file:', devfile_row)
        # Row hidden by user request: the loaded list in Devices &
        # Commands IS the list devices get added to — a second list
        # selector here was redundant and confusing. Widgets kept in
        # code (list creation moved to the tree's New button).
        for w in (devfile_label, self.devfile_choice, self.btn_new_devfile):
            w.Hide()

        self._show_serial(False)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(self.list_visa, 1, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        main.Add(scan_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(PAD_SMALL))
        main.Add(self.mode_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(PAD_SMALL))
        main.Add(form, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        main.Add(action_row, 0, wx.ALL, self.FromDIP(PAD_SMALL))
        self.SetSizer(main)

        self._on_scan(None)

    # -----------------------------------------------------------------
    def _show_serial(self, show: bool):
        for w in self.serial_widgets + self._serial_labels:
            w.Show(show)
        self.Layout()

    def _list_dev_files(self):
        try:
            return sorted(p.name for p in dev_dir().glob('*.dev'))
        except Exception:
            return []

    # -----------------------------------------------------------------
    def _on_scan(self, event):
        """Ported from v1's UpdateConnectedVISADeviceList — synchronous,
        same as v1. Failed connection test -> red row, same convention."""
        self.list_visa.DeleteAllItems()
        self.rm = get_resource_manager()
        if self.rm is None:
            self.list_visa.InsertItem(0, 'NO CONNECTION AVAILABLE')
            return
        try:
            resources = self.rm.list_resources()
        except Exception:
            resources = ()
        for idx, addr in enumerate(resources):
            self.list_visa.InsertItem(idx, addr)
            try:
                dev = self.rm.open_resource(addr)
                dev.close()
            except Exception:
                self.list_visa.SetItemBackgroundColour(idx, wx.Colour(220, 100, 100))

    def _on_add_tcpip(self, event):
        with TCPIPAddressDialog(self) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            addr = dlg.get_address()
        idx = self.list_visa.GetItemCount()
        self.list_visa.InsertItem(idx, addr)
        self.list_visa.SetItemBackgroundColour(idx, wx.Colour(220, 100, 100))

    def _on_add_http(self, event):
        with HTTPAddressDialog(self) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            addr = dlg.get_address()
        idx = self.list_visa.GetItemCount()
        self.list_visa.InsertItem(idx, addr)
        self.list_visa.SetItemBackgroundColour(idx, wx.Colour(220, 100, 100))

    def _on_manual_address(self, event):
        with wx.TextEntryDialog(self, 'Enter full device address '
                                '(e.g. GPIB0::5::INSTR — for TCPIP, use "Add TCPIP..." instead)',
                                'Manual address entry') as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            raw = dlg.GetValue().strip()
        # VISA addresses are conventionally uppercased (v1 behavior,
        # kept). HTTP addresses are NOT — URL paths are case-sensitive
        # and 'application/json-rpc' should stay lowercase; only strip
        # whitespace for those.
        if raw.upper().startswith('HTTP::'):
            addr = raw
        else:
            addr = raw.replace(' ', '').upper()
        if not addr:
            return
        idx = self.list_visa.GetItemCount()
        self.list_visa.InsertItem(idx, addr)
        self.list_visa.SetItemBackgroundColour(idx, wx.Colour(220, 100, 100))

    def _on_assign_address(self, event):
        addr = self.list_visa.GetItemText(event.GetIndex())
        self.selected_addr = addr
        self.addr_label.SetLabel(addr)
        self._show_serial(_is_serial_addr(addr))

    # -----------------------------------------------------------------
    def _on_new_devfile(self, event):
        with wx.TextEntryDialog(self, 'New device list name (without .dev):',
                                'New device list') as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            name = dlg.GetValue()
        # Shared safe creator — duplicate names are now REJECTED here
        # too (this handler used to silently overwrite an existing
        # list's .dev, losing its device entries).
        devfile, error = _create_device_list(name)
        if error:
            wx.MessageBox(error, 'New device list', wx.OK | wx.ICON_ERROR)
            return
        self.devfile_choice.Set(self._list_dev_files())
        self.devfile_choice.SetStringSelection(devfile.name)

    # -----------------------------------------------------------------
    def load_existing_device(self, device_name: str, stem):
        """Populate the form from an existing device's {device_name}.txt
        and switch into 'edit existing' mode: Save overwrites that file
        in place instead of appending a new device to a .dev list (see
        _on_save_device). Wired from DevicesPanel — single-click
        (_on_tree_sel_changed) and double-click (_on_item_activated)
        both call this; double-click additionally reveals this pane.
        Renaming isn't offered here — the alias field is locked
        read-only in edit mode; use the tree's existing 'Rename
        Device...' for that, so there's exactly one way to rename and
        it can't be attempted through two half-finished paths."""
        try:
            addr, wterm_sel, rterm_sel, timeout, baud_sel, bits_sel = _read_device_settings(stem, device_name)
        except Exception as e:
            wx.MessageBox(f"Could not read settings for '{device_name}':\n{e}",
                          'Load failed', wx.OK | wx.ICON_ERROR)
            return
        self.editing_device = (device_name, stem)
        self.alias_ctrl.SetValue(device_name)
        self.alias_ctrl.SetEditable(False)
        self.addr_label.SetLabel(addr)
        self.selected_addr = addr
        self.write_term.SetSelection(wterm_sel)
        self.read_term.SetSelection(rterm_sel)
        self.timeout_ctrl.SetValue(str(int(timeout)) if float(timeout).is_integer() else str(timeout))
        self.serial_baud.SetSelection(baud_sel)
        self.serial_bits.SetSelection(bits_sel)
        self._show_serial(_is_serial_addr(addr))
        self.devfile_choice.Enable(False)
        self.btn_new_devfile.Enable(False)
        self.btn_save.SetLabel('Save Changes')
        self.mode_label.SetLabel(f"Editing existing device: '{device_name}'")
        self.Layout()

    def _on_new_device(self, event):
        """Exit 'edit existing device' mode (if in it) and return to a
        blank form for adding a brand new device — the only way back
        out, kept explicit rather than inferred from some other click,
        so switching modes is never a surprise."""
        self.editing_device = None
        self.alias_ctrl.SetValue('')
        self.alias_ctrl.SetEditable(True)
        self.addr_label.SetLabel('[no VISA assigned]')
        self.selected_addr = None
        self.timeout_ctrl.SetValue('5000')
        self.write_term.SetSelection(0)
        self.read_term.SetSelection(0)
        self._show_serial(False)
        self.devfile_choice.Enable(True)
        self.btn_new_devfile.Enable(True)
        self.btn_save.SetLabel('Save Device')
        self.mode_label.SetLabel('Add a new device')
        self.Layout()

    # -----------------------------------------------------------------
    def _on_save_device(self, event):
        if self.is_run_active is not None and self.is_run_active():
            wx.MessageBox('Cannot save device settings while a run is in progress — '
                          'the run reads these files from disk on every row.',
                          'Run in progress', wx.OK | wx.ICON_WARNING)
            return
        if self.editing_device is not None:
            self._save_existing_device()
        else:
            self._save_new_device()

    def _save_existing_device(self):
        """Overwrite {device_name}.txt for the device currently loaded
        via load_existing_device. No .dev list or alias changes here —
        just the VISA address/termination/timeout/serial settings."""
        device_name, stem = self.editing_device
        addr = self.addr_label.GetLabel().strip()
        timeout_str = self.timeout_ctrl.GetValue().strip()

        errors = []
        if addr == '[no VISA assigned]' or not addr:
            errors.append('No VISA address assigned.')
        try:
            tval = float(timeout_str)
            if tval < 100:
                errors.append('Timeout must be >= 100 ms (default 5000).')
        except Exception:
            errors.append('Timeout is not a valid number.')
        if errors:
            wx.MessageBox('\n'.join(errors), 'Cannot save - fix the following:',
                          wx.OK | wx.ICON_WARNING)
            return

        is_serial = _is_serial_addr(addr)
        lines = [
            addr,
            str(self.write_term.GetSelection()),
            str(self.read_term.GetSelection()),
            timeout_str,
            str(self.serial_baud.GetSelection()),
            str(self.serial_bits.GetSelection()),
        ]
        n_lines = 6 if is_serial else 4
        try:
            (stem / f'{device_name}.txt').write_text('\n'.join(lines[:n_lines]) + '\n')
        except Exception as e:
            wx.MessageBox(f'Could not save settings:\n{e}', 'File Access Error', wx.OK | wx.ICON_EXCLAMATION)
            return

        if self.on_device_saved is not None:
            self.on_device_saved(stem.with_suffix('.dev'))
        wx.MessageBox(f"Saved settings for '{device_name}'.", 'Device saved', wx.OK | wx.ICON_INFORMATION)

    def _save_new_device(self):
        """Validation and file-write sequence ported verbatim from v1's
        OnSaveVISAUpdates."""
        alias = self.alias_ctrl.GetValue().strip()
        addr = self.addr_label.GetLabel().strip()
        timeout_str = self.timeout_ctrl.GetValue().strip()
        # Target = the list currently LOADED in Devices & Commands —
        # the only list a user is looking at. The editor's own .dev
        # dropdown row is hidden (redundant second selector, confusing
        # by user report); get_current_devfile is wired in _build_panes.
        devfile = self.get_current_devfile() if self.get_current_devfile else None

        errors = []
        if not alias:
            errors.append('Device alias cannot be empty.')
        elif not _NAME_RE.match(alias):
            errors.append('Device alias may only contain lowercase letters (a-z) and digits (0-9).')
        if addr == '[no VISA assigned]' or not addr:
            errors.append('No VISA address assigned.')
        if devfile is None:
            errors.append('No device list loaded — load or create one (New) in '
                          'Devices & Commands first.')
        try:
            tval = float(timeout_str)
            if tval < 100:
                errors.append('Timeout must be >= 100 ms (default 5000).')
        except Exception:
            errors.append('Timeout is not a valid number.')

        if devfile is not None:
            existing = _collect_device_names(devfile)
            if alias in existing or alias in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME):
                # The reserved-name check is separate from the file-based
                # one: 'script' is injected into every loaded list (see
                # load_devfile) rather than necessarily being a literal
                # line in an older .dev file yet, so relying on `existing`
                # alone wouldn't catch a real device someone tries to name
                # 'script' on a list created before this feature existed.
                errors.append('Device alias already in use in this list.')

        if errors:
            wx.MessageBox('\n'.join(errors), 'Cannot save - fix the following:',
                          wx.OK | wx.ICON_WARNING)
            return

        stem = devfile.with_suffix('')
        ok = True
        try:
            stem.mkdir(parents=True, exist_ok=True)
            with open(devfile, 'a') as f:
                f.write(f'{alias}\n')
        except Exception:
            ok = False
        for suffix in ('.txt', '_commands.txt'):
            try:
                (stem / f'{alias}{suffix}').touch()
            except Exception:
                ok = False

        is_serial = _is_serial_addr(addr)
        lines = [
            addr,
            str(self.write_term.GetSelection()),
            str(self.read_term.GetSelection()),
            timeout_str,
            str(self.serial_baud.GetSelection()),
            str(self.serial_bits.GetSelection()),
        ]
        n_lines = 6 if is_serial else 4
        try:
            (stem / f'{alias}.txt').write_text('\n'.join(lines[:n_lines]) + '\n')
        except Exception:
            ok = False

        if not ok:
            wx.MessageBox('One or more files could not be written.\n'
                          'Device list may be inconsistent.',
                          'File Access Error', wx.OK | wx.ICON_EXCLAMATION)
            return

        if self.on_device_saved is not None:
            self.on_device_saved(devfile)   # reloads the Devices tree directly — no manual step
        wx.MessageBox(f"Saved '{alias}' to {devfile.name}.", 'Device saved', wx.OK | wx.ICON_INFORMATION)
        self.alias_ctrl.SetValue('')
        self.addr_label.SetLabel('[no VISA assigned]')
        self.selected_addr = None
        self._show_serial(False)

    # -----------------------------------------------------------------
    def _on_test_connection(self, event):
        """Ported verbatim from v1's OnTestVISAConnection."""
        addr = self.addr_label.GetLabel().strip()
        if addr == '[no VISA assigned]' or not addr:
            wx.MessageBox('Assign an address first.', 'Not possible', wx.OK | wx.ICON_WARNING)
            return

        if _is_http_addr(addr):
            # An HTTP device has no *IDN? — there is no universal
            # protocol-level ping for an arbitrary HTTP API. Test plain
            # TCP reachability of host:port instead; use Query Once on
            # a real command (Command Editor) to test the API itself.
            try:
                host, port, _path, _ctype = _parse_http_addr(addr)
            except Exception:
                wx.MessageBox('Cannot parse HTTP address.', 'Error', wx.OK)
                return
            try:
                s = socket.create_connection((host, port), timeout=5)
                s.close()
                wx.MessageBox(f'TCP connection to {host}:{port} OK.\n\n'
                              'No protocol-level check is performed for HTTP '
                              'devices — use Query Once on a command to test '
                              'the API itself.', 'Connection OK', wx.OK)
            except Exception as e:
                wx.MessageBox(f'Could not open a connection to {host}:{port}.\n\n{e}',
                              'Error', wx.OK)
            return

        if wx.MessageBox('Press OK to query *IDN?.\n\n'
                         'If no connection can be established, the app may freeze briefly.',
                         'Test connection', wx.OK | wx.CANCEL) != wx.OK:
            return

        wterm_sel = self.write_term.GetSelection()

        if '::SOCKET' in addr:
            parts = addr.split('::')
            try:
                host, port = parts[1], int(parts[2])
            except Exception:
                wx.MessageBox('Cannot parse TCPIP address.', 'Error', wx.OK)
                return
            wterm = _TERM_MAP.get(wterm_sel, '\n')
            cmd = f'*IDN?{wterm}'.encode('utf-8')
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(cmd)
                identifier_txt = s.recv(1024).decode('ascii', errors='replace').strip()
                s.close()
                error = 0
            except Exception:
                error = 1
                identifier_txt = ''
        else:
            if self.rm is None:
                wx.MessageBox('No VISA resource manager available.', 'Error', wx.OK)
                return
            try:
                dev = self.rm.open_resource(addr)
            except Exception:
                wx.MessageBox('Could not open connection.', 'Error', wx.OK)
                return
            try:
                dev.timeout = int(float(self.timeout_ctrl.GetValue()))
            except Exception:
                dev.timeout = 5000
            wterm_str = _TERM_MAP.get(wterm_sel, '')
            rterm_str = _TERM_MAP.get(self.read_term.GetSelection(), '')
            if wterm_str:
                try: dev.write_termination = wterm_str
                except Exception: pass
            if rterm_str:
                try: dev.read_termination = rterm_str
                except Exception: pass
            if _is_serial_addr(addr):
                try: dev.baud_rate = int(BAUD_RATES[self.serial_baud.GetSelection()])
                except Exception: pass
                try: dev.data_bits = int(DATA_BITS[self.serial_bits.GetSelection()])
                except Exception: pass
                try: dev.flow_control = False
                except Exception: pass
            try:
                identifier_txt = dev.query('*IDN?')
                error = 0
            except Exception:
                error = 2
                identifier_txt = ''
            finally:
                try: dev.close()
                except Exception: pass

        if error == 0:
            wx.MessageBox(f'{identifier_txt}\n\nConnection OK.', 'Device response received!', wx.OK)
        elif error == 1:
            wx.MessageBox('Could not open a connection.', 'Error', wx.OK)
        else:
            wx.MessageBox('No *IDN? response (timeout).\n\n'
                          'A failed IDN does not necessarily prevent communication.',
                          'Error', wx.OK)


# =========================================================================
# Tasks pane
# =========================================================================

class TasksPanel(wx.Panel):
    """Task grid. Column schema, adapted from v1's sub_grid.py (MyGrid):
    On | [hidden: structure] | Command | Constant/Start | Final | Steps |
    Integration Time | Conditions | Comments (rightmost, always).

    v1's separate 'Dependencies' text column (none/linked/loop/endloop)
    is gone. Replaced with: a hidden per-row structure value
    ('depth:linked') plus Python-style visuals on the Command column —
    nesting depth as indentation + vertical guide lines (CommandCellRenderer),
    linked rows get a black box outline around the cell. No explicit
    'nested' flag: nesting is just depth > 0, set via Indent/Outdent in
    the row context menu. 'Linked' is an independent toggle, since it
    describes where a row's value comes from, not its loop position — a
    row can be both nested and linked.

    Indent/Outdent are independent per-row edits — touching one row does
    NOT affect any other row. Indenting sets a row's depth to exactly
    (depth of the row above) + 1; a staircase (e.g. depth 1, 2, 3 down
    consecutive rows) results from indenting each of those rows
    yourself, one at a time, not from any automatic cascade. Outdenting
    just decrements a row's own depth by 1 (floor 0).

    Deferred from v1, not yet ported: Conditions is set via a popup
    dialog in v1 (sub_conditionals.py) — here it's plain editable text,
    no popup. The loop/linked-aware validation in v1's onEnterValuesCheck
    (red/white cell colouring, dependency-based value clearing) isn't
    ported either — TaskRunnerThread only executes flat constant-value
    rows so far (see _on_start_stop), so there's no loop/link execution
    path yet for this validation to check against.

    'On' column: v1 used a native checkbox (GridCellBoolRenderer). Here
    it's the same LED convention as the Devices tree (LedCellRenderer),
    click-to-toggle, per your request.

    Row reordering: ported from v1's proven working mechanism (same file,
    same comment: "no CaptureMouse — avoids wx stack assertion errors").
    v1 already hit and solved the exact CaptureMouse assertion this file
    ran into — click-hold-drag-release on a row label, motion tracked
    with a pixel threshold before a drag is considered "active" (avoids
    misfiring on plain clicks), a small red/accent drop-indicator line
    shown at the target position, release commits the move. No
    CaptureMouse, no native OS drag-and-drop anywhere in this mechanism."""

    COL_ON, COL_STRUCT, COL_CMD, COL_START, COL_FINAL, COL_STEPS, COL_INTTIME, \
        COL_COND, COL_COMMENT, COL_COMPLETED = range(10)

    INDENT_PX = 16   # per nesting level, Python-style
    MAX_DEPTH = 4
    MAX_LINK_CHAIN = 4   # total rows in one linked box, anchor included

    _DRAG_THRESHOLD = 8

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(COLOR_PANEL_BG)

        self.grid = wx.grid.Grid(self)
        self.grid.CreateGrid(0, 10)
        self.grid.SetRowLabelSize(self.FromDIP(30))
        self.grid.SetColLabelSize(self.FromDIP(20))
        self.grid.DisableDragRowSize()
        self.grid.SetSelectionMode(wx.grid.Grid.GridSelectRows)

        col_setup = [
            (self.COL_ON,      'On',               self.FromDIP(30)),
            (self.COL_STRUCT,  '',                  0),   # hidden: 'depth:linked' per row
            (self.COL_CMD,     'Command',           self.FromDIP(180)),
            (self.COL_START,   'Constant/Start',    self.FromDIP(100)),
            (self.COL_FINAL,   'Final',             self.FromDIP(90)),
            (self.COL_STEPS,   'Steps',             self.FromDIP(80)),
            (self.COL_INTTIME, 'Integration Time',  self.FromDIP(110)),
            (self.COL_COND,    '',                  self.FromDIP(5)),   # header blanked on request; column/data untouched. Width reduced to 5px on request (not hidden — that distinction was explicitly requested)
            (self.COL_COMMENT, 'Comments',          self.FromDIP(220)),
            (self.COL_COMPLETED, '',                0),   # hidden: '1' once executed successfully
        ]
        for col, label, width in col_setup:
            self.grid.SetColLabelValue(col, label)
            self.grid.SetColSize(col, width)

        attr_on = wx.grid.GridCellAttr()
        attr_on.SetRenderer(LedCellRenderer())
        attr_on.SetReadOnly(True)   # toggled by click handler, not text edit
        self.grid.SetColAttr(self.COL_ON, attr_on)

        attr_cmd = wx.grid.GridCellAttr()
        attr_cmd.SetReadOnly(True)   # populated only via double-click from tree
        attr_cmd.SetRenderer(CommandCellRenderer(self.COL_STRUCT, self.INDENT_PX))
        self.grid.SetColAttr(self.COL_CMD, attr_cmd)

        attr_cond = wx.grid.GridCellAttr()
        attr_cond.SetReadOnly(True)   # unused for now (header blanked too) — placeholder column, not a free-text field
        self.grid.SetColAttr(self.COL_COND, attr_cond)

        self._running = False
        # Wired externally by QMeasMain._build_panes, after DevicesPanel
        # and LogPanel both exist. None until then — _on_start_stop
        # checks and refuses cleanly rather than crashing if somehow
        # invoked before wiring.
        self.get_device_stem = None
        self.get_query_specs = None   # DevicesPanel.get_active_query_specs, once wired
        self.get_onhold_devices = None   # DevicesPanel.get_active_onhold_devices, once wired
        self.get_active_devices = None   # DevicesPanel.get_active_device_names, once wired
        self.on_log = None
        self._device_states = {}   # device_name -> bool, from on_device_toggled
        self._thread = None        # TaskRunnerThread, while Start is running
        self._run_frontier = -1    # highest row index claimed by the live scan this run; see _row_frozen
        self.last_sweep_data = None   # {'headers','matrix','filepath'} after a counter/sweep row runs — see _on_sweep_done
        self.on_sweep_data_ready = None   # wired to GraphWindow.refresh_data by QMeasMain, if that window exists
        self._active_row = None    # row currently being executed, if any
        self._row_start_readonly_state = {}   # row -> {col: was_readonly} — keyed by row (not flat)
                                               # so multiple rows can be "active" at once for a
                                               # nested chain without clobbering each other's state
        self._passed_over_rows = []           # (row, prior_readonly_state) pairs, reverted in _on_run_finished

        toolbar = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_start = wx.Button(self, label='Start')
        self.btn_pause = wx.Button(self, label='Pause')
        self.btn_pause.Show(False)
        # This gauge is shared between two different modes: pause uses
        # it as an indeterminate pulse (_pause_ui_start/_on_gauge_pulse
        # — genuinely no way to know how long a pause or a plain write
        # will take), while a sweep (single-level or nested — see
        # TaskRunnerThread._sweep_ui_start/_sweep_ui_progress) drives it
        # as a real determinate progress bar, since the total number of
        # measurements is known up front. Embedded, not a popup: an
        # inline gauge has no close affordance at all, which is a more
        # robust way to prevent the user closing it mid-run than putting
        # it in a separate window and vetoing EVT_CLOSE.
        self.gauge = wx.Gauge(self, range=100, size=self.FromDIP(wx.Size(120, -1)))
        self.gauge.Show(False)
        self._gauge_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_gauge_pulse, self._gauge_timer)

        # Elapsed time (count UP), not a countdown — a real ETA would
        # need to compound every level's own Integration Time across
        # however many measurements remain (loop order itself is now
        # resolved — see TaskRunnerThread._run_nested_sweep — but
        # nothing computes a time estimate from it yet). Faking a
        # countdown with no basis would just be a wrong number dressed
        # up as a real one. _elapsed_accum + _segment_start let pause/
        # resume stop and restart the clock without losing the
        # accumulated total.
        self.time_label = wx.StaticText(self, label='00:00:00')
        self._elapsed_accum = 0.0
        self._segment_start = None
        self._time_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_time_tick, self._time_timer)
        self.time_label.Show(False)

        # requirement: 'When a device is set to ramping and I start the task
        # list, it appears as if qmeas is frozen because there is no
        # progress. Please add a static text underneath the stop
        # button "Device X is currently ramping to a setpoint. Please
        # wait!"' — a plain constant-value ramp (Steps=0) has NO sweep
        # progress gauge at all otherwise, so this is the ONLY visual
        # feedback that anything is happening. Also covers verify
        # (TaskRunnerThread._verify_equals) — same 'looks frozen'
        # problem, arguably worse there since a verify wait can span
        # minutes. Bold/colored so it's not mistaken for routine status
        # text — the whole point is to be impossible to miss.
        self.wait_status_label = wx.StaticText(self, label='')
        self.wait_status_label.SetForegroundColour(COLOR_ACCENT)
        font = self.wait_status_label.GetFont()
        font.MakeBold()
        self.wait_status_label.SetFont(font)
        self.wait_status_label.Show(False)

        # requirement: 'just by looking at the devices, I wouldnt be able to
        # tell where I am... add a bracket that shows (x1[current
        # step/max steps], x2[current step/max step]...etc)' — one
        # entry per level of whatever's currently running (a nested
        # chain's every level, or the single row for an ordinary sweep/
        # counter), updated on every step, not just when a measurement
        # is recorded — so it moves the instant a level's OWN step
        # advances even while a deeper level (or a slow verify wait)
        # hasn't caught up yet. On its own row, not squeezed into the
        # toolbar next to Load/Save/LaLM/Simulate — a 3+ level chain
        # with real device aliases gets long.
        self.step_progress_label = wx.StaticText(self, label='')
        self.step_progress_label.Show(False)

        self.Bind(wx.EVT_WINDOW_DESTROY, self._on_destroy)
        self._last_insert_count = 1   # remembered between Insert prompts
        self.btn_load     = wx.Button(self, label='Load')
        self.btn_save     = wx.Button(self, label='Save')
        self.btn_lalm     = wx.Button(self, label='LaLM')
        self.btn_lalm.Disable()
        self.btn_lalm.Hide()   # removed from the UI for now (button + handler kept in
                               # code); a different AI-assistant approach is planned —
                                  # not removed, just disabled, so it's ready to re-enable when
                                  # that feature is actually being built
        self.btn_simulate = wx.Button(self, label='Simulate')
        self.btn_clear    = wx.Button(self, label='Clear')
        for btn in (self.btn_start, self.btn_pause, self.gauge, self.time_label):
            toolbar.Add(btn, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, self.FromDIP(PAD_SMALL))
        toolbar.AddStretchSpacer()
        for btn in (self.btn_load, self.btn_save, self.btn_clear, self.btn_lalm, self.btn_simulate):
            toolbar.Add(btn, 0, wx.ALL, self.FromDIP(PAD_SMALL))

        self.btn_start.Bind(wx.EVT_BUTTON, self._on_start_stop)
        self.btn_pause.Bind(wx.EVT_BUTTON, self._on_pause)
        self.btn_load.Bind(wx.EVT_BUTTON, self._on_load)
        self.btn_save.Bind(wx.EVT_BUTTON, self._on_save)
        self.btn_clear.Bind(wx.EVT_BUTTON, self._on_clear)
        self.btn_lalm.Bind(wx.EVT_BUTTON, self._on_lalm)
        self.btn_simulate.Bind(wx.EVT_BUTTON, self._on_simulate)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(toolbar, 0, wx.EXPAND)
        sizer.Add(self.wait_status_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(PAD_SMALL))
        sizer.Add(self.step_progress_label, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(PAD_SMALL))
        sizer.Add(self.grid, 1, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        self.SetSizer(sizer)

        self.grid.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self._on_cell_left_click)
        self.grid.Bind(wx.grid.EVT_GRID_EDITOR_CREATED, self._on_editor_created)
        self.grid.Bind(wx.grid.EVT_GRID_EDITOR_SHOWN, self._on_editor_shown)
        self.grid.Bind(wx.grid.EVT_GRID_CELL_CHANGED, self._on_cell_changed)

        # --- drag-drop state (ported from v1, see class docstring) ---
        self._drag_row      = -1
        self._drag_rows     = []   # full set being moved, once drag is active
        self._drag_start_y  = -1
        self._drag_active   = False
        self._drop_indicator = _DropIndicator(self.grid.GetGridWindow())

        row_label_win = self.grid.GetGridRowLabelWindow()
        row_label_win.Bind(wx.EVT_LEFT_DOWN,   self._on_label_left_down)
        row_label_win.Bind(wx.EVT_LEFT_UP,     self._on_label_left_up)
        row_label_win.Bind(wx.EVT_MOTION,      self._on_label_motion)
        row_label_win.Bind(wx.EVT_LEAVE_WINDOW, self._on_label_leave)
        self.grid.Bind(wx.grid.EVT_GRID_LABEL_RIGHT_CLICK, self._on_label_right_click)

        # An empty grid has no row to right-click (Insert Above/Below
        # needs an existing row as the anchor) and no row to select for
        # the fill-into-empty-row mechanism — an unresolvable bootstrap
        # gap on first launch. Seed one blank row so there's always
        # somewhere to start from.
        self._insert_blank_row(0)

    def _insert_blank_row(self, index: int):
        """Insert one row and blank it. Shared by __init__ (seed row)
        and the delete-to-zero guard."""
        self.grid.InsertRows(index, 1)
        self._blankify_row(index)

    def _ensure_trailing_blank(self):
        """Keep the LAST row always empty (the Excel phantom-row
        pattern): with sticky wx.grid row selection there is otherwise
        no way to get back to 'append at the bottom' once any row is
        selected — selecting the trailing blank IS the append gesture
        (the fill-in-place path lands the command there). Returns the
        trailing blank's index, creating it if the last row has a
        command."""
        n = self.grid.GetNumberRows()
        if n > 0 and self.grid.GetCellValue(n - 1, self.COL_CMD) == '':
            return n - 1
        self._insert_blank_row(n)
        return n

    def _blankify_row(self, index: int):
        """Just the cell setup for 'this is a blank task row' — no
        InsertRows, so Insert Above/Below (which inserts N rows in one
        call) can reuse this per-row without double-inserting."""
        self.grid.SetCellValue(index, self.COL_ON, '0')
        self.grid.SetReadOnly(index, self.COL_ON, True)
        self.grid.SetCellValue(index, self.COL_STRUCT, '0:')
        self.grid.SetReadOnly(index, self.COL_CMD, True)
        self.grid.SetCellValue(index, self.COL_COMPLETED, '0')

    # -----------------------------------------------------------------
    # Run freeze — frontier-based, not blanket. While TaskRunnerThread
    # is alive, rows AT OR ABOVE self._run_frontier (the highest row
    # index the scan has claimed so far — see _fetch_row_state_for_
    # thread) are a no-touch zone: no cell edits, no LED toggle, no
    # drag/drop as source or destination, no context menu. Rows BELOW
    # the frontier (not yet reached) stay fully live on purpose — requirement:
    # editable, draggable, and new tasks addable, so you can queue up
    # or correct upcoming work while a run is in progress. This is
    # genuinely live, not cosmetic: TaskRunnerThread no longer works
    # from a pre-built snapshot — it fetches each row fresh, right
    # before using it (see TaskRunnerThread._fetch_row and
    # _fetch_row_state_for_thread below), so an edit made before the
    # scan reaches that row actually changes what gets sent.
    #
    # Rows at/above the frontier remain frozen for the same reason as
    # before: the thread addresses the CURRENT row by index at the
    # moment it fetches it, so moving/deleting/inserting something at
    # or above that point — after it's already been claimed — would
    # still corrupt which grid row the thread's next UI callback
    # (highlight/grey/mark-completed) lands on. Below the frontier this
    # problem doesn't arise: nothing has been claimed there yet, and
    # the live fetch means "whatever occupies position N when the scan
    # gets to N" is correct by construction, however it got there.
    #
    # The Devices tree and Device Editor are UNCHANGED by this — still
    # fully frozen for the whole run via is_run_active, not frontier-
    # scoped. That's a different concern (the thread reads device/
    # command files from disk on every row; live-editing comms
    # parameters mid-run is a hazard, not a queueing convenience) and
    # no request to relax it.
    # -----------------------------------------------------------------
    def run_active(self) -> bool:
        """True while a run is in progress — the single source of truth
        used by this panel and (via is_run_active wiring) by
        DevicesPanel and DeviceEditorPanel, which stay fully frozen for
        the whole run regardless of frontier."""
        return self._thread is not None and self._thread.is_alive()

    def _row_frozen(self, row: int) -> bool:
        """True if `row` is at or above the frontier during a run —
        i.e. already claimed by the scan (executing, done, or passed
        over) and therefore off-limits to any grid mutation. False for
        every row below the frontier, and always False when no run is
        active."""
        return self.run_active() and row <= self._run_frontier

    def _evaluate_row_eligibility(self, row: int) -> dict:
        """Single source of truth for 'is this row eligible to run, and
        with what values' — reads the grid live. Used both by the Start
        preflight check (below) and, via _fetch_row_state_for_thread,
        by TaskRunnerThread's live per-row fetch during an actual run.
        Main-thread only.

        Eligibility: active, not linked. depth>0 (nested) rows are now
        eligible too — see TaskRunnerThread._run_nested_sweep, which
        recursively executes a chain of rows at strictly increasing
        depth (row R's child is the row directly below it IF that row
        is one level deeper — see _has_child) as genuine nested for-
        loops, one iteration of the deeper level per iteration of the
        shallower one. 'linked' rows are still not INDEPENDENTLY
        eligible — they execute as followers of their box's anchor row
        (see TaskRunnerThread._gather_link_box / the follower handling
        in _run_timed_sweep), never standalone; reached directly by the
        scan they're passed over like an orphaned nested row.

        has_child is included so run()'s dispatch can tell a standalone
        row (execute it alone, unchanged single-level path) from a
        chain root (gather the whole descending-depth chain first, then
        hand it to _run_nested_sweep) without a second grid read."""
        alias = self.grid.GetCellValue(row, self.COL_CMD)
        is_on = self.grid.GetCellValue(row, self.COL_ON) == '1'
        depth, linked = self._struct_get(row)

        # 'linked' and 'has_follower' are included even in the
        # ineligible returns: TaskRunnerThread._gather_link_box walks
        # follower rows via this same fetch and must know (a) is this
        # row part of the box and (b) does the box CONTINUE below it —
        # without a second grid read, and critically without fetching
        # the first row PAST the box just to discover it ended (a fetch
        # advances the frozen frontier; over-fetching would freeze the
        # next unrelated row for the box's whole execution). Same trick
        # _gather_nested_chain gets from 'has_child'.
        has_follower = False
        if row + 1 < self.grid.GetNumberRows():
            below_depth, below_linked = self._struct_get(row + 1)
            has_follower = below_linked and below_depth == depth

        if not alias or not is_on:
            return {'eligible': False, 'linked': linked, 'has_follower': has_follower}

        device_name, _, command_name = alias.partition('_')

        value_str = self.grid.GetCellValue(row, self.COL_START)
        final_str = self.grid.GetCellValue(row, self.COL_FINAL)
        steps_str = self.grid.GetCellValue(row, self.COL_STEPS)
        inttime_str = self.grid.GetCellValue(row, self.COL_INTTIME)
        comment = self.grid.GetCellValue(row, self.COL_COMMENT)
        info = {'alias': alias, 'device_name': device_name,
                'command_name': command_name, 'value_str': value_str,
                'final_str': final_str, 'steps_str': steps_str,
                'inttime_str': inttime_str, 'comment': comment,
                'depth': depth, 'has_child': self._has_active_child(row),
                'linked': linked, 'has_follower': has_follower}
        if linked:
            # Still NOT independently eligible — a follower only ever
            # runs as part of its anchor's box (gathered by
            # _gather_link_box, which reads the full field data returned
            # here). Reached directly by the scan = orphan (anchor
            # inactive/failed/absent): passed over, same as an orphaned
            # nested row.
            info['eligible'] = False
            info['skip_reason'] = 'linked row — runs with its anchor'
        else:
            info['eligible'] = True
        return info

    def _fetch_row_state_for_thread(self, row: int, result_holder: dict, done_event):
        """Runs on the main thread via wx.CallAfter — the ONLY place
        TaskRunnerThread's row data comes from, and it goes through
        this synchronous round-trip rather than the thread touching
        self.grid directly (wx is not thread-safe). Advances
        self._run_frontier to `row` UNCONDITIONALLY, before even
        checking eligibility — freezing takes effect the instant the
        thread claims a row, not after it decides what to do with it.
        This ordering also closes the drag-mid-fetch race: by the time
        _evaluate_row_eligibility reads the row's cells, dragging it is
        already refused.

        SaveEditControlValue() first: if a cell is still in its
        in-place text editor when this fires (typed a value, then
        clicked Start or another widget WITHOUT first pressing Enter/
        Tab or clicking a different cell), GetCellValue() can return
        the value from BEFORE that edit — wx.grid only commits an
        edit's text into the cell's stored value once the editor is
        told to close, and that isn't guaranteed to happen just because
        a different widget got focus. This closes that gap for every
        row this method ever reads, not only the one being fetched
        right now."""
        self.grid.SaveEditControlValue()
        try:
            if row >= self.grid.GetNumberRows():
                result_holder['exists'] = False
            else:
                self._run_frontier = row
                result_holder.update(self._evaluate_row_eligibility(row))
                result_holder['exists'] = True
        finally:
            done_event.set()

    def _on_editor_shown(self, event):
        """Veto cell editing only for frozen rows (frontier or above).
        Belt-and-suspenders alongside the per-cell readonly flags
        _on_row_start/_on_row_pass_over/_mark_completed already set —
        this can't drift out of sync with those the way a blanket
        'any run' veto would have (that was last round's overcorrection:
        it also blocked editing rows below the frontier, which are
        explicitly required to stay live)."""
        if self._row_frozen(event.GetRow()):
            event.Veto()
            return
        event.Skip()

    def _on_cell_left_click(self, event):
        if event.GetCol() == self.COL_ON:
            row = event.GetRow()
            if self._row_frozen(row):
                return   # frozen: at or above the frontier
            if self._is_completed(row):
                return   # frozen — already ran, toggling On doesn't undo that
            cur = self.grid.GetCellValue(row, self.COL_ON) == '1'
            alias = self.grid.GetCellValue(row, self.COL_CMD)
            if not cur and alias == '':
                return   # refuse: can't activate an empty row
            if not cur and not self._device_is_active(alias):
                return   # refuse: can't activate while its device is off
            self.grid.SetCellValue(row, self.COL_ON, '0' if cur else '1')
            self.grid.ForceRefresh()
            return   # consumed: don't open an editor for this column
        event.Skip()

    # -----------------------------------------------------------------
    # Arrow-key-only navigation: commit the current edit and move to the
    # adjacent cell on Left/Right/Up/Down, instead of requiring Enter
    # first (default wx.Grid behaviour moves the text cursor within the
    # field while editing; this overrides that for all four arrow keys).
    # -----------------------------------------------------------------
    def _on_editor_created(self, event):
        event.GetControl().Bind(wx.EVT_KEY_DOWN, self._on_editor_key_down)
        event.Skip()

    def _on_editor_key_down(self, event):
        move = {
            wx.WXK_LEFT:  self.grid.MoveCursorLeft,
            wx.WXK_RIGHT: self.grid.MoveCursorRight,
            wx.WXK_UP:    self.grid.MoveCursorUp,
            wx.WXK_DOWN:  self.grid.MoveCursorDown,
        }.get(event.GetKeyCode())
        if move is not None:
            self.grid.DisableCellEditControl()   # commits the pending value
            move(False)
            return   # consumed — don't also move the text cursor within the field
        event.Skip()

    # -----------------------------------------------------------------
    # Integration Time: bare number -> seconds, explicit s/m/h kept,
    # anything else clipped to '0s'
    # -----------------------------------------------------------------
    def _on_cell_changed(self, event):
        if event.GetCol() == self.COL_INTTIME:
            row = event.GetRow()
            val = self.grid.GetCellValue(row, self.COL_INTTIME)
            normalized = _normalize_integration_time(val)
            alias = self.grid.GetCellValue(row, self.COL_CMD)
            device_name, _, command_name = alias.partition('_')
            if (device_name == STANDARD_DEVICE_NAME
                    and command_name not in _RESERVED_CONTROL_COMMAND_NAMES):
                # While row: IntTime is the poll interval, floor 0.1s —
                # rejected HERE at entry (requirement: "prevent entering 0s"),
                # not only at run time. Note the normalizer itself turns
                # garbage/empty into '0s', so any unparseable entry also
                # lands in this rejection instead of silently becoming a
                # dead value. The previous cell value is restored if it
                # was itself valid (a mis-typed edit shouldn't destroy a
                # good interval), else the '1s' seed. The run-time check
                # in _run_while_loop stays as defense for hand-edited
                # task files.
                try:
                    secs = _parse_duration_seconds(normalized)
                except ValueError:
                    secs = -1.0
                if secs < 0.1:
                    old_norm = _normalize_integration_time(event.GetString())
                    try:
                        old_ok = _parse_duration_seconds(old_norm) >= 0.1
                    except ValueError:
                        old_ok = False
                    self.grid.SetCellValue(row, self.COL_INTTIME,
                                           old_norm if old_ok else '1s')
                    wx.MessageBox(f'{alias}: Integration Time is the poll interval '
                                  f'for a while row — minimum 0.1s ({val.strip()!r} '
                                  f'rejected).', 'Invalid poll interval',
                                  wx.OK | wx.ICON_WARNING)
                    event.Skip()
                    return
            if val.strip() and not _INTTIME_RE.match(val):
                # Non-while rows: garbage IntTime used to be silently
                # normalized to '0s' — the same silent-coercion family
                # as the Steps '10a' incident (typo swallowed, row runs
                # as something other than intended). Keep the raw text
                # instead so _validate_row_fields below paints it red;
                # the user sees exactly what they typed, flagged. (While
                # rows never reach here — rejected above with a
                # restore.) Execution unchanged: a run with garbage
                # IntTime already fails loudly in the sweep engines.
                pass
            else:
                self.grid.SetCellValue(row, self.COL_INTTIME, normalized)
        if event.GetCol() in (self.COL_START, self.COL_FINAL,
                              self.COL_STEPS, self.COL_INTTIME):
            self._validate_row_fields(event.GetRow())
            self.grid.ForceRefresh()
        event.Skip()

    # -----------------------------------------------------------------
    # Row drag-drop (ported from v1's sub_grid.py — proven mechanism,
    # explicitly avoids CaptureMouse; see class docstring)
    # -----------------------------------------------------------------
    def _on_label_left_down(self, event):
        y = event.GetY()
        row = self.grid.YToRow(y)
        if row < 0 or row >= self.grid.GetNumberRows():
            event.Skip()
            return
        if self._is_completed(row) or self._row_frozen(row):
            self._drag_row = -1   # frozen (completed, or at/above the run frontier):
            event.Skip()          # can still be selected/viewed, never dragged
            return
        self._drag_row = row
        self._drag_start_y = y
        self._drag_active = False

        sel = list(self.grid.GetSelectedRows())
        if row in sel and len(sel) > 1:
            # Clicked on a row that's part of an existing multi-row
            # marked selection: preserve the whole group so it can be
            # dragged together. event.Skip() here would let wx.Grid's
            # default mousedown handling collapse the selection down to
            # just this row before the drag even starts.
            return
        event.Skip()   # single row (or nothing) marked — normal select

    def _on_label_motion(self, event):
        if self._drag_row < 0 or not event.LeftIsDown():
            if not event.LeftIsDown() and self._drag_active:
                self._drag_active = False
                self._drag_row = -1
                self._drag_rows = []
                self._drop_indicator.hide()
            event.Skip()
            return

        y = event.GetY()
        if not self._drag_active:
            if abs(y - self._drag_start_y) < self._DRAG_THRESHOLD:
                event.Skip()
                return
            self._drag_active = True   # crossed threshold — this is a real drag
            # Move the whole current marked selection if the drag started
            # on a row that's part of it; otherwise just the one row.
            # Completed and frozen (at/above the run frontier) rows are
            # excluded even if selected alongside draggable ones.
            sel = list(self.grid.GetSelectedRows())
            rows = sorted(sel) if self._drag_row in sel else [self._drag_row]
            self._drag_rows = [r for r in rows if not self._is_completed(r) and not self._row_frozen(r)]
            if not self._drag_rows:
                self._drag_active = False
                return

        # A drag that started legitimately (all rows below the frontier)
        # can still be overtaken mid-gesture if the scan catches up to
        # one of the dragged rows while the mouse is still moving — kill
        # it rather than let a stale drag complete against a row that's
        # since been claimed.
        if self.run_active() and any(self._row_frozen(r) for r in self._drag_rows):
            self._drag_active = False
            self._drag_row = -1
            self._drag_rows = []
            self._drop_indicator.hide()
            return

        # Don't Skip during an active drag: prevents wx.Grid's own default
        # label-drag processing (range selection) from also engaging.
        target = self._drop_target_row(y)
        if target < 0:
            self._drop_indicator.hide()
            return
        try:
            n = self.grid.GetNumberRows()
            if target >= n:
                # Appending past the last row — CellToRect(n, 0) is out of
                # bounds (only 0..n-1 are real rows), so use the bottom
                # edge of the last row instead.
                rect = self.grid.CellToRect(n - 1, 0)
                y_pos = rect.y + rect.height
            else:
                y_pos = self.grid.CellToRect(target, 0).y
            _, loy = self.grid.GetGridRowLabelWindow().GetPosition()
            _, goy = self.grid.GetGridWindow().GetPosition()
            self._drop_indicator.place(y_pos + loy - goy)
        except Exception:
            pass

    def _on_label_left_up(self, event):
        self._drop_indicator.hide()
        was_dragging = self._drag_active
        drag_rows = self._drag_rows[:]
        self._drag_active = False
        self._drag_row = -1
        self._drag_rows = []
        self._drag_start_y = -1

        if not was_dragging or not drag_rows:
            event.Skip()
            return

        if self.run_active() and any(self._row_frozen(r) for r in drag_rows):
            event.Skip()   # scan caught up to one of these between motion and drop — discard
            return

        target = self._drop_target_row(event.GetY())
        if target < 0 or target in drag_rows:
            event.Skip()
            return

        self._move_rows(drag_rows, target)
        event.Skip()

    def _on_label_leave(self, event):
        if self._drag_active:
            self._drop_indicator.hide()
        event.Skip()

    def _drop_target_row(self, label_y: int) -> int:
        """Return the target row index, where n (== GetNumberRows()) means
        'append after the last row' — distinct from n-1, which would mean
        'insert before the last row' and made it impossible to ever move
        a row past the current last position.

        Returns -1 (invalid) if the target is at or before ANY completed
        row currently in the grid, OR — during a run — at or before the
        scan's frontier (see _row_frozen). A completed row represents
        something that already happened; inserting content before it —
        even between two completed rows, not touching either one's own
        slot directly — would still retroactively change what came
        before it in the sequence. The frontier is the live-run version
        of the same rule: 'do not drag it above the running task,' not
        just onto its exact slot."""
        n = self.grid.GetNumberRows()
        if n == 0:
            return -1
        label_y = max(0, label_y)
        row = self.grid.YToRow(label_y)
        target = row if row >= 0 else n
        last_completed = max((r for r in range(n) if self._is_completed(r)), default=-1)
        if self.run_active():
            last_completed = max(last_completed, self._run_frontier)
        if target <= last_completed:
            return -1
        return target

    def _snapshot_row(self, row: int) -> list:
        return [self.grid.GetCellValue(row, c) for c in range(self.grid.GetNumberCols())]

    def _move_rows(self, rows_to_move: list, dst: int):
        """Move a list of rows (any order) to position dst. dst may equal
        GetNumberRows(), meaning 'append after everything'. Ported from
        v1's sub_grid.py: delete bottom-to-top so earlier indices stay
        valid, adjusting the insert position for every deletion that
        occurred above it, then insert the whole block at once.

        If ANY moved row was nested (depth > 0) before the drag, ALL
        nesting in the ENTIRE grid is reset to flat afterward — not just
        the dragged row(s). Nesting depth is only meaningful relative to
        a specific sequence of rows; dragging rearranges that sequence,
        so ANY pre-existing nesting anywhere becomes unverifiable. Rather
        than try to patch up relationships (tried twice, wrong both
        times), just flatten everything and let it be rebuilt
        deliberately with Nest. Linked flags are untouched — orthogonal
        to nesting, not affected by this."""
        if not rows_to_move:
            return
        if self.run_active() and (dst <= self._run_frontier
                                  or any(self._row_frozen(r) for r in rows_to_move)):
            return   # last line of defense at the mutation site itself —
                     # entry points are guarded, but a future caller
                     # (DEL key, new menu item) must not bypass this
        rows_to_move = sorted(rows_to_move)
        snapshots = [self._snapshot_row(r) for r in rows_to_move]
        any_was_nested = any(
            int((values[self.COL_STRUCT].partition(':')[0]) or 0) > 0
            for values in snapshots
        )
        insert_at = dst
        for r in reversed(rows_to_move):
            self.grid.DeleteRows(r, 1)
            if r < insert_at:
                insert_at -= 1
        insert_at = max(0, min(insert_at, self.grid.GetNumberRows()))
        self.grid.InsertRows(insert_at, len(rows_to_move))
        for i, values in enumerate(snapshots):
            row = insert_at + i
            for c, v in enumerate(values):
                self.grid.SetCellValue(row, c, v)
            self.grid.SetReadOnly(row, self.COL_ON, True)
            self.grid.SetReadOnly(row, self.COL_CMD, True)
        if any_was_nested:
            for row in range(self.grid.GetNumberRows()):
                depth, linked = self._struct_get(row)
                if depth > 0:
                    self._struct_set(row, 0, linked)
        self.grid.ClearSelection()
        for i in range(len(rows_to_move)):
            self.grid.SelectRow(insert_at + i, addToSelected=True)
        self.grid.ForceRefresh()

    # -----------------------------------------------------------------
    # Context menu: deactivate / delete marked rows
    # -----------------------------------------------------------------
    def _row_is_script(self, row: int) -> bool:
        """True if this row's command is a script_* one. A script row
        has no sweep values at all (see run()'s SCRIPT_DEVICE_NAME
        dispatch branch, and _run_nested_sweep's explicit rejection of
        it as a chain member) — it can never validly be nested, nor
        have anything nested under it."""
        alias = self.grid.GetCellValue(row, self.COL_CMD)
        device_name, _, _ = alias.partition('_')
        return device_name == SCRIPT_DEVICE_NAME

    def _row_is_nonnestable_control(self, row: int) -> bool:
        """True if this row is a control command with no sweep values —
        anything on control EXCEPT counter: while-commands,
        pause/userprompt/stop, and legacy dead entries. Exactly the
        set _run_nested_sweep rejects as chain members at run time
        (its 'anything on control except counter' check), so the menu
        gating and the execution rule can never disagree."""
        alias = self.grid.GetCellValue(row, self.COL_CMD)
        device_name, _, command_name = alias.partition('_')
        return device_name == STANDARD_DEVICE_NAME and command_name != 'counter'

    def _selection_touches_nonnestable(self, sel) -> bool:
        """True if any row in sel can't take part in nesting, OR the
        row directly above any row in sel can't — covering both
        invalid directions: nesting the row itself (it would become a
        child), and nesting something under it (it would become a
        parent/chain root). Was _selection_touches_script (scripts
        only, requirement: 'If I mark a script either individually or in a
        multiselection, nesting... must be grayed out'); widened to
        non-counter control rows after the same gap showed up with
        whiles (nest was offered on a while row, and on a device row
        directly below one) — previously those were only rejected at
        run time and in Simulate, but users don't necessarily
        simulate first. The parent direction matters because run()'s
        dispatch executes these rows standalone and advances past
        them before ever checking has_child, so anything nested under
        one would just be silently orphaned."""
        for row in sel:
            if self._row_is_script(row) or self._row_is_nonnestable_control(row):
                return True
            if row > 0 and (self._row_is_script(row - 1)
                            or self._row_is_nonnestable_control(row - 1)):
                return True
        return False

    def _on_label_right_click(self, event):
        row = event.GetRow()
        if row < 0:
            event.Skip()
            return
        if self._is_completed(row) or self._row_frozen(row):
            return   # frozen — no context menu at all, not even a partial one
        sel = list(self.grid.GetSelectedRows())
        if row not in sel:
            self.grid.SelectRow(row)   # right-click on an unmarked row marks just that one
            sel = [row]
        elif self.run_active():
            # An existing multi-row selection may span the frontier —
            # e.g. marked before Start, and the scan has since passed
            # some of them. Drop the now-frozen ones so Delete/Nest/
            # etc. below only ever act on rows still fair game.
            keep = [r for r in sel if not self._row_frozen(r)]
            if len(keep) != len(sel):
                self.grid.ClearSelection()
                for r in keep:
                    self.grid.SelectRow(r, addToSelected=True)
                sel = keep

        # Menu reflects the clicked row's actual current state — no point
        # offering Unnest on a row that isn't nested, or Link on a row
        # that's already linked. Nest specifically depends on the row
        # ABOVE having room, not this row's own current depth — nesting
        # always targets (row above's depth) + 1. Also: don't offer Nest
        # if the row is ALREADY at that target — that's a pure no-op
        # (e.g. row already sitting one level under its parent).
        depth, linked = self._struct_get(row)
        above_depth = self._struct_get(row - 1)[0] if row > 0 else self.MAX_DEPTH
        has_child = self._has_child(row)
        nest_would_change = depth < above_depth + 1
        nonnestable_involved = self._selection_touches_nonnestable(sel)

        menu = wx.Menu()
        if depth == 0 and not self.run_active():
            # Execute Row isn't wired up yet regardless (see
            # _on_execute_row) — hidden here specifically so it's not
            # offered on a row that's already live-queued to run for
            # real via the currently-executing scan.
            item_exec = menu.Append(wx.ID_ANY, 'Execute Row')
            self.grid.Bind(wx.EVT_MENU, self._on_execute_row, item_exec)
            menu.AppendSeparator()
        item_act   = menu.Append(wx.ID_ANY, 'Activate marked row(s)')
        item_deact = menu.Append(wx.ID_ANY, 'Deactivate marked row(s)')
        menu.AppendSeparator()
        if row > 0 and above_depth < self.MAX_DEPTH and not has_child and nest_would_change:
            # requirement: 'If I mark a script either individually or in a
            # multiselection, nesting... must be grayed out' — shown
            # (structurally it WOULD be a valid nest target) but
            # disabled, matching Link's own grayed-not-hidden treatment
            # below, rather than silently vanishing and leaving the
            # user to guess why. _on_nest_marked isn't bound at all in
            # that case — same pattern Link already uses.
            item_nest = menu.Append(wx.ID_ANY, 'Nest')
            if nonnestable_involved or any(self._row_in_link_box(r) for r in (sel or [row])):
                # Link-box members can't nest: changing any member's
                # depth breaks the same-depth invariant the box is
                # built on (and v1 linking is depth-0 only). Grayed,
                # not hidden — same treatment as scripts.
                item_nest.Enable(False)
            else:
                self.grid.Bind(wx.EVT_MENU, self._on_nest_marked, item_nest)
        if depth > 0 and not has_child:
            # Deliberately NOT gated on nonnestable_involved — Unnest is the
            # escape hatch out of an already-nested script (however it
            # got there, e.g. a grid built before this check existed),
            # not a way to create a new invalid structure. Blocking it
            # too would trap a script permanently nested with no way to
            # fix it short of deleting the row.
            item_unnest = menu.Append(wx.ID_ANY, 'Unnest')
            self.grid.Bind(wx.EVT_MENU, self._on_unnest_marked, item_unnest)
        if not linked:
            # Linking is now implemented — offered whenever the clicked
            # row isn't already linked, grayed (not hidden, the user's
            # preference over a popup) whenever the selection couldn't
            # form a valid box: wrong depth (v1: depth-0 only, no
            # nest+link combining), non-contiguous multi-selection, a
            # script/control row as follower, an invalid mother
            # (script, or a control command other than counter), or
            # the box exceeding MAX_LINK_CHAIN.
            item_link = menu.Append(wx.ID_ANY, 'Link')
            if self._link_menu_allowed(sel if len(sel) > 1 else [row]):
                self.grid.Bind(wx.EVT_MENU, self._on_link_marked, item_link)
            else:
                item_link.Enable(False)
        else:
            item_unlink = menu.Append(wx.ID_ANY, 'Unlink')
            self.grid.Bind(wx.EVT_MENU, self._on_unlink_marked, item_unlink)
        menu.AppendSeparator()
        item_ins_above = menu.Append(wx.ID_ANY, 'Insert row(s) above...')
        item_ins_below = menu.Append(wx.ID_ANY, 'Insert row(s) below...')
        self.grid.Bind(wx.EVT_MENU, lambda evt, r=row: self._on_insert_rows(r, True), item_ins_above)
        self.grid.Bind(wx.EVT_MENU, lambda evt, r=row: self._on_insert_rows(r, False), item_ins_below)
        item_del   = menu.Append(wx.ID_ANY, 'Delete marked row(s)')
        self.grid.Bind(wx.EVT_MENU, self._on_activate_marked, item_act)
        self.grid.Bind(wx.EVT_MENU, self._on_deactivate_marked, item_deact)
        self.grid.Bind(wx.EVT_MENU, self._on_delete_marked, item_del)
        self.grid.PopupMenu(menu)
        menu.Destroy()

    def _on_insert_rows(self, row: int, above: bool):
        """Prompts for the count via a small number-entry dialog rather
        than a toolbar control — wx (and native menus generally) can't
        embed a live text entry inside a menu item, so this is the
        standard substitute (same pattern as Excel's 'Insert Cells...').
        Remembers the last value used. New rows are always flat (depth 0,
        unlinked) — no attempt to infer nesting from context; inserting
        into the middle of an existing nested block is the user's call
        to reconcile afterward with Nest/Unnest, same as drag already
        requires."""
        if self.run_active() and self._row_frozen(row):
            return   # site guard, same rationale as _move_rows
        n = wx.GetNumberFromUser('How many empty rows?', 'Count:', 'Insert Rows',
                                 value=self._last_insert_count, min=1, max=50, parent=self)
        if n == -1:
            return   # cancelled
        self._last_insert_count = n
        insert_at = row if above else row + 1
        self.grid.InsertRows(insert_at, n)
        for r in range(insert_at, insert_at + n):
            self._blankify_row(r)
        self.grid.ForceRefresh()

    def _on_activate_marked(self, event):
        for row in self.grid.GetSelectedRows():
            if self._row_frozen(row):
                continue   # defensive — entry point already filters these out
            alias = self.grid.GetCellValue(row, self.COL_CMD)
            if alias == '':
                continue   # can't activate an empty row
            if not self._device_is_active(alias):
                continue   # can't activate while its device is off
            self.grid.SetCellValue(row, self.COL_ON, '1')
        self.grid.ForceRefresh()

    def _on_deactivate_marked(self, event):
        for row in self.grid.GetSelectedRows():
            if self._row_frozen(row):
                continue   # defensive — entry point already filters these out
            self.grid.SetCellValue(row, self.COL_ON, '0')
        self.grid.ForceRefresh()

    def _on_delete_marked(self, event):
        for row in sorted(self.grid.GetSelectedRows(), reverse=True):
            if self._row_frozen(row):
                continue   # site guard, same rationale as _move_rows
            self.grid.DeleteRows(row, 1)
        if self.grid.GetNumberRows() == 0:
            self._insert_blank_row(0)   # same bootstrap gap as an empty grid at startup
        self._ensure_trailing_blank()   # deleting the last row(s) must not lose it
        self.grid.ClearSelection()
        self.grid.ForceRefresh()

    # -----------------------------------------------------------------
    # Structure (nesting depth + linked flag), stored hidden in
    # COL_STRUCT as 'depth:linked'. No separate 'nested' flag — nesting
    # is just depth > 0.
    # -----------------------------------------------------------------
    def _struct_get(self, row):
        raw = self.grid.GetCellValue(row, self.COL_STRUCT)
        depth_str, _, linked_str = raw.partition(':')
        try:
            depth = int(depth_str)
        except ValueError:
            depth = 0
        return depth, linked_str == 'linked'

    def _struct_set(self, row, depth, linked):
        self.grid.SetCellValue(row, self.COL_STRUCT, f'{depth}:{"linked" if linked else ""}')

    def _row_in_link_box(self, row: int) -> bool:
        """True if this row is part of a link box — either itself
        linked (a follower) or the anchor of one (the row directly
        below is linked at the same depth). Mirrors
        CommandCellRenderer._chain_segment's membership logic. Used to
        gray Nest for the whole box: nesting any member would change
        its depth and break the same-depth invariant linking is built
        on (and v1 linking is depth-0 only anyway)."""
        depth, linked = self._struct_get(row)
        if linked:
            return True
        if row + 1 < self.grid.GetNumberRows():
            below_depth, below_linked = self._struct_get(row + 1)
            return below_linked and below_depth == depth
        return False

    def _link_box_anchor(self, row: int) -> int:
        """The anchor ('mother') row of the box `row` belongs to — walks
        up while the current row is linked. For an unlinked row this is
        the row itself."""
        top = row
        while top > 0 and self._struct_get(top)[1]:
            top -= 1
        return top

    def _row_linkable_as_follower(self, row: int) -> bool:
        """Can this row carry the linked flag? Must be a real device
        write row: not empty, not a control command (nothing numeric to
        write — counter included; a counter can only be a MOTHER), not
        a script."""
        alias = self.grid.GetCellValue(row, self.COL_CMD)
        if not alias:
            return False
        device_name, _, _cmd = alias.partition('_')
        return device_name not in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME)

    def _row_valid_as_anchor(self, row: int) -> bool:
        """Can this row be a link-box mother? Real device rows and
        control_counter (followers = f(counter value)) qualify; other
        control commands (pause/userprompt/stop — no numeric setpoint
        to follow) and scripts don't. Empty rows don't either."""
        alias = self.grid.GetCellValue(row, self.COL_CMD)
        if not alias:
            return False
        device_name, _, command_name = alias.partition('_')
        if device_name == SCRIPT_DEVICE_NAME:
            return False
        if device_name == STANDARD_DEVICE_NAME:
            return command_name == 'counter'
        return True

    def _apply_link_field_locks(self, row: int):
        """A follower's whole definition is the expression in
        Constant/Start ([%] = mother's value) — Final/Steps/IntTime are
        meaningless for it (the mother's sweep drives everything) and
        are cleared + locked, same pattern as _apply_control_field_locks.
        Comment stays editable. Released again by
        _release_link_field_locks on Unlink."""
        for col in (self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
            self.grid.SetCellValue(row, col, '')
            self.grid.SetReadOnly(row, col, True)

    def _release_link_field_locks(self, row: int):
        """Inverse of _apply_link_field_locks, on Unlink. Safe to apply
        unconditionally: control/script rows can never carry the linked
        flag in the first place (_row_linkable_as_follower), so nothing
        that must STAY locked ever passes through here."""
        for col in (self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
            self.grid.SetReadOnly(row, col, False)

    def _is_completed(self, row: int) -> bool:
        return self.grid.GetCellValue(row, self.COL_COMPLETED) == '1'

    def _validate_row_fields(self, row: int):
        """Paint Constant/Start, Final, Steps, Integration Time with a
        light-red background when the row's fields are inconsistent or
        unparseable — a purely VISUAL guard (requirement: 'I dont want too
        much meddling with the execution. Everything must happen in
        the grid.'). Execution and Simulate are untouched: a run with
        bad fields fails (or, for the documented blank/0-Steps case,
        runs as a constant) exactly as before; this exists so the trap
        is visible in the grid BEFORE Start — motivated by a real
        incident: Steps '10a' silently coerced to 0 at dispatch, the
        row ran as 'constant set 0 T', and the user believed they had
        swept to 0.01 T.

        Rules for a real-device write row:
        - Constant only (Start numeric, Final blank, Steps blank/'0'):
          fine, nothing painted. '0' Steps alone stays the documented
          constant marker.
        - Sweep intent (Final non-blank, or Steps non-blank and not
          '0'): ALL FOUR fields are required — any blank one among
          Start/Final/Steps/IntTime is painted until it contains a
          value ('you enter values in final then steps and integration
          must be highlighted in red until they contain values...
          you first enter a value in final but not constant/start,
          then also highlight constant/start').
        - A non-numeric string in any field is painted regardless
          ('you enter a string somewhere, same thing'); Steps
          additionally rejects negatives, and '0' Steps WITH a Final
          is painted (that contradiction is the exact 10a-class trap:
          it runs as a constant while looking like a sweep).

        IntTime alone also signals sweep intent — a constant write
        ignores IntTime, so a row carrying one without Final/Steps is
        painted on the fields left of it.

        Special rules per row type ('ensure the other cases dont give
        false error (like while, linked)'):
        - blank rows (no command): nothing to validate
        - linked FOLLOWERS: Start legitimately holds an f([%])
          expression and is never numeric-checked — but it is the
          follower's only live field, so a BLANK Start is painted
          (nothing for the link to write). Anchors/mothers are
          ordinary sweep rows and validate normally, as do nested
          rows at any depth.
        - pause: Start is its duration — full duration syntax
          accepted ('4', '5m', '500ms'), blank = the 1s default,
          garbage painted (it fails loudly at run time).
        - userprompt/stop (fields locked) and while rows (own locks +
          own IntTime entry validation): skipped
        - script rows: fields unused — skipped
        - counter: blank Start/Final stay the documented idiom
          (Start->0, Final->Start+Steps), but any field entered makes
          Steps (integer >= 1) and IntTime required and painted until
          present; garbage painted regardless
        - completed rows mid-run keep their completed color untouched.
        """
        cols = (self.COL_START, self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME)

        def paint(bad_start=False, bad_final=False, bad_steps=False, bad_intt=False):
            for col, bad in zip(cols, (bad_start, bad_final, bad_steps, bad_intt)):
                self.grid.SetCellBackgroundColour(
                    row, col, COLOR_CELL_INVALID if bad else COLOR_PANEL_BG)

        if self._is_completed(row):
            return   # don't fight the completed-row color mid-run
        alias = self.grid.GetCellValue(row, self.COL_CMD)
        if not alias:
            paint()
            return
        device_name, _, command_name = alias.partition('_')
        if device_name == SCRIPT_DEVICE_NAME:
            paint()
            return
        if device_name == STANDARD_DEVICE_NAME and command_name == 'pause':
            # pause's Start IS its duration and accepts the full
            # duration syntax the run uses ('4', '5m', '1h', '500ms' —
            # _parse_duration_seconds); blank means the documented 1s
            # default. Garbage ('s') is painted — it fails loudly at
            # run time, and previously nothing in the grid said so.
            start = self.grid.GetCellValue(row, self.COL_START)
            bad = False
            if start.strip():
                try:
                    _parse_duration_seconds(start)
                except ValueError:
                    bad = True
            paint(bad_start=bad)
            return
        if device_name == STANDARD_DEVICE_NAME and command_name != 'counter':
            paint()
            return   # userprompt/stop (fields locked) and while rows
                     # (own locks + own IntTime entry validation)
        _, linked = self._struct_get(row)
        if linked:
            # A follower's Start legitimately holds an f([%])
            # expression — never numeric-checked — but it is the
            # follower's ONLY field (the rest is locked empty), so a
            # BLANK Start is painted: there is nothing for the link to
            # write ('A linked device must show red in constant if
            # nothing is entered').
            paint(bad_start=not self.grid.GetCellValue(row, self.COL_START).strip())
            return

        start = self.grid.GetCellValue(row, self.COL_START)
        final = self.grid.GetCellValue(row, self.COL_FINAL)
        steps = self.grid.GetCellValue(row, self.COL_STEPS)
        intt = self.grid.GetCellValue(row, self.COL_INTTIME)

        def is_float(s):
            try:
                float(s)
                return True
            except ValueError:
                return False

        def intt_ok(s):
            return _INTTIME_RE.match(s) is not None

        steps_s = steps.strip()
        steps_int = None
        if steps_s:
            try:
                steps_int = int(steps_s)
            except ValueError:
                steps_int = -1   # unparseable — bad either way below

        if device_name == STANDARD_DEVICE_NAME:
            # counter: blank Start/Final stay the documented idiom
            # (Start defaults to 0, Final to Start+Steps) — but a
            # counter is a sweep by nature, so the moment ANY of the
            # four fields is entered, Steps (an integer >= 1) and
            # IntTime are required and painted until present ('I can
            # enter 1s in IntTime and 1 in final. No error' — now:
            # Steps red). Garbage is painted regardless, as everywhere.
            intent = (bool(start.strip()) or bool(final.strip())
                      or steps_s != '' or bool(intt.strip()))
            # Blank Start/Final are forgiven ONLY once Steps is valid —
            # the 0..N idiom needs an N by definition. While Steps is
            # missing/invalid, the row is incomplete and blanks paint
            # exactly like a device row's (requirement: IntTime-only on a
            # counter must highlight Constant and Final too, 'you do
            # for devices BUT NOT for counter' — round-7's blanket
            # blank forgiveness was too lax for the incomplete case).
            steps_valid = steps_s != '' and steps_int >= 1
            paint(bad_start=(bool(start.strip()) and not is_float(start))
                            or (intent and not start.strip() and not steps_valid),
                  bad_final=(bool(final.strip()) and not is_float(final))
                            or (intent and not final.strip() and not steps_valid),
                  bad_steps=(steps_s != '' and steps_int < 0)
                            or (intent and not steps_valid),
                  bad_intt=(bool(intt.strip()) and not intt_ok(intt))
                           or (intent and not intt.strip()))
            return

        # Device write row. IntTime alone also signals sweep intent
        # ('entering ONLY IntTime should also show all fields left
        # next to it in red') — a constant write ignores IntTime, so a
        # row carrying one without Final/Steps is ambiguous about what
        # the user meant, exactly the ambiguity this feature exists to
        # surface.
        sweep_intent = (bool(final.strip()) or steps_s not in ('', '0')
                        or bool(intt.strip()))
        paint(
            bad_start=(bool(start.strip()) and not is_float(start))
                      or (sweep_intent and not start.strip()),
            bad_final=(bool(final.strip()) and not is_float(final))
                      or (sweep_intent and not final.strip()),
            bad_steps=(steps_s != '' and steps_int < 0)
                      or (sweep_intent and steps_s == '')
                      or (steps_int == 0 and bool(final.strip())),
            bad_intt=(bool(intt.strip()) and not intt_ok(intt))
                     or (sweep_intent and not intt.strip()),
        )

    def _revalidate_all_rows(self):
        """Re-run field validation over every row — after bulk
        operations that repaint or restructure (task-file load, run
        end's _clear_row_highlight sweep, link/unlink changing a row's
        follower exemption)."""
        for row in range(self.grid.GetNumberRows()):
            self._validate_row_fields(row)
        self.grid.ForceRefresh()

    def _mark_completed(self, row: int):
        """Once marked: greyed out AND read-only on every value/comment
        column, frozen against drag/drop/context-menu edits too (see
        _on_label_left_down / _drop_target_row / _on_label_right_click).
        Only successfully-executed rows get marked — a failed or
        skipped row stays editable so it can be fixed and retried.

        NOT permanent: released automatically when the whole run
        finishes (_on_run_finished) — confirmed via screenshot:
        the list needs to be re-runnable, not frozen forever. This lock
        holds for the duration of the run only, same as the temporary
        pass-over lock on ineligible rows.

        The read-only part was missing entirely before an earlier fix —
        _mark_completed only greyed the background, so a completed
        row's values were still directly editable via a normal
        double-click, bypassing every other lockout built around it."""
        self.grid.SetCellValue(row, self.COL_COMPLETED, '1')
        for col in range(self.grid.GetNumberCols()):
            self.grid.SetCellBackgroundColour(row, col, COLOR_BORDER)
        for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS,
                   self.COL_INTTIME, self.COL_COMMENT):
            self.grid.SetReadOnly(row, col, True)

    def _has_child(self, row: int) -> bool:
        """True if the row immediately below this one is nested deeper —
        i.e. this row currently anchors a nested block beneath it. Such a
        row can't change its own depth: nothing cascades to descendants
        (by design, per earlier discussion), so changing the anchor's
        depth would silently orphan them. Forces unnest to peel from the
        bottom: the deepest row has no child and is always eligible;
        unnesting it makes the row above it eligible next, and so on.

        STRUCTURAL only — deliberately blind to the child's On checkbox.
        Correct for every UI call site (unnest peeling, drag rules, the
        link menu's no-children condition): structure must persist while
        a row is merely switched off, or toggling a child off would
        open loopholes like linking the parent and re-activating the
        child into an invalid combination. For run-time/simulate
        dispatch, use _has_active_child instead."""
        if row + 1 >= self.grid.GetNumberRows():
            return False
        depth_this, _ = self._struct_get(row)
        depth_next, _ = self._struct_get(row + 1)
        return depth_next > depth_this

    def _has_active_child(self, row: int) -> bool:
        """_has_child AND the child row is actually switched on with a
        non-empty command — the RUN-TIME notion of 'has a child'. An
        unchecked row is skipped, not deleted (same as everywhere
        else), so at execution time it isn't part of any structure: a
        parent whose only child is deactivated runs standalone, and
        anything nested deeper below the deactivated row is orphaned
        (passed over by the scan, exactly like a depth>0 row with no
        live parent has always been). Before this existed, run()'s
        dispatch and Simulate used structural _has_child, so a
        deactivated child was gathered into the chain as a live member
        — its eligibility fetch returns a minimal dict (no 'alias', no
        field values), which crashed _run_nested_sweep with a KeyError
        the moment it read chain_fetched (the user's 'huge bug'). Mirrors
        what _gather_link_box already did for deactivated followers
        (in the box structurally, excluded from execution)."""
        if not self._has_child(row):
            return False
        return (self.grid.GetCellValue(row + 1, self.COL_ON) == '1'
                and self.grid.GetCellValue(row + 1, self.COL_CMD) != '')

    def _on_nest_marked(self, event):
        """Set to exactly (row above)+1. Refuses if this row has a child
        (see _has_child), if the row above is already at MAX_DEPTH, or if
        the row is already at the target depth (no-op — e.g. already
        sitting one level under its parent)."""
        for row in sorted(self.grid.GetSelectedRows()):
            if self._row_frozen(row):
                continue   # defensive — entry point already filters these out
            if row == 0 or self._has_child(row):
                continue
            if self._row_in_link_box(row):
                continue   # nesting a link-box member would break the
                           # box's same-depth invariant — menu already
                           # grays this, enforced here too
            if self._selection_touches_nonnestable([row]):
                continue   # script or non-counter control row, as the
                           # row itself OR as the would-be parent above —
                           # menu grays this too; enforced here so batch
                           # selections and future entry points can't
                           # slip past it
            above_depth, _ = self._struct_get(row - 1)
            if above_depth >= self.MAX_DEPTH:
                continue   # refuse — row above is already maxed out
            depth, linked = self._struct_get(row)
            if depth == above_depth + 1:
                continue   # already there — nothing to do
            self._struct_set(row, above_depth + 1, linked)
        self.grid.ForceRefresh()

    def _on_unnest_marked(self, event):
        """Full reset to depth 0, not a -1 decrement. 'Unnest' means take
        this row OUT of the nested structure entirely — a command
        running inside 3 of 4 loops instead of 4 isn't a state anyone
        deliberately wants; it's not a well-defined measurement intent.
        Still gated by _has_child, still processed bottom-to-top: select
        just the deepest row to reset only it (the row above then loses
        its child and becomes independently eligible); select the whole
        chain to flatten it completely in one press, since each reset
        immediately clears the row above's child status too."""
        for row in sorted(self.grid.GetSelectedRows(), reverse=True):
            if self._row_frozen(row):
                continue   # defensive — entry point already filters these out
            if self._has_child(row):
                continue
            _, linked = self._struct_get(row)
            self._struct_set(row, 0, linked)
        self.grid.ForceRefresh()

    def _chain_size_if_linked(self, row: int) -> int:
        """Hypothetical total row count of the link-box row would join if
        its linked flag were set True — walks up through the anchor and
        down through any rows already linked to it. Used to enforce
        MAX_LINK_CHAIN before actually setting the flag, and mirrored in
        the menu-visibility check so batch link actions can't bypass it."""
        depth, _ = self._struct_get(row)
        top = row
        while top > 0:
            prev_depth, prev_linked = self._struct_get(top - 1)
            if prev_depth != depth:
                break
            top -= 1
            if not prev_linked:
                break
        bottom = row
        n = self.grid.GetNumberRows()
        while bottom + 1 < n:
            next_depth, next_linked = self._struct_get(bottom + 1)
            if next_linked and next_depth == depth:
                bottom += 1
            else:
                break
        return bottom - top + 1

    def _link_menu_allowed(self, rows: list) -> bool:
        """Menu-gating mirror of _on_link_marked's own per-row checks —
        the Link item is grayed unless the selection would actually
        produce a valid box, so a disabled menu item is the ONLY 'no'
        the user ever gets (no silent no-ops from an enabled item,
        the user's grayed-not-hidden convention). rows is the selection
        (multi-select) or [clicked row] (single).

        Valid means: contiguous; every row that would GET the linked
        flag is a real device write row (_row_linkable_as_follower —
        not empty, not control, not script); the effective mother (top
        of the box the first row attaches to) is a device row or
        control_counter (_row_valid_as_anchor); everything at depth 0
        with no children (v1: linking and nesting don't combine); and
        the resulting box stays within MAX_LINK_CHAIN."""
        rows = sorted(rows)
        if len(rows) < 2:
            r = rows[0]
            if r == 0:
                return False
            followers = [r]
            attach_to = r - 1
        else:
            for i in range(1, len(rows)):
                if rows[i] != rows[i - 1] + 1:
                    return False
            followers = rows[1:]
            attach_to = rows[0]
        anchor_row = self._link_box_anchor(attach_to)
        if self._struct_get(anchor_row)[0] != 0 or self._struct_get(attach_to)[0] != 0:
            return False
        if any(self._struct_get(r)[0] != 0 for r in followers):
            return False
        if self._has_child(anchor_row) or any(self._has_child(r) for r in followers):
            return False
        if not self._row_valid_as_anchor(anchor_row):
            return False
        if any(not self._row_linkable_as_follower(r) for r in followers):
            return False
        # Box size if all of them join: the first follower's hypothetical
        # chain size already counts the existing box above it; each
        # further follower adds exactly one (contiguity checked above).
        if self._chain_size_if_linked(followers[0]) + (len(followers) - 1) > self.MAX_LINK_CHAIN:
            return False
        return True

    def _on_link_marked(self, event):
        """Only permitted between same-depth rows (siblings) and within
        MAX_LINK_CHAIN total — see class docstring / _chain_size_if_linked.
        Now ALSO enforces per-row what _link_menu_allowed gates in the
        menu (real-device follower, valid mother, depth 0, no children)
        — belt and suspenders, so no future entry point (keyboard
        shortcut, batch action) can create a box execution would have
        to reject.

        Single row selected: link it to whatever's directly above.
        Multiple rows selected: link each to the PREVIOUS SELECTED row
        only — NOT to whatever sits above the whole selection. Selecting
        rows 14-15 and clicking Link must produce a 2-row chain
        {14,15}, not pull row 13 in as an unintended anchor just because
        it happened to be directly above row 14.

        Note the SEMANTICS are anchor-only regardless of how the box
        was built: every follower's [%] is the box's TOP row's value —
        followers never follow each other (requirement: 'ensure that when you
        link 3 or 4 devices that ALL are linked to the mother and not
        amongst each other')."""
        sel = sorted(self.grid.GetSelectedRows())
        if len(sel) < 2:
            if not sel or sel[0] == 0:
                return
            row = sel[0]
            if self._row_frozen(row):
                return   # defensive — entry point already filters these out
            if not self._link_menu_allowed([row]):
                return
            depth, _ = self._struct_get(row)
            above_depth, _ = self._struct_get(row - 1)
            if above_depth != depth:
                return
            if self._chain_size_if_linked(row) > self.MAX_LINK_CHAIN:
                return
            self._struct_set(row, depth, True)
            self._apply_link_field_locks(row)
        else:
            for i in range(1, len(sel)):
                row, prev_row = sel[i], sel[i - 1]
                if self._row_frozen(row):
                    continue   # defensive — entry point already filters these out
                if row != prev_row + 1:
                    continue   # not contiguous — linking wouldn't be meaningful
                if not self._row_linkable_as_follower(row):
                    continue
                if not self._row_valid_as_anchor(self._link_box_anchor(prev_row)):
                    continue
                depth, _ = self._struct_get(row)
                prev_depth, _ = self._struct_get(prev_row)
                if depth != 0 or prev_depth != depth:
                    continue
                if self._has_child(row):
                    continue
                if self._chain_size_if_linked(row) > self.MAX_LINK_CHAIN:
                    continue
                self._struct_set(row, depth, True)
                self._apply_link_field_locks(row)
        self._revalidate_all_rows()   # a newly-linked follower's Start may hold an
                                       # expression — its exemption just changed
        self.grid.ForceRefresh()

    def _on_unlink_marked(self, event):
        for row in self.grid.GetSelectedRows():
            if self._row_frozen(row):
                continue   # defensive — entry point already filters these out
            depth, was_linked = self._struct_get(row)
            self._struct_set(row, depth, False)
            if was_linked:
                # requirement: 'remember to allow / release them once it is
                # unlinked' — Final/Steps/IntTime become editable again.
                self._release_link_field_locks(row)
        self._revalidate_all_rows()   # an unlinked row loses its follower exemption:
                                       # a leftover f([%]) expression in Start is now a
                                       # non-numeric value on an ordinary row and is
                                       # correctly painted (it genuinely can't run)
        self.grid.ForceRefresh()

    # -----------------------------------------------------------------
    # Toolbar: Start/Stop/Pause, Load/Save, LaLM, Simulate
    # -----------------------------------------------------------------
    def _on_start_stop(self, event):
        """Execution now covers: constant-value writes and real device
        Start/Final/Steps sweeps (each optionally rate-limited, or
        verified against a linked read — see TaskRunnerThread.
        _run_timed_sweep / _get_command_method), control_counter as the
        virtual version of the same sweep engine, control_pause (a real
        wait), control_userprompt (modal 'Continue'/'Abort' popup,
        blocks until answered), control_stop (acts exactly like a
        manual Stop click), nested chains of 2+ rows at strictly
        increasing depth as genuine nested for-loops (_run_nested_
        sweep), every active read/query command (per-step for a sweep/
        nested chain, not after a plain constant write — see
        _run_timed_sweep's docstring for why not), onhold-on-abort
        for any active device that has one, and link boxes: same-depth
        rows linked below an anchor ('mother') row execute as followers
        — at every step of the mother's sweep (or once, for a constant
        mother) each follower writes the value of the expression in its
        Constant/Start cell with [%] = the mother's setpoint (see
        _run_timed_sweep / _run_constant_with_followers /
        _gather_link_box). An ORPHANED follower (anchor inactive or
        absent) is passed over, like an orphaned nested row.

        Runs on a background thread (TaskRunnerThread) — a real one,
        not the earlier wx.Yield()-based stopgap, which was wrong. The
        thread fetches each row LIVE as it reaches it (see
        TaskRunnerThread and _fetch_row_state_for_thread) rather than
        working from a plan built here — this method's only jobs are a
        preflight sanity check (is there anything to do at all right
        now) and resetting the frontier to 0, closing the race window
        between clicking Start and the thread's first live fetch.

        Sweeps through EVERY row from the top, not just the eligible
        ones: an ineligible row (inactive, empty, linked, or an
        orphaned nested row not preceded by its parent) still gets
        visually greyed + blocked as the scan passes it — requirement: 'you
        always start at row 1... if row 1 is not active, you still grey
        it out and block.' This is a TEMPORARY state, not the
        permanent completed-lock: it reverts to white (and unblocked)
        for every such row once the whole run finishes or is stopped
        (_on_run_finished). A row that actually executes successfully
        still gets the existing PERMANENT lock via _mark_completed —
        that's the historical-record behavior requested earlier and
        this doesn't touch it."""
        if self._thread is not None and self._thread.is_alive():
            self._thread.request_abort()
            self.btn_start.SetLabel('Stopping...')
            self.btn_start.Disable()
            return

        # Same reasoning as _fetch_row_state_for_thread: if a cell is
        # still in its in-place editor right now (typed a value, then
        # clicked Start directly without pressing Enter/Tab or clicking
        # a different cell first), GetCellValue() below and the first
        # live fetch could both see the value from BEFORE this edit.
        self.grid.SaveEditControlValue()

        if self.get_device_stem is None or self.on_log is None:
            wx.MessageBox('Not wired up yet.', 'Cannot start', wx.OK | wx.ICON_ERROR)
            return
        stem = self.get_device_stem()
        if stem is None:
            wx.MessageBox('No device list loaded — load one in Devices & Commands first.',
                          'Cannot start', wx.OK | wx.ICON_WARNING)
            return

        # Preflight only — a cheap "is there anything to do right now"
        # check. The actual run doesn't use this result for anything;
        # TaskRunnerThread re-evaluates every row live via the same
        # _evaluate_row_eligibility, fresh, right before using it.
        n = self.grid.GetNumberRows()
        if not any(self._evaluate_row_eligibility(r)['eligible'] for r in range(n)):
            wx.MessageBox('No eligible active rows to run.', 'Nothing to do', wx.OK | wx.ICON_INFORMATION)
            return

        # Snapshotted once here — see the TaskRunnerThread class
        # docstring for why query_specs stays a one-time snapshot while
        # row data does not: the Devices tree stays fully frozen for
        # the whole run, so nothing could change this even if re-polled.
        query_specs = self.get_query_specs() if self.get_query_specs else []
        onhold_devices = self.get_onhold_devices() if self.get_onhold_devices else []
        active_devices = self.get_active_devices() if self.get_active_devices else None

        self._passed_over_rows = []
        self._run_frontier = 0   # closes the Start-click race: row 0 is
                                  # spoken for immediately, before the
                                  # thread's first live fetch even runs
        self.btn_start.SetLabel('Stop')
        self.btn_load.Disable()   # visual cue; _on_load has the real guard
        self._thread = TaskRunnerThread(self, stem, query_specs, onhold_devices,
                                        active_devices=active_devices)
        self._thread.start()

    def _show_userprompt_dialog(self, message: str, result_holder: dict, done_event):
        """Called via wx.CallAfter from TaskRunnerThread._show_userprompt
        (control_userprompt). Modal, blocks the main thread's own event
        loop until answered — requirement: 'incorporate control_userprompt as
        a popup window that requires the user to press continue.'

        Abort is an addition beyond the literal request: this is a lab-
        automation tool driving real hardware (including a magnet), and
        a modal dialog blocks interaction with everything behind it,
        including the toolbar's own Stop button — without a way to stop
        from HERE, a user who needs to abort during a prompt would have
        no way to do it until answering the prompt first. Reuses
        _on_start_stop for that path — the exact same request_abort()
        call and UI update ('Stopping...', disabled) a real Stop-button
        click makes, not a separate, lesser abort behavior. The dialog's
        own close ('X') button is treated the same as Abort, not
        Continue — an accidental dismiss should never silently mean
        'proceed.'"""
        dlg = wx.Dialog(self, title='User Prompt', style=wx.DEFAULT_DIALOG_STYLE)
        sizer = wx.BoxSizer(wx.VERTICAL)
        text = wx.StaticText(dlg, label=message)
        text.Wrap(self.FromDIP(380))
        sizer.Add(text, 0, wx.ALL, self.FromDIP(PAD_LARGE))
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_abort = wx.Button(dlg, wx.ID_CANCEL, label='Abort')
        btn_continue = wx.Button(dlg, wx.ID_OK, label='Continue')
        btn_row.AddStretchSpacer()
        btn_row.Add(btn_abort, 0, wx.RIGHT, self.FromDIP(PAD_SMALL))
        btn_row.Add(btn_continue, 0)
        sizer.Add(btn_row, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_LARGE))
        dlg.SetSizerAndFit(sizer)
        btn_continue.SetDefault()
        answer = dlg.ShowModal()
        dlg.Destroy()
        if answer == wx.ID_OK:
            result_holder['continue'] = True
        else:
            result_holder['continue'] = False
            self._on_start_stop(None)   # same path a real Stop click takes
        done_event.set()

    def _pause_ui_start(self):
        """Called via wx.CallAfter from TaskRunnerThread — runs on the
        main thread."""
        self.gauge.Show(True)
        self.time_label.Show(True)
        self._elapsed_accum = 0.0
        self._segment_start = time.time()
        self.time_label.SetLabel('00:00:00')
        self._gauge_timer.Start(80)
        self._time_timer.Start(1000)
        self.Layout()

    def _pause_ui_stop(self):
        """Called via wx.CallAfter from TaskRunnerThread."""
        self._gauge_timer.Stop()
        self._time_timer.Stop()
        self.gauge.Show(False)
        self.gauge.SetValue(0)
        self.time_label.Show(False)
        self._segment_start = None
        self.Layout()

    def _while_ui_start(self):
        """Called via wx.CallAfter from TaskRunnerThread._run_while_loop.
        Pause's indeterminate pulse + elapsed clock (an unbounded poll
        loop has no total to drive the gauge in determinate mode), PLUS
        the step-progress label pause doesn't use — the runner pushes a
        running poll count into it ('(alias: poll 14)') via the existing
        _sweep_ui_step_progress, since the determinate [i/n] bracket
        doesn't map onto an open-ended count."""
        self.gauge.Show(True)
        self.time_label.Show(True)
        self.step_progress_label.Show(True)
        self.step_progress_label.SetLabel('')
        self._elapsed_accum = 0.0
        self._segment_start = time.time()
        self.time_label.SetLabel('00:00:00')
        self._gauge_timer.Start(80)
        self._time_timer.Start(1000)
        self.Layout()

    def _while_ui_stop(self):
        """Called via wx.CallAfter from TaskRunnerThread._run_while_loop
        — _pause_ui_stop's teardown plus the step-progress label."""
        self._gauge_timer.Stop()
        self._time_timer.Stop()
        self.gauge.Show(False)
        self.gauge.SetValue(0)
        self.time_label.Show(False)
        self.step_progress_label.Show(False)
        self.step_progress_label.SetLabel('')
        self._segment_start = None
        self.Layout()

    def _sweep_ui_start(self, n_total: int):
        """Called via wx.CallAfter from TaskRunnerThread._run_timed_sweep
        (counter or real device). This was missing entirely before —
        the counter ran with no visible progress indication at all.
        Unlike pause's indeterminate Pulse() (used because we
        genuinely don't know how long a pause or a plain write will
        take), a timed sweep DOES know its total step count up front,
        so this drives the SAME gauge widget in determinate mode
        instead: real range/value, no _gauge_timer pulsing. The
        elapsed-time label reuses _time_timer exactly as pause does."""
        self.gauge.SetRange(max(n_total, 1))
        self.gauge.SetValue(0)
        self.gauge.Show(True)
        self.time_label.Show(True)
        self.step_progress_label.Show(True)
        self.step_progress_label.SetLabel('')
        self._elapsed_accum = 0.0
        self._segment_start = time.time()
        self.time_label.SetLabel('00:00:00')
        self._time_timer.Start(1000)
        self.Layout()

    def _sweep_ui_progress(self, steps_done: int):
        """Called via wx.CallAfter from TaskRunnerThread._run_timed_sweep
        after every completed step."""
        self.gauge.SetValue(min(steps_done, self.gauge.GetRange()))

    def _wait_status_show(self, text: str):
        """Called via wx.CallAfter from TaskRunnerThread._write_with_
        rate_limit (an actual ramp in progress) or _verify_equals (an
        actual verify wait in progress) — requirement: 'it appears as if
        qmeas is frozen because there is no progress... add a static
        text.' Bold/accent-colored (set once at construction), shown/
        hidden rather than created fresh each time so there's no
        flicker between consecutive ramps/verifies in the same run."""
        self.wait_status_label.SetLabel(text)
        self.wait_status_label.Show(True)
        self.wait_status_label.GetContainingSizer().Layout()

    def _wait_status_hide(self):
        """Called via wx.CallAfter once a ramp or verify wait completes
        (success, failure, or abort — see the try/finally around each
        in TaskRunnerThread)."""
        self.wait_status_label.Show(False)
        self.wait_status_label.SetLabel('')
        self.wait_status_label.GetContainingSizer().Layout()

    def _sweep_ui_step_progress(self, text: str):
        """Called via wx.CallAfter from TaskRunnerThread every time ANY
        level of the currently-running sweep/nested chain advances to a
        new step — see _run_timed_sweep and _run_nested_level. text is
        already fully formatted, e.g.
        '(magnet_stepfield[2/6], 2450keithley1_setvoltage[13/21])' —
        built there, not here, since only the runner knows the actual
        per-level aliases/step counts."""
        self.step_progress_label.SetLabel(text)
        self.step_progress_label.GetContainingSizer().Layout()

    def _sweep_ui_stop(self):
        """Called via wx.CallAfter from TaskRunnerThread._run_timed_sweep
        once the loop ends (finished, aborted, or a write failure)."""
        self._time_timer.Stop()
        self.gauge.Show(False)
        self.gauge.SetValue(0)
        self.gauge.SetRange(100)   # restore pause's default indeterminate range
        self.time_label.Show(False)
        self.step_progress_label.Show(False)
        self.step_progress_label.SetLabel('')
        self._segment_start = None
        self.Layout()

    def _on_row_start(self, row):
        """Called via wx.CallAfter right before TaskRunnerThread actually
        processes this row. Highlights it (yellow) and temporarily locks
        its value/comment columns against editing — the snapshot taken
        when Start was pressed already means an edit wouldn't change
        what gets sent for this run, but letting the user edit a row
        mid-execution and have it silently do nothing is misleading, not
        harmless.

        Remembers the PRIOR read-only state per column before
        overriding it, so _restore_row_after_execution can put it back
        exactly — correct for both a plain row (was editable, becomes
        editable again) and a control command like control_userprompt
        (was already permanently locked by _apply_control_field_locks,
        must STAY locked, not become editable just because it ran).
        Keyed by row (see _run_nested_sweep's _on_chain_start): more
        than one row can be 'active' simultaneously for the whole
        duration of a nested chain, and a flat (non-row-keyed) dict here
        would have the LAST row's saved state silently overwrite every
        earlier row's — caught and fixed while building nesting, not
        shipped as a latent bug for whenever nesting was added."""
        self._active_row = row
        cols = (self.COL_START, self.COL_FINAL, self.COL_STEPS,
                self.COL_INTTIME, self.COL_COMMENT)
        self._row_start_readonly_state[row] = {col: self.grid.IsReadOnly(row, col) for col in cols}
        for col in range(self.grid.GetNumberCols()):
            self.grid.SetCellBackgroundColour(row, col, COLOR_ACTIVE_ROW)
        for col in cols:
            self.grid.SetReadOnly(row, col, True)
        self.grid.ForceRefresh()

    def _restore_row_after_execution(self, row):
        """Undo the temporary lock from _on_row_start, restoring
        whatever read-only state existed before. Background color is
        NOT touched here — the caller decides that (grey-and-locked for
        a success via _mark_completed, or back to normal for a
        failure via _clear_row_highlight)."""
        state = self._row_start_readonly_state.pop(row, {})
        for col, was_readonly in state.items():
            self.grid.SetReadOnly(row, col, was_readonly)
        if self._active_row == row:
            self._active_row = None

    def _on_chain_start(self, rows: list):
        """Same as _on_row_start, applied to every row in a nested
        chain at once (see TaskRunnerThread._run_nested_sweep) — the
        whole block is 'the active row(s)' for the duration of its
        execution, not just its outermost anchor."""
        for row in rows:
            self._on_row_start(row)

    def _on_chain_done(self, rows: list, log_entry: str):
        """Same visual effect as _on_row_done, applied to every row in
        a nested chain — but the log entry is appended ONCE here, not
        once per row (calling _on_row_done itself once per row would
        duplicate the same message across the whole chain)."""
        self.on_log(log_entry)
        for row in rows:
            self._restore_row_after_execution(row)
            self._mark_completed(row)
        self.grid.ForceRefresh()

    def _on_chain_failed(self, rows: list, log_entry: str):
        """Same as _on_chain_done, for the failure/abort case — see
        _on_row_failed for why a failed row stays editable rather than
        getting permanently locked."""
        self.on_log(log_entry)
        for row in rows:
            self._restore_row_after_execution(row)
            self._clear_row_highlight(row)
        self.grid.ForceRefresh()

    def _clear_row_highlight(self, row):
        for col in range(self.grid.GetNumberCols()):
            self.grid.SetCellBackgroundColour(row, col, COLOR_PANEL_BG)

    def _on_row_pass_over(self, row):
        """Called via wx.CallAfter from TaskRunnerThread for a row the
        scan passes but doesn't execute (inactive, empty, nested/linked,
        or a sweep) — requirement: 'you always start at row 1... if row 1 is
        not active, you still grey it out and block.' Light grey
        (COLOR_PASSED_OVER, distinct from the darker permanent
        COLOR_BORDER used for an actually-completed row) and fully
        blocked, same lock pattern as _on_row_start. TEMPORARY: reverted
        by _on_run_finished, not permanent like a real completion."""
        cols = (self.COL_ON, self.COL_START, self.COL_FINAL, self.COL_STEPS,
                self.COL_INTTIME, self.COL_COMMENT)
        prior_state = {col: self.grid.IsReadOnly(row, col) for col in cols}
        self._passed_over_rows.append((row, prior_state))
        for col in range(self.grid.GetNumberCols()):
            self.grid.SetCellBackgroundColour(row, col, COLOR_PASSED_OVER)
        for col in cols:
            self.grid.SetReadOnly(row, col, True)
        self.grid.ForceRefresh()

    def _on_row_done(self, row, log_entry):
        """Called via wx.CallAfter from TaskRunnerThread — the only
        place a background thread's per-row success touches the grid,
        and it does so on the main thread, as required."""
        self.on_log(log_entry)
        self._restore_row_after_execution(row)
        self._mark_completed(row)   # overrides both readonly + background — a
        self.grid.ForceRefresh()    # successful row is always fully locked+grey,
                                    # regardless of its pre-execution state

    def _on_row_failed(self, row, log_entry):
        """Called via wx.CallAfter from TaskRunnerThread on a per-row
        failure. Unlike success, a failed row is NOT marked completed —
        it stays editable (or stays locked, if it's a control command
        that was already permanently locked) for retry, matching the
        existing established behavior for failures."""
        self.on_log(log_entry)
        self._restore_row_after_execution(row)
        self._clear_row_highlight(row)
        self.grid.ForceRefresh()

    def _stash_sweep_data(self, headers: list, data_rows: list, filepath):
        """Shared by _on_sweep_progress (during a run, filepath=None —
        the file doesn't exist yet) and _on_sweep_done (end of run,
        real filepath). Converts to a plain numeric matrix — cells that
        don't parse as float (a query's error string) are left as the
        original string rather than dropped, so a row's shape stays
        consistent even if one channel misbehaved. Multi-value reads no
        longer arrive here as one unparseable space-joined string — see
        TaskRunnerThread._run_timed_sweep, which explodes them into
        real columns before this is ever called; that's what actually
        fixes a Y series silently going all-NaN, not this conversion
        step."""
        def to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return v
        matrix = [[to_float(v) for v in row_values] for row_values in data_rows]
        self.last_sweep_data = {'headers': headers, 'matrix': matrix, 'filepath': filepath}
        if self.on_sweep_data_ready is not None:
            self.on_sweep_data_ready()

    def _on_sweep_progress(self, headers: list, data_rows: list):
        """Called via wx.CallAfter from TaskRunnerThread._run_timed_sweep
        PERIODICALLY DURING a counter or real-device sweep run
        (throttled to at most 2/sec — see LIVE_PUSH_INTERVAL there),
        not only once at the end. requirement: opening the Graph window
        mid-run showed nothing selectable (nothing had ever finished
        yet), and once something WAS selected, it stayed stuck showing
        whatever existed at that moment for the rest of the run's
        duration, only catching up once the row fully completed. Both
        were the direct consequence of the graph only ever being told
        about new data at the very end — this makes it live instead.
        filepath is None here (deliberately — the file doesn't exist
        until the run finishes), so a screenshot taken mid-run falls
        back to the generic 'counter_run'/'sweep_run' name (see
        GraphWindow._on_screenshot) rather than the eventual real
        filename, which isn't known yet either."""
        self._stash_sweep_data(headers, data_rows, filepath=None)

    def _on_sweep_done(self, headers: list, data_rows: list, filepath):
        """Called via wx.CallAfter from TaskRunnerThread._run_timed_sweep
        once the whole row finishes (success or abort), in addition to
        (not instead of) the usual _on_row_done/_on_row_failed for the
        row itself. Same stashing as the live progress updates, just
        with the real, now-written data file path."""
        self._stash_sweep_data(headers, data_rows, filepath)

    def _on_run_finished(self, n_executed, n_skipped):
        """Called via wx.CallAfter from TaskRunnerThread when the whole
        run (or an abort) completes. Releases EVERY row — both the
        temporarily passed-over ones AND ones that actually executed
        successfully. Confirmed via screenshot: the task list
        needs to be re-runnable after finishing, not frozen forever —
        supersedes the earlier 'permanent historical lock, never
        unlock' framing from the completed-row request. Control-command
        rows (control_userprompt/stop/pause) keep their OWN permanent
        field locks via _relock_control_row, which never touches cell
        values — unlike _apply_control_field_locks, which would reset a
        real pause duration back to its default."""
        for row, prior_state in self._passed_over_rows:
            self._clear_row_highlight(row)
            for col, was_readonly in prior_state.items():
                self.grid.SetReadOnly(row, col, was_readonly)
        self._passed_over_rows = []

        for row in range(self.grid.GetNumberRows()):
            if not self._is_completed(row):
                continue
            self.grid.SetCellValue(row, self.COL_COMPLETED, '0')
            self._clear_row_highlight(row)
            alias = self.grid.GetCellValue(row, self.COL_CMD)
            device_name, _, command_name = alias.partition('_')
            for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS,
                       self.COL_INTTIME, self.COL_COMMENT):
                self.grid.SetReadOnly(row, col, False)
            self._relock_control_row(row, device_name, command_name)

        self._thread = None
        self._run_frontier = -1
        self.wait_status_label.Show(False)   # defensive — the try/finally in
        self.wait_status_label.SetLabel('')  # _write_with_rate_limit/_verify_equals should
                                              # already have cleared this, but a run ending any
                                              # other way shouldn't leave it stuck on screen
        self.btn_start.SetLabel('Start')
        self.btn_start.Enable()
        self.btn_load.Enable()
        self._revalidate_all_rows()   # _clear_row_highlight above wiped ALL cell
                                       # backgrounds, including validation paint —
                                       # restore it so a flagged row stays flagged
                                       # after the run that skipped it
        self.grid.ForceRefresh()
        if n_skipped:
            wx.MessageBox(f'{n_executed} command(s) sent, {n_skipped} skipped/failed.\nSee Log for details.',
                          'Done (with skips)', wx.OK | wx.ICON_WARNING)
        else:
            wx.MessageBox(f'{n_executed} command(s) sent.', 'Done', wx.OK | wx.ICON_INFORMATION)

    def _on_pause(self, event):
        paused = self.btn_pause.GetLabel() == 'Pause'   # about to pause
        self.btn_pause.SetLabel('Resume' if paused else 'Pause')
        if paused:
            self._gauge_timer.Stop()   # paused means not actively progressing
            self._time_timer.Stop()
            if self._segment_start is not None:
                self._elapsed_accum += time.time() - self._segment_start
                self._segment_start = None
        else:
            self._gauge_timer.Start(80)
            self._segment_start = time.time()
            self._time_timer.Start(1000)

    def _on_gauge_pulse(self, event):
        self.gauge.Pulse()

    def _on_time_tick(self, event):
        total = self._elapsed_accum
        if self._segment_start is not None:
            total += time.time() - self._segment_start
        h, rem = divmod(int(total), 3600)
        m, s = divmod(rem, 60)
        self.time_label.SetLabel(f'{h:02d}:{m:02d}:{s:02d}')

    def _on_destroy(self, event):
        if self._gauge_timer.IsRunning():
            self._gauge_timer.Stop()
        if self._time_timer.IsRunning():
            self._time_timer.Stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.request_abort()
        event.Skip()

    def _on_save(self, event):
        """Plain pipe-delimited ASCII text, matching v1's own file
        convention (device/command .txt files) rather than JSON."""
        with wx.FileDialog(self, 'Save task list', wildcard='Text files (*.txt)|*.txt',
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            with open(path, 'w', encoding='ascii', errors='replace') as f:
                f.write('|'.join(TASK_COLUMN_NAMES) + '\n')
                for r in range(self.grid.GetNumberRows()):
                    f.write('|'.join(self._snapshot_row(r)) + '\n')
        except Exception as e:
            wx.MessageBox(f'Could not save:\n{e}', 'Save failed', wx.OK | wx.ICON_ERROR)

    def _on_clear(self, event):
        """requirement: 'a button above the task list called clear to just
        clear it and only leave one empty row.' Same run-in-progress
        guard as Load (clearing out from under a running thread would
        be worse than loading over it — the thread is actively reading
        row values live). Confirmed first — this is a one-click way to
        lose an entire constructed task list, unlike Load (which at
        least replaces it with another real file) or a single row
        delete (which only affects one row)."""
        if self.run_active():
            wx.MessageBox('Cannot clear the task list while a run is in progress.',
                          'Run in progress', wx.OK | wx.ICON_WARNING)
            return
        if self.grid.GetNumberRows() <= 1 and not self.grid.GetCellValue(0, self.COL_CMD):
            return   # already empty, nothing to confirm or do
        if wx.MessageBox('Clear the entire task list?', 'Clear task list',
                         wx.YES_NO | wx.ICON_WARNING) != wx.YES:
            return
        if self.grid.GetNumberRows() > 0:
            self.grid.DeleteRows(0, self.grid.GetNumberRows())
        self.grid.AppendRows(1)
        self.grid.ForceRefresh()

    def _on_load(self, event):
        if self.run_active():
            wx.MessageBox('Cannot load a task list while a run is in progress — '
                          'it would replace the grid the run is executing from.',
                          'Run in progress', wx.OK | wx.ICON_WARNING)
            return
        with wx.FileDialog(self, 'Load task list', wildcard='Text files (*.txt)|*.txt',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        try:
            with open(path, 'r', encoding='ascii', errors='replace') as f:
                lines = [ln.rstrip('\r\n') for ln in f if ln.strip() != '']
            rows = [ln.split('|') for ln in lines[1:]]   # skip header line
        except Exception as e:
            wx.MessageBox(f'Could not load:\n{e}', 'Load failed', wx.OK | wx.ICON_ERROR)
            return
        if self.grid.GetNumberRows() > 0:
            self.grid.DeleteRows(0, self.grid.GetNumberRows())
        self.grid.AppendRows(len(rows))
        for r, values in enumerate(rows):
            for c, v in enumerate(values):
                self.grid.SetCellValue(r, c, v)
            self.grid.SetReadOnly(r, self.COL_ON, True)
            self.grid.SetReadOnly(r, self.COL_CMD, True)
            # Re-establish per-row field locks the saved file can't
            # carry (read-only flags aren't persisted, only values):
            # a loaded FOLLOWER row gets Final/Steps/IntTime locked
            # again (values untouched — _relock pattern, not _apply,
            # so nothing in the file is destroyed), and a loaded
            # control/script row gets its own locks back too — the
            # latter was a pre-existing gap: before this, a loaded
            # control_userprompt row's supposedly-locked fields were
            # freely editable.
            _, r_linked = self._struct_get(r)
            if r_linked:
                for col in (self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
                    self.grid.SetReadOnly(r, col, True)
            r_alias = self.grid.GetCellValue(r, self.COL_CMD)
            if r_alias:
                r_device, _, r_command = r_alias.partition('_')
                self._relock_control_row(r, r_device, r_command)
        self._revalidate_all_rows()   # a loaded file can contain the same
                                       # inconsistencies as typed input
        self._ensure_trailing_blank()   # loaded lists get the phantom row too
        self.grid.ForceRefresh()

    def _on_lalm(self, event):
        wx.MessageBox("LaLM assistant — not wired up yet.",
                      'LaLM', wx.OK | wx.ICON_INFORMATION)

    def _simulate_row_description(self, stem, device_name: str, command_name: str,
                                  start_str: str, final_str: str, steps_str: str):
        """Returns (description, n_values) for one row's value/command
        preview — description is the part that goes after '{row_label}:
        {alias} ' for a standalone row, or after a nested chain's own
        per-level label; n_values is how many distinct values this row
        represents (1 for a fixed/constant row, Steps+1 for a sweep) —
        needed by the nested-chain case to compute a total combination
        count. Shared by both the standalone-row and nested-chain
        simulate paths so a chain level is described identically to how
        the same row would be described standalone — extracted from
        what was previously _on_simulate's own inline per-row logic,
        with no change in behavior for the standalone case (verified
        against the original inline version before this was pulled
        out). Raises on error (missing command definition, missing
        Start/Final for Steps>=1, bad number) — same errors the
        standalone path always surfaced."""
        fields = _find_command_fields(stem, device_name, command_name)
        if fields is None:
            raise RuntimeError('command definition not found')
        cmdstr = fields[4]
        has_placeholder = '[%]' in cmdstr
        is_trivial_cmd = cmdstr.strip() == '[%]'   # e.g. control_counter — nothing beyond the value itself

        if not has_placeholder:
            return cmdstr, 1

        try:
            steps = int(steps_str) if steps_str.strip() else 0
        except ValueError:
            steps = 0

        if steps >= 1:
            if not start_str.strip() or not final_str.strip():
                raise RuntimeError('sweep needs both Constant/Start and Final')
            raw_values = _linspace(float(start_str), float(final_str), steps)
            value_labels = [f'{v:g}' for v in raw_values]
            shown_values = _abbreviate(value_labels)
            if is_trivial_cmd:
                return ', '.join(shown_values), len(raw_values)
            resolved = [_resolve_final_string(stem, device_name, command_name, vl)
                       for vl in value_labels]
            shown_cmds = _abbreviate(resolved)
            return f'{", ".join(shown_values)}, {", ".join(shown_cmds)}', len(raw_values)
        else:
            final_str_resolved = _resolve_final_string(stem, device_name, command_name, start_str)
            if is_trivial_cmd:
                return start_str.strip(), 1
            return f'{start_str.strip()}, {final_str_resolved}', 1

    def _on_simulate(self, event):
        """Preview only — no VISA I/O, no log entries, no completion
        marking. Same row-eligibility scope as real execution, now
        including nesting: a nested chain (2+ rows at strictly
        increasing depth, gathered via _has_child exactly like real
        execution's _gather_nested_chain, just synchronously — no
        background thread here to need the live-fetch machinery for)
        gets previewed as one block: each level's own value sequence
        (via _simulate_row_description, same helper a standalone row
        uses) plus the total combination count across all levels. A
        chain containing pause/userprompt/stop is an error here too,
        matching _run_nested_sweep's own rejection of those as chain
        members ('no sweep values to loop over'). A link box (anchor +
        same-depth linked followers directly below) is previewed as one
        block: the mother's value sequence plus every follower's
        expression evaluated at each mother value — the exact numbers
        execution will write. An ORPHANED linked row (anchor inactive
        or absent) is shown as skipped, matching real execution's
        pass-over. Standalone control_userprompt/stop get
        a plain-English description ('shows popup', 'acts as pressing
        Stop') instead of their literal command string, which is just
        an unused placeholder ('0') for both and would otherwise be
        actively misleading rather than merely uninformative. A
        standalone row's own Steps>1 expands into the actual value
        sequence (linspace Start->Final), abbreviated for long
        sequences: first 2, '...', last (requirement: 'you don't have to show
        all')."""
        if self.get_device_stem is None:
            wx.MessageBox('Not wired up yet.', 'Cannot simulate', wx.OK | wx.ICON_ERROR)
            return
        stem = self.get_device_stem()
        if stem is None:
            wx.MessageBox('No device list loaded — load one in Devices & Commands first.',
                          'Cannot simulate', wx.OK | wx.ICON_WARNING)
            return

        lines = []
        row = 0
        n_rows = self.grid.GetNumberRows()
        while row < n_rows:
            if self.grid.GetCellValue(row, self.COL_ON) != '1':
                row += 1
                continue
            alias = self.grid.GetCellValue(row, self.COL_CMD)
            if not alias:
                row += 1
                continue
            row_label = row + 1   # matches the grid's own 1-indexed row labels
            depth, linked = self._struct_get(row)
            if linked:
                # Reached directly = orphan: its anchor row above is
                # inactive, empty, or structurally absent — a box with
                # a live anchor is consumed whole below, followers
                # included, before the scan ever lands here. Same
                # semantics as real execution's pass-over.
                lines.append(f'{row_label}: {alias}: SKIPPED — linked row not preceded '
                             f'by its (active) anchor')
                row += 1
                continue
            if depth > 0:
                # Should have been consumed already as part of a chain
                # gathered from a shallower row above (see the
                # has_child branch below) — reaching it directly means
                # it wasn't: a sibling structure, same as real
                # execution's own handling of this.
                lines.append(f'{row_label}: {alias}: SKIPPED — nested row not preceded by its parent')
                row += 1
                continue

            device_name, _, command_name = alias.partition('_')

            if device_name == STANDARD_DEVICE_NAME and command_name == 'userprompt':
                comment = self.grid.GetCellValue(row, self.COL_COMMENT)
                message = comment.strip() if comment.strip() else 'Continue?'
                lines.append(f'{row_label}: {alias} — shows popup {message!r}, blocks until Continue/Abort')
                row += 1
                continue
            if device_name == STANDARD_DEVICE_NAME and command_name == 'stop':
                lines.append(f'{row_label}: {alias} — acts as pressing Stop (aborts the run)')
                row += 1
                continue
            if device_name == SCRIPT_DEVICE_NAME:
                # Same reasoning as userprompt/stop above: a script
                # command has no _find_command_fields entry at all (no
                # commands file — see load_devfile), so the generic
                # standalone path below would just raise 'command
                # definition not found'. Say what it actually does
                # instead.
                lines.append(f'{row_label}: {alias} — runs custom/{command_name}.py via exec() '
                             f'(no sandboxing; a script error fails this row, run continues)')
                row += 1
                continue
            if device_name == STANDARD_DEVICE_NAME and command_name not in _RESERVED_CONTROL_COMMAND_NAMES:
                # A while-command (or a legacy dead generic entry on
                # control) — described plainly, mirroring execution's
                # _run_while_loop, including its structural rejections
                # (any while failure aborts the whole run there). The
                # generic path below would misdescribe it as a write.
                d = _get_while_definition(stem, command_name)
                if d is None:
                    lines.append(f'{row_label}: {alias}: FAILED — not a valid while-command '
                                 f'(no exit condition defined); aborts the run')
                    row += 1
                    continue
                if self._has_active_child(row):
                    # _has_ACTIVE_child, matching execution: a while row
                    # whose only child is deactivated runs standalone.
                    lines.append(f"{row_label}: {alias}: FAILED — a while row can't have "
                                 f"nested rows under it (not supported); aborts the run")
                    row += 1
                    continue
                w_follow = 0
                fr = row + 1
                while fr < n_rows:
                    fr_depth, fr_linked = self._struct_get(fr)
                    if fr_linked and fr_depth == depth:
                        w_follow += 1
                        fr += 1
                    else:
                        break
                if w_follow:
                    lines.append(f"{row_label}: {alias}: FAILED — a while row can't be a "
                                 f"link-box mother ({w_follow} linked row(s) below); "
                                 f"aborts the run")
                    row += 1 + w_follow   # consume the box, same as execution would
                    continue
                inttime_raw = self.grid.GetCellValue(row, self.COL_INTTIME)
                try:
                    poll_s = _parse_duration_seconds(inttime_raw) if inttime_raw.strip() else 0.0
                except ValueError:
                    poll_s = -1.0
                if poll_s < 0.1:
                    lines.append(f'{row_label}: {alias}: FAILED — Integration Time '
                                 f'{inttime_raw.strip()!r} invalid for a while row '
                                 f'(poll interval, minimum 0.1s); aborts the run')
                    row += 1
                    continue
                try:
                    t = _parse_duration_seconds(d['timeout_raw'])
                    timeout_txt = 'no timeout' if t == 0 else f'timeout {t:g}s (aborts the run)'
                except ValueError:
                    timeout_txt = f"invalid timeout {d['timeout_raw']!r} — FAILS"
                lines.append(f"{row_label}: {alias} — polls "
                             f"{d['linked_device']}_{d['linked_read']} every {poll_s:g}s, "
                             f"recording all active queries, until its value "
                             f"{d['operator']} {d['threshold'].strip()!r}; {timeout_txt}")
                row += 1
                continue

            # Link box: this row is the anchor if the row directly
            # below is linked at the same depth. Previewed as one
            # block, mirroring execution: mother's own value sequence
            # (same _simulate_row_description as standalone), then each
            # follower's expression EVALUATED at every mother value
            # (abbreviated) — exactly the numbers _write_follower_step
            # will produce, so a wrong equation is visible here, not on
            # the hardware. First error aborts the box preview, same
            # pattern as a nested chain (execution fails the whole box
            # too).
            box_follow_rows = []
            fr = row + 1
            while fr < n_rows:
                fr_depth, fr_linked = self._struct_get(fr)
                if fr_linked and fr_depth == depth:
                    box_follow_rows.append(fr)
                    fr += 1
                else:
                    break
            if box_follow_rows:
                box_lines = []
                box_error = None
                mother_values = []
                start_str = self.grid.GetCellValue(row, self.COL_START)
                final_str = self.grid.GetCellValue(row, self.COL_FINAL)
                steps_str = self.grid.GetCellValue(row, self.COL_STEPS)
                try:
                    desc, n_values = self._simulate_row_description(
                        stem, device_name, command_name, start_str, final_str, steps_str)
                    box_lines.append(f'    mother: {alias} {desc}')
                    try:
                        steps_n = int(steps_str) if steps_str.strip() else 0
                    except ValueError:
                        steps_n = 0
                    if steps_n >= 1:
                        mother_values = _linspace(float(start_str), float(final_str), steps_n)
                    else:
                        try:
                            mother_values = [float(start_str)]
                        except ValueError:
                            raise RuntimeError(
                                f'Constant/Start {start_str!r} is not a number — a '
                                f'link-box mother needs a numeric value for its '
                                f'followers to evaluate [%] against')
                except Exception as e:
                    box_error = f'{alias} (row {row + 1}): {e}'

                if box_error is None:
                    for fr in box_follow_rows:
                        fr_label = fr + 1
                        fr_alias = self.grid.GetCellValue(fr, self.COL_CMD)
                        fr_on = self.grid.GetCellValue(fr, self.COL_ON) == '1'
                        if not fr_alias or not fr_on:
                            box_lines.append(f'    linked (row {fr_label}): '
                                             f'{fr_alias or "(empty)"} — inactive, skipped')
                            continue
                        fr_device, _, fr_command = fr_alias.partition('_')
                        if fr_device in (STANDARD_DEVICE_NAME, SCRIPT_DEVICE_NAME):
                            box_error = (f'{fr_alias} (row {fr_label}): control/script '
                                         f'commands cannot be linked followers')
                            break
                        fr_fields = _find_command_fields(stem, fr_device, fr_command)
                        if fr_fields is None:
                            box_error = f'{fr_alias} (row {fr_label}): command definition not found'
                            break
                        if '[%]' not in fr_fields[4]:
                            box_error = (f'{fr_alias} (row {fr_label}): command has no [%] '
                                         f'placeholder — a fixed command string cannot '
                                         f'follow the mother value')
                            break
                        fr_expr = self.grid.GetCellValue(fr, self.COL_START)
                        try:
                            computed = [_eval_link_expression(fr_expr, mv) for mv in mother_values]
                        except Exception as e:
                            box_error = f'{fr_alias} (row {fr_label}): {e}'
                            break
                        shown = _abbreviate([f'{v:g}' for v in computed])
                        box_lines.append(f'    linked: {fr_alias} = {fr_expr} -> {", ".join(shown)}')

                box_span = f'{row + 1}-{box_follow_rows[-1] + 1}'
                if box_error is not None:
                    lines.append(f'{box_span}: LINK BOX: ERROR — {box_error}')
                else:
                    lines.append(f'{box_span}: LINK BOX ({len(mother_values)} steps, '
                                 f'followers set after each mother step):')
                    lines.extend(box_lines)
                row += 1 + len(box_follow_rows)
                continue

            if self._has_active_child(row):
                # _has_ACTIVE_child in both the trigger and the
                # continuation, matching run()'s dispatch and
                # _gather_nested_chain exactly: a deactivated child
                # ends the chain before it — the parent above it is
                # described standalone (or as a shorter chain), the
                # deactivated row is silently omitted (Simulate's
                # standing treatment of every off row, top of loop),
                # and anything deeper shows as 'SKIPPED — nested row
                # not preceded by its parent', same as execution's
                # orphan pass-over.
                chain_rows = [row]
                cur = row
                while self._has_active_child(cur):
                    cur += 1
                    chain_rows.append(cur)

                chain_lines = []
                chain_error = None
                total = 1
                for r in chain_rows:
                    r_alias = self.grid.GetCellValue(r, self.COL_CMD)
                    r_device, _, r_command = r_alias.partition('_')
                    if (r_device == STANDARD_DEVICE_NAME and r_command != 'counter') \
                            or r_device == SCRIPT_DEVICE_NAME:
                        # Widened from ('pause', 'userprompt', 'stop') —
                        # see _run_nested_sweep's matching check: while-
                        # commands are rejected as chain members too.
                        what = f"'{r_command}'" if r_device == STANDARD_DEVICE_NAME else 'a script'
                        chain_error = (f"{r_alias} (row {r + 1}): {what} has no sweep values "
                                      f"and can't be part of a nested chain")
                        break
                    r_start = self.grid.GetCellValue(r, self.COL_START)
                    r_final = self.grid.GetCellValue(r, self.COL_FINAL)
                    r_steps = self.grid.GetCellValue(r, self.COL_STEPS)
                    try:
                        desc, n_values = self._simulate_row_description(
                            stem, r_device, r_command, r_start, r_final, r_steps)
                        total *= max(n_values, 1)
                        chain_lines.append(f'    {r_alias} {desc}')
                    except Exception as e:
                        chain_error = f'{r_alias} (row {r + 1}): {e}'
                        break

                chain_span = f'{chain_rows[0] + 1}-{chain_rows[-1] + 1}'
                if chain_error is not None:
                    lines.append(f'{chain_span}: NESTED CHAIN: ERROR — {chain_error}')
                else:
                    lines.append(f'{chain_span}: NESTED CHAIN ({total} total measurements):')
                    lines.extend(chain_lines)
                row += len(chain_rows)
                continue

            start_str = self.grid.GetCellValue(row, self.COL_START)
            final_str = self.grid.GetCellValue(row, self.COL_FINAL)
            steps_str = self.grid.GetCellValue(row, self.COL_STEPS)
            try:
                desc, _n_values = self._simulate_row_description(
                    stem, device_name, command_name, start_str, final_str, steps_str)
                lines.append(f'{row_label}: {alias} {desc}')
            except Exception as e:
                lines.append(f'{row_label}: {alias}: ERROR — {e}')
            row += 1

        if not lines:
            wx.MessageBox('No active rows to simulate.', 'Simulate', wx.OK | wx.ICON_INFORMATION)
            return

        with SimulationPreviewDialog(self, '\n'.join(lines)) as dlg:
            dlg.ShowModal()

    def _on_execute_row(self, event):
        wx.MessageBox("Single-row execution isn't wired up yet — TaskRunnerThread "
                      "exists and handles this today via Start, just not from this menu item.",
                      'Execute Row', wx.OK | wx.ICON_INFORMATION)

    # -----------------------------------------------------------------
    def _get_empty_selected_row(self):
        """If exactly one row is selected and its Command cell is empty,
        return its index — that's the fill target. Otherwise None (no
        selection, multiple rows selected, or the selected row already
        has a command — all ambiguous, so fall back to append)."""
        sel = self.grid.GetSelectedRows()
        if len(sel) != 1:
            return None
        row = sel[0]
        return row if self.grid.GetCellValue(row, self.COL_CMD) == '' else None

    def _get_insert_above_row(self):
        """If exactly ONE row is selected, it already has a command
        (empty ones are the fill path), and it is not frozen by the
        live run scan — return its index as the insert-above target.
        None otherwise (no selection, MULTIPLE rows selected, or the
        row is at/above the run frontier, i.e. currently executing,
        already executed, or part of the claimed nest/link chain) —
        every None falls back to plain appending at the bottom."""
        sel = self.grid.GetSelectedRows()
        if len(sel) != 1:
            return None
        row = sel[0]
        if self.grid.GetCellValue(row, self.COL_CMD) == '':
            return None
        if self._row_frozen(row):
            return None
        return row

    def _apply_control_field_locks(self, row, device, command):
        """control_userprompt/control_stop take no value at all: lock
        Constant/Start, Final, Steps, Integration Time. control_pause
        takes exactly one (auto-defaulted to '1' in Constant/Start,
        which stays editable if a different pause value is wanted);
        everything else locked. Conditions/Comments are left alone —
        general annotation fields, not sweep parameters, EXCEPT for
        control_userprompt specifically: with Constant/Start locked
        empty, Comment doubles as the actual message text shown in the
        popup (TaskRunnerThread.run(), 'userprompt' branch) — the only
        editable free-text field left on that row. Blank Comment falls
        back to a generic 'Continue?' at execution time, not enforced
        here.

        Every script_* command gets the same treatment as userprompt/
        stop — requirement: 'no parameters allowed other than comment' — this
        applies uniformly to every .py file (unlike control, where the
        lock pattern varies by which of the 4 fixed commands it is),
        since there's no per-script configuration at all, just whether
        to run it."""
        if device == SCRIPT_DEVICE_NAME:
            for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
                self.grid.SetCellValue(row, col, '')
                self.grid.SetReadOnly(row, col, True)
            return
        if device != STANDARD_DEVICE_NAME:
            return
        if command in ('userprompt', 'stop'):
            for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
                self.grid.SetCellValue(row, col, '')
                self.grid.SetReadOnly(row, col, True)
        elif command == 'pause':
            self.grid.SetCellValue(row, self.COL_START, '1')
            for col in (self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
                self.grid.SetCellValue(row, col, '')
                self.grid.SetReadOnly(row, col, True)
        elif command not in _RESERVED_CONTROL_COMMAND_NAMES:
            # A while-command: Integration Time IS its one parameter
            # (the poll interval, per-use like a counter's — minimum
            # 0.1s, enforced at execution) and stays editable, seeded
            # with a sensible default; Start/Final/Steps have no
            # meaning (a while has no sweep values). Detected by NAME
            # (not by reading fields 19-23 off disk): with the generic
            # Add Command entry removed from control's menu, every
            # non-reserved control command the UI can create is a
            # while; a legacy dead generic entry getting the same locks
            # is harmless — it can't execute either way. counter is in
            # the reserved set, so its all-fields-editable behavior is
            # untouched by this elif.
            self.grid.SetCellValue(row, self.COL_INTTIME, '1s')
            for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS):
                self.grid.SetCellValue(row, col, '')
                self.grid.SetReadOnly(row, col, True)

    def _relock_control_row(self, row, device, command):
        """Same lock PATTERN as _apply_control_field_locks, but never
        touches cell values — needed when releasing a row after
        execution: _apply_control_field_locks would reset a real pause
        duration (e.g. '10') back to its default '1' and blank
        Final/Steps/IntTime, which is correct when a command is first
        added but destructive when just re-establishing which columns
        stay locked after a run finishes."""
        if device == SCRIPT_DEVICE_NAME:
            for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
                self.grid.SetReadOnly(row, col, True)
            return
        if device != STANDARD_DEVICE_NAME:
            return
        if command in ('userprompt', 'stop'):
            for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
                self.grid.SetReadOnly(row, col, True)
        elif command == 'pause':
            for col in (self.COL_FINAL, self.COL_STEPS, self.COL_INTTIME):
                self.grid.SetReadOnly(row, col, True)
        elif command not in _RESERVED_CONTROL_COMMAND_NAMES:
            # while-command — same lock pattern as in
            # _apply_control_field_locks, values untouched (a real poll
            # interval in IntTime must survive a run, exactly like a
            # real pause duration does).
            for col in (self.COL_START, self.COL_FINAL, self.COL_STEPS):
                self.grid.SetReadOnly(row, col, True)

    def add_task(self, device, command):
        """Appending (the common case — no row selected, or nothing
        empty selected) is always safe during a run: a brand new row's
        index is by construction past every row that exists yet, hence
        past the frontier, hence live-picked-up by TaskRunnerThread's
        next fetch once the scan reaches it (see class docstring —
        nothing here is pre-snapshotted). Filling an EXISTING empty
        placeholder row is only safe if that target hasn't since become
        frozen (the scan could have caught up to it since it was
        selected) — falls back to appending instead of silently doing
        nothing."""
        target = self._get_empty_selected_row()
        if target is not None and not self._row_frozen(target):
            # Fill the placeholder in place — On/Struct (nest depth,
            # linked) are left exactly as they were. That's the point:
            # build the structure with empty rows first, populate
            # commands after.
            was_last = (target == self.grid.GetNumberRows() - 1)
            self.grid.SetCellValue(target, self.COL_CMD, f'{device}_{command}')
            self._apply_control_field_locks(target, device, command)
            self._validate_row_fields(target)   # values may pre-exist in the placeholder
            if was_last:
                # The trailing blank was just consumed: recreate it and
                # WALK THE SELECTION DOWN onto the new blank, so
                # repeated double-clicks build the list top-to-bottom
                # (selection staying put would make the next command
                # insert ABOVE this one — list built in reverse).
                blank = self._ensure_trailing_blank()
                self.grid.ClearSelection()
                self.grid.SelectRow(blank)
            self.grid.ForceRefresh()
            return
        insert_at = self._get_insert_above_row()
        if insert_at is not None:
            # Exactly one (non-empty, non-frozen) row selected: insert
            # the new command ABOVE it instead of appending. Safe
            # mid-run for the same reason _on_insert_rows is: the
            # frontier guard (checked in _get_insert_above_row) refuses
            # rows at/above the live scan — and because a nested/linked
            # chain is claimed whole the moment it starts (see
            # _fetch_row_state_for_thread and the chain fetch), 'below
            # the frontier' can never land inside the executing group.
            # New row is flat (depth 0, unlinked), same as every other
            # insert path; restructuring around it is the user's call.
            self.grid.InsertRows(insert_at, 1)
            self._blankify_row(insert_at)
            self.grid.SetCellValue(insert_at, self.COL_CMD, f'{device}_{command}')
            self._apply_control_field_locks(insert_at, device, command)
            self.grid.ForceRefresh()
            return
        # Alias naming matches v1's all_commands convention: "{device}_{command}".
        # Land in the trailing blank when it exists and isn't frozen;
        # otherwise genuinely append (e.g. mid-run with the blank
        # already claimed by the scan — the new row past the frontier is
        # always safe). Either way the trailing-blank invariant is
        # restored afterwards.
        row = self.grid.GetNumberRows() - 1
        if not (row >= 0 and self.grid.GetCellValue(row, self.COL_CMD) == ''
                and not self._row_frozen(row)):
            row = self.grid.GetNumberRows()
            self.grid.AppendRows(1)
            self._blankify_row(row)
        self.grid.SetCellValue(row, self.COL_CMD, f'{device}_{command}')
        self._apply_control_field_locks(row, device, command)
        self._ensure_trailing_blank()

    def on_device_toggled(self, device_name: str, is_on: bool):
        """Called from DevicesPanel when a device's LED is clicked. Now
        tracks every device's current state (not just off-transitions,
        as before) so activation can be BLOCKED while a device is off —
        not just auto-deactivated once and then immediately
        re-activatable by hand right after. Turning a device back on
        does NOT reactivate tasks that already got auto-deactivated —
        still a deliberate asymmetry: you decide what runs, the device
        switch only ever takes things away or blocks them, never adds
        back on its own.

        Matches by exact prefix "{device_name}_". Device names are
        restricted to [a-z0-9]+ (no underscores possible), so a device
        name can never itself contain the separator — the only
        remaining edge case is one device name being a literal prefix
        of another (e.g. 'dev1' and 'dev10'), narrow enough not to
        worry about."""
        if self.run_active():
            return   # frozen — defensive; the tree toggle itself is
                     # also blocked at the source during a run
        self._device_states[device_name] = is_on
        if is_on:
            return
        prefix = f'{device_name}_'
        changed = False
        for row in range(self.grid.GetNumberRows()):
            if (self.grid.GetCellValue(row, self.COL_CMD).startswith(prefix)
                    and self.grid.GetCellValue(row, self.COL_ON) == '1'):
                self.grid.SetCellValue(row, self.COL_ON, '0')
                changed = True
        if changed:
            self.grid.ForceRefresh()

    def _device_is_active(self, alias: str) -> bool:
        """True unless we know for certain this alias's device is off.
        Defaults to True for a device we haven't seen a toggle event
        for yet — don't block activation based on absence of
        information."""
        device_name, _, _ = alias.partition('_')
        return self._device_states.get(device_name, True)


# =========================================================================
# Settings dialog
# =========================================================================

class SettingsDialog(wx.Dialog):
    """Data file path + suffix. Real, working — pure config data, no
    hardware involved, same reasoning as Load/Save being real rather
    than placeholders."""

    def __init__(self, parent, settings: dict):
        super().__init__(parent, title='Settings', size=wx.Size(520, 220))

        path_label = wx.StaticText(self, label='Data file path:')
        self.path_ctrl = wx.TextCtrl(self, value=settings['data_path'])
        browse_btn = wx.Button(self, label='Browse...')
        browse_btn.Bind(wx.EVT_BUTTON, self._on_browse)

        suffix_label = wx.StaticText(self, label='Data file suffix:')
        self.suffix_ctrl = wx.TextCtrl(self, value=settings['file_suffix'])
        suffix_hint = wx.StaticText(self,
            label='Appended to every file from the same measurement run, '
                  'so they visibly group together (e.g. sort adjacently by name).')
        suffix_hint.SetForegroundColour(COLOR_TEXT_MUTED)
        suffix_hint.Wrap(self.FromDIP(460))

        path_row = wx.BoxSizer(wx.HORIZONTAL)
        path_row.Add(self.path_ctrl, 1, wx.EXPAND | wx.RIGHT, self.FromDIP(PAD_SMALL))
        path_row.Add(browse_btn, 0)

        grid = wx.FlexGridSizer(2, 2, self.FromDIP(PAD_SMALL), self.FromDIP(PAD_SMALL))
        grid.AddGrowableCol(1)
        grid.Add(path_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(path_row, 1, wx.EXPAND)
        grid.Add(suffix_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.suffix_ctrl, 1, wx.EXPAND)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(grid, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_LARGE))
        main.Add(suffix_hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(PAD_LARGE))
        main.Add(self.CreateButtonSizer(wx.OK | wx.CANCEL), 0,
                wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        self.SetSizerAndFit(main)

    def _on_browse(self, event):
        with wx.DirDialog(self, 'Choose data folder',
                          defaultPath=self.path_ctrl.GetValue()) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.path_ctrl.SetValue(dlg.GetPath())

    def get_settings(self) -> dict:
        return {'data_path': self.path_ctrl.GetValue(),
                'file_suffix': self.suffix_ctrl.GetValue()}


# =========================================================================
# Graph window — deliberately NOT an AUI pane; separate top-level window
# =========================================================================

# =========================================================================
# Log pane — real, not a placeholder
# =========================================================================

class LogPanel(wx.Panel):
    """Mirrors v1's AddToLog/text_logginglist convention exactly:
    {data_path}/logfile.txt, appended plain text, shown live in the GUI
    at the same time. Layout of individual log entries can differ from
    v1 (requirement: 'you can use a different layout for the logfile') — only
    the file location and append-plus-mirror mechanism is preserved."""

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(COLOR_PANEL_BG)
        self.text_ctrl = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.text_ctrl, 1, wx.EXPAND | wx.ALL, self.FromDIP(PAD_SMALL))
        self.SetSizer(sizer)

    def append_log(self, text: str):
        self.text_ctrl.AppendText(text)
        settings = load_settings()
        data_path_str = settings.get('data_path', '')
        if not data_path_str:
            return
        data_path = Path(data_path_str)
        try:
            data_path.mkdir(parents=True, exist_ok=True)
            with open(data_path / 'logfile.txt', 'a', encoding='utf-8') as f:
                f.write(text)
        except Exception:
            pass   # logging failure shouldn't block the measurement itself


def _wx_colour_to_hex(colour: wx.Colour) -> str:
    """Convert a wx.Colour (0-255 int channels) to a matplotlib-
    compatible hex string. matplotlib color arguments need hex strings
    or 0-1 floats — NOT a wx.Colour object and NOT a 0-255 tuple.
    Passing a wx.Colour straight through (as GraphWindow._draw_empty
    originally did with COLOR_TEXT_MUTED) makes matplotlib try to
    interpret its 0-255-range channels as already being in the [0,1]
    range it expects, raising 'RGBA values should be within 0-1
    range' — confirmed the hard way, not hypothetically."""
    return '#{:02x}{:02x}{:02x}'.format(colour.Red(), colour.Green(), colour.Blue())


def _prettify_header(name: str) -> str:
    """Turn a raw data-file column name into a display label for the
    Graph window's X-list. The single-level sweep's own three special
    columns get their established exact wording; anything else
    (nested-sweep columns like 'outer_count'/'inner1_value', or a plain
    read alias) just gets underscores turned into spaces and the first
    letter capitalized — good enough for 'Outer count'/'Inner1 value'
    without needing a name for every possible nesting depth."""
    exact = {'counter_value': 'Counter value', 'device_value': 'Device value',
             'step_number': 'Step number', 'elapsed_s': 'Elapsed time (s)'}
    if name in exact:
        return exact[name]
    pretty = name.replace('_', ' ')
    return pretty[0].upper() + pretty[1:] if pretty else pretty


class GraphWindow(wx.Frame):
    """Plots TasksPanel.last_sweep_data (headers + numeric matrix from
    the most recent control_counter OR real-device Steps>=1 sweep run —
    see TaskRunnerThread._run_timed_sweep, which handles both). Separate
    top-level window, not an AUI pane — by design, meant to live on a
    second monitor during a run (see module docstring).

    Built on matplotlib (FigureCanvasWxAgg + NavigationToolbar2Wx)
    rather than hand-rolled wx.dc plotting: independent multi-axis
    overlays, zoom/pan, and image export are all built-in there and
    would otherwise mean reinventing a plotting library. See the
    defensive import at the top of this file — if matplotlib isn't
    installed, __init__ shows a plain message instead of crashing.

    X list (multi-select, capped at 2): step number, counter/device
    value, elapsed time, or any active read column — plotting one
    channel against another (e.g. a lock-in signal against a DAC
    read-back) is entirely ordinary and not restricted to the
    'structural' columns. The counter/device-value option's label
    switches between 'Counter value' and 'Device value' depending on
    which one produced the data (headers[0] is literally
    'counter_value' or 'device_value' — see refresh_data). Checking a
    SECOND X switches to a 2D map (_draw_2d/_build_2d_grid) — requirement:
    'select two x values to plot y(x1,x2) to obtain a 2D plot,' most
    useful for nested-sweep data where the two X's actually vary
    together as a genuine grid (e.g. outer_value vs inner1_value), but
    not restricted to nested data specifically — any two columns can be
    picked, they just won't form a meaningful grid unless they actually
    do vary together that way. One pcolormesh subplot per selected Y,
    each with its own colorbar; the mode radios (overlay/separate-
    canvas) are hidden in this mode since neither applies to stacking
    color maps. A missing (x1,x2) combination — an aborted/incomplete
    sweep — becomes a NaN gap rather than crashing or mis-shaping the
    grid.
    Y list (multi-select, capped at MAX_DATASETS): read columns first,
    then the structural columns — everything selectable on both axes
    (requirement: 'I want to be able to plot everything against everything').
    Nothing is pre-selected — the canvas stays blank
    ('Select X and Y...') until the user picks something, by request:
    'Dont pre-select. Just dont show anything until the user selects.'

    Two display modes, chosen once >=1 Y is picked:
    - 'One canvas, separate y-axes': every selected Y against the
      shared X, each with its own y-axis/scale/color. twinx() gives a
      clean 2nd axis; a 3rd/4th need the y-axis spine manually offset
      outward (standard matplotlib pattern for >2 parasite axes, not
      wx-specific) — see _draw_overlay.
    - 'Separate canvas per dataset': one subplot per selected Y,
      sharex=True so a horizontal zoom/pan on one lines up with the
      rest — see _draw_stacked.

    No manual 'Refresh' button — requirement: 'I dont want to press the
    button to refresh. That should be done automatically, if I dont
    want to see a plot I either de-select or close the graph.' See
    refresh_data's docstring for the two automatic triggers this
    relies on instead, and _force_repaint for why canvas.draw() alone
    wasn't reliably reaching the screen.

    Zoom/pan: the standard NavigationToolbar2Wx (rectangle zoom, pan,
    home-to-reset). requirement: 'if possible, but not required' — this is the
    off-the-shelf mechanism rather than custom independent per-axis
    scroll-zoom, which would be extra work for a stated nice-to-have.
    One real limitation worth knowing in overlay mode: multiple
    twinx() axes occupy the same screen position, so a zoom-rectangle
    drag affects whichever one of them the click actually lands on
    (usually the most-recently-added), not all of them uniformly —
    switch to 'separate canvas' if that's confusing for a given plot.

    Screenshot button: NOT a literal screen-grab — figure.savefig(),
    which captures the plot at full quality with no window chrome or
    display-scaling artifacts, which is what you actually want here.
    Saved next to the run's own .dat file (requirement: 'automatically saved
    in the data folder'), named '{data file stem}_{n}.png' where n is
    chosen by checking what's already on disk (not an in-memory
    counter, which would collide with earlier screenshots after an app
    restart) — so it's really 'the next unused screenshot number for
    this run's data file', which is what 'the counter represents the
    number of screenshots' means in practice."""

    MAX_DATASETS = 4
    _COLORS = ['#1f77b4', '#d62728', '#2ca02c', '#9467bd']

    def __init__(self, parent, get_sweep_data):
        super().__init__(parent, title=f'{APP_NAME} — Graph', size=wx.Size(950, 650))
        self.SetBackgroundColour(COLOR_APP_BG)
        self.get_sweep_data = get_sweep_data
        self._headers, self._matrix, self._filepath = [], [], None
        self._x_cols, self._y_cols = [], []

        if not _HAS_MATPLOTLIB:
            panel = PlaceholderPanel(self, "Graph needs matplotlib.\n\npip install matplotlib")
            sizer = wx.BoxSizer(wx.VERTICAL)
            sizer.Add(panel, 1, wx.EXPAND)
            self.SetSizer(sizer)
            return

        x_label = wx.StaticText(self, label='X axis (1 = normal plot, 2 = 2D map):')
        self.x_list = wx.CheckListBox(self)
        self.x_list.Bind(wx.EVT_CHECKLISTBOX, self._on_x_checked)

        y_label = wx.StaticText(self, label=f'Y axis (up to {self.MAX_DATASETS}):')
        self.y_list = wx.CheckListBox(self)
        self.y_list.Bind(wx.EVT_CHECKLISTBOX, self._on_y_checked)

        self.status_label = wx.StaticText(self, label='')
        self.status_label.SetForegroundColour(COLOR_TEXT_MUTED)

        self.mode_overlay = wx.RadioButton(self, label='One canvas, separate y-axes', style=wx.RB_GROUP)
        self.mode_stacked = wx.RadioButton(self, label='Separate canvas per dataset')
        self.mode_overlay.SetValue(True)
        self.mode_overlay.Bind(wx.EVT_RADIOBUTTON, self._on_selection_changed)
        self.mode_stacked.Bind(wx.EVT_RADIOBUTTON, self._on_selection_changed)

        btn_screenshot = wx.Button(self, label='Screenshot')
        btn_screenshot.Bind(wx.EVT_BUTTON, self._on_screenshot)

        self.figure = Figure(figsize=(6, 5))
        self.canvas = FigureCanvasWxAgg(self, -1, self.figure)
        self.toolbar = _GraphToolbar(self.canvas)
        self.toolbar.Realize()

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(x_label, 0, wx.BOTTOM, self.FromDIP(PAD_SMALL))
        left.Add(self.x_list, 1, wx.EXPAND | wx.BOTTOM, self.FromDIP(PAD_LARGE))
        left.Add(y_label, 0, wx.BOTTOM, self.FromDIP(PAD_SMALL))
        left.Add(self.y_list, 1, wx.EXPAND | wx.BOTTOM, self.FromDIP(PAD_SMALL))
        left.Add(self.status_label, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(PAD_LARGE))
        left.Add(self.mode_overlay, 0, wx.BOTTOM, self.FromDIP(PAD_SMALL))
        left.Add(self.mode_stacked, 0, wx.BOTTOM, self.FromDIP(PAD_LARGE))
        left.Add(btn_screenshot, 0, wx.EXPAND)

        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(self.canvas, 1, wx.EXPAND)
        right.Add(self.toolbar, 0, wx.EXPAND)

        main = wx.BoxSizer(wx.HORIZONTAL)
        main.Add(left, 0, wx.EXPAND | wx.ALL, self.FromDIP(PAD_LARGE))
        main.Add(right, 1, wx.EXPAND)
        self.SetSizer(main)

        self._draw_empty()

    # -----------------------------------------------------------------
    def refresh_data(self):
        """Pulls the current TasksPanel.last_sweep_data. No manual
        button — purely automatic, by request: 'I dont want to press the
        button to refresh... if I dont want to see a plot I either
        de-select or close the graph.' Two triggers: (1) TasksPanel.
        on_sweep_data_ready (wired in QMeasMain._build_graph_window),
        which fires repeatedly DURING a counter or real-device sweep
        run now — throttled live pushes from TaskRunnerThread.
        _run_timed_sweep, not only once at the very end — REGARDLESS of
        whether this window is currently shown, so self._matrix is
        never actually stale, only possibly not yet painted to screen
        (see _force_repaint for that distinction); (2) once when the
        window is shown (QMeasMain._toggle_graph), to pick up anything
        that changed while it was hidden and force an actual repaint (a
        draw() that happened while hidden never reaches the screen —
        matplotlib's own gui_repaint no-ops for a hidden canvas).
        If the column set is UNCHANGED from last time (e.g. you just
        re-ran the same setup), keeps the current X/Y selection and
        mode and just redraws with the new numbers. If the columns
        differ (different active reads, or switched from counter to a
        real device or vice versa), resets both lists entirely — new
        columns showing an old, possibly-nonsensical selection would be
        worse than starting blank again."""
        if not _HAS_MATPLOTLIB:
            return
        data = self.get_sweep_data() if self.get_sweep_data else None
        if not data or not data.get('matrix'):
            self._headers, self._matrix, self._filepath = [], [], None
            self.x_list.Clear()
            self.y_list.Clear()
            self._draw_empty()
            return

        headers, matrix, filepath = data['headers'], data['matrix'], data['filepath']
        same_columns = (headers == self._headers)
        self._matrix, self._filepath = matrix, filepath

        if same_columns:
            self._headers = headers
            self._redraw()
            return

        self._headers = headers
        # 'elapsed_s' is always present, in BOTH the single-level sweep
        # format (counter_value/device_value, step_number, elapsed_s,
        # reads...) and the nested format (outer_count, outer_value,
        # inner1_count, inner1_value, ..., elapsed_s, reads...) — found
        # by NAME rather than assuming a fixed preamble width, so this
        # doesn't need separate logic per nesting depth. Everything up
        # to and including it is an X-axis candidate; everything after
        # is a read column (Y-axis only).
        elapsed_idx = headers.index('elapsed_s') if 'elapsed_s' in headers else len(headers) - 1
        read_headers = headers[elapsed_idx + 1:]

        is_single_level = (elapsed_idx == 2 and len(headers) > 1 and headers[1] == 'step_number'
                          and headers[0] in ('counter_value', 'device_value'))
        if is_single_level:
            # Preserve the exact existing reordering/labels for this
            # shape: step number, current value, elapsed time — the user's
            # originally requested order, not the raw column order.
            structural_x_cols = [1, 0, 2]
            value_label = 'Counter value' if headers[0] == 'counter_value' else 'Device value'
            structural_x_names = ['Step number', value_label, 'Elapsed time (s)']
        else:
            # Nested (2+ levels) or any other shape: raw column order —
            # already sensible (outer before inner, count before value,
            # by request) — with prettified labels instead of raw
            # snake_case.
            structural_x_cols = list(range(elapsed_idx + 1))
            structural_x_names = [_prettify_header(h) for h in headers[:elapsed_idx + 1]]

        # ALL columns are candidates on BOTH axes — requirement: 'I want to be
        # able to plot everything against everything.' The read columns
        # were originally in _x_cols here (their indices were appended)
        # but their LABELS never made it into x_names, so x_list.Set()
        # silently displayed only the structural entries and the reads
        # became unpickable — an off-by-a-list-length regression, not a
        # design decision (this class's own docstring promised 'or any
        # active read column' the whole time). Reads keep their raw
        # header names in both lists (matching what the Y list always
        # showed) so the same channel is recognizable as the same
        # channel; ordering keeps each list's familiar entries first —
        # X: structural then reads, Y: reads then structural — so
        # existing habits (X from the top, Y from the top) land on the
        # same entries as before.
        self._x_cols = structural_x_cols + list(range(elapsed_idx + 1, len(headers)))
        x_names = structural_x_names + read_headers
        self.x_list.Set(x_names)
        for i in range(self.x_list.GetCount()):
            self.x_list.Check(i, False)

        self._y_cols = list(range(elapsed_idx + 1, len(headers))) + structural_x_cols
        self.y_list.Set(read_headers + structural_x_names)
        for i in range(self.y_list.GetCount()):
            self.y_list.Check(i, False)

        self.status_label.SetLabel('')
        self._draw_empty()

    def _on_selection_changed(self, event):
        self._redraw()

    def _on_x_checked(self, event):
        idx = event.GetInt()
        checked = [i for i in range(self.x_list.GetCount()) if self.x_list.IsChecked(i)]
        if len(checked) > 2:
            self.x_list.Check(idx, False)   # revert the one that just got checked
            self.status_label.SetLabel('Maximum 2 X selections (pick 2 for a 2D map).')
            return
        self.status_label.SetLabel('')
        self._redraw()

    def _on_y_checked(self, event):
        idx = event.GetInt()
        checked = [i for i in range(self.y_list.GetCount()) if self.y_list.IsChecked(i)]
        if len(checked) > self.MAX_DATASETS:
            self.y_list.Check(idx, False)   # revert the one that just got checked
            self.status_label.SetLabel(f'Maximum {self.MAX_DATASETS} datasets at once.')
            return
        self.status_label.SetLabel('')
        self._redraw()

    @staticmethod
    def _to_plot_float(v):
        """A cell that isn't already numeric (a query's error string —
        split-mode reads no longer land here as an unparseable
        space-joined string; see TaskRunnerThread._run_timed_sweep,
        which now explodes them into real separate columns) becomes NaN
        rather than crashing the plot or silently dropping the point —
        matplotlib draws a gap for NaN, which is an honest
        representation of 'no valid reading here'."""
        return v if isinstance(v, (int, float)) else float('nan')

    def _redraw(self):
        if not _HAS_MATPLOTLIB or not self._matrix:
            self._draw_empty()
            return
        x_checked = [i for i in range(self.x_list.GetCount()) if self.x_list.IsChecked(i)]
        y_checked = [i for i in range(self.y_list.GetCount()) if self.y_list.IsChecked(i)]
        if not x_checked or not y_checked:
            self._draw_empty()
            return

        y_cols = [self._y_cols[i] for i in y_checked]
        is_2d = (len(x_checked) == 2)
        # The mode radios (overlay/separate-canvas) only mean anything
        # for the normal 1D line plot — requirement: pick 2 X's for a 2D map
        # instead, which always renders one subplot per Y (no
        # 'overlay' equivalent makes sense for stacking color maps).
        self.mode_overlay.Show(not is_2d)
        self.mode_stacked.Show(not is_2d)
        self.Layout()

        saved_view = self._capture_view()
        self.figure.clear()
        if is_2d:
            x1_col, x2_col = self._x_cols[x_checked[0]], self._x_cols[x_checked[1]]
            axes = self._draw_2d(x1_col, x2_col, y_cols)
        else:
            x_col = self._x_cols[x_checked[0]]
            x_data = [self._to_plot_float(row[x_col]) for row in self._matrix]
            if self.mode_overlay.GetValue():
                axes = self._draw_overlay(x_data, x_col, y_cols)
            else:
                axes = self._draw_stacked(x_data, x_col, y_cols)
        self._apply_view(saved_view, axes)
        self.figure.tight_layout()
        self._force_repaint()

    def _build_2d_grid(self, x1_col: int, x2_col: int, y_col: int):
        """Build a 2D grid for pcolormesh out of the flat measurement
        list: unique sorted values along each selected X column become
        the two axes, and y_col's value at each (x1,x2) combination
        fills the corresponding cell. A nested sweep's own measurement
        order (outer slowest, innermost fastest — see TaskRunnerThread.
        _run_nested_level) means this naturally forms a complete
        rectangular grid when the run finished normally. A combination
        with no matching row (the sweep was aborted partway through,
        or the two X's picked don't actually vary together in a
        regular grid) is left as NaN rather than crashing or silently
        mis-shaping the array — matplotlib draws a gap for NaN, an
        honest 'no data here' rather than a guess."""
        x1_vals = sorted({row[x1_col] for row in self._matrix if isinstance(row[x1_col], (int, float))})
        x2_vals = sorted({row[x2_col] for row in self._matrix if isinstance(row[x2_col], (int, float))})
        x1_index = {v: i for i, v in enumerate(x1_vals)}
        x2_index = {v: i for i, v in enumerate(x2_vals)}
        grid = [[float('nan')] * len(x2_vals) for _ in range(len(x1_vals))]
        for row in self._matrix:
            x1v, x2v = row[x1_col], row[x2_col]
            if x1v in x1_index and x2v in x2_index:
                grid[x1_index[x1v]][x2_index[x2v]] = self._to_plot_float(row[y_col])
        return x1_vals, x2_vals, grid

    def _draw_2d(self, x1_col: int, x2_col: int, y_cols: list):
        """One pcolormesh subplot per selected Y — requirement: 'select two x
        values to plot y(x1,x2) to obtain a 2D plot.' Each gets its own
        colorbar (a shared one wouldn't mean anything across datasets
        with different physical units/ranges)."""
        n = len(y_cols)
        axes = []
        x1_label = _prettify_header(self._headers[x1_col])
        x2_label = _prettify_header(self._headers[x2_col])
        for i, y_col in enumerate(y_cols):
            ax = self.figure.add_subplot(n, 1, i + 1)
            x1_vals, x2_vals, grid = self._build_2d_grid(x1_col, x2_col, y_col)
            y_label = _prettify_header(self._headers[y_col])
            if len(x1_vals) < 2 or len(x2_vals) < 2:
                # Not enough distinct values along one axis to form a
                # real 2D grid (e.g. the two X's picked don't actually
                # form a rectangular sweep together) — say so rather
                # than handing pcolormesh a degenerate 1xN/Nx1 grid.
                ax.set_axis_off()
                ax.text(0.5, 0.5, f"Not enough distinct values for a 2D grid\n({x1_label} x {x2_label})",
                       ha='center', va='center', transform=ax.transAxes, color=_wx_colour_to_hex(COLOR_TEXT_MUTED))
            else:
                mesh = ax.pcolormesh(x2_vals, x1_vals, grid, shading='auto')
                self.figure.colorbar(mesh, ax=ax, label=y_label)
                ax.set_xlabel(x2_label)
                ax.set_ylabel(x1_label)
                ax.set_title(y_label)
            axes.append(ax)
        return axes

    def _capture_view(self):
        """Snapshot the current view IF the user has manually zoomed/
        panned since the last Home press — detected via matplotlib's
        own documented side effect of set_xlim/set_ylim (which is what
        the zoom/pan tools call under the hood): it turns autoscale OFF
        for that axis. _GraphToolbar.home() explicitly turns it back
        ON, which is what makes Home mean 'resume auto-scaling to live
        data' rather than just 'jump back to one specific old view.'
        Returns None if there's nothing to preserve (no axes yet, or
        autoscale is still on) — the caller should just let the new
        plot autoscale normally in that case, which is what makes a
        live-growing plot keep expanding to show new data by default,
        right up until the user actually touches zoom/pan."""
        if not self.figure.axes:
            return None
        ax0 = self.figure.axes[0]
        if ax0.get_autoscalex_on():
            return None
        return {'xlim': ax0.get_xlim(),
                'ylims': [ax.get_ylim() for ax in self.figure.axes],
                'n_axes': len(self.figure.axes)}

    def _apply_view(self, saved_view, axes):
        """Restore a captured view onto freshly-rebuilt axes — only if
        the shape still matches (same number of axes as when it was
        captured). If the X/Y selection changed shape since then (a
        different number of Y's checked, say), the old view doesn't
        necessarily mean anything for the new plot, so it's dropped in
        favor of a fresh autoscale rather than applied somewhere
        arbitrary."""
        if saved_view is None or len(axes) != saved_view['n_axes']:
            return
        axes[0].set_xlim(saved_view['xlim'])
        for ax, ylim in zip(axes, saved_view['ylims']):
            ax.set_ylim(ylim)

    def _force_repaint(self):
        """canvas.draw() alone was not enough — see the two mechanisms
        this specifically works around:

        1. self.toolbar (NavigationToolbar2Wx) keeps its own view-
           history stack (for Home/Back/Forward/zoom) tied to the
           Axes objects it last saw. _redraw() calls figure.clear()
           and builds entirely NEW Axes every time — the toolbar can
           keep applying limits meant for axes that no longer exist,
           which looks exactly like 'the x-range is stuck at whatever
           it was the first time.' toolbar.update() tells it to drop
           that stale history and adopt the current axes fresh.
        2. matplotlib's own gui_repaint() (backend_wx.py) explicitly
           no-ops if the canvas isn't currently shown on screen
           ('if not (self and self.IsShownOnScreen()): return') — so
           a draw() that happens while this window is hidden never
           reaches the screen, and nothing later forces a repaint of
           that stale bitmap once the window reopens. canvas.Refresh()
           (a real wx.Window repaint request, independent of
           matplotlib's own internal gating) is the belt-and-suspenders
           fix for that.

        Both are safe to call unconditionally, on every redraw, whether
        or not either mechanism is actually the culprit in any given
        matplotlib/wx version — this doesn't need to know which one it
        is to fix both possibilities at once."""
        self.canvas.draw()
        self.toolbar.update()
        self.canvas.Refresh()

    def _draw_overlay(self, x_data, x_col, y_cols):
        """All selected Y against the shared X, one canvas, each with
        its own y-axis. twinx() for the 2nd axis; 3rd/4th get their
        right spine manually pushed further out so they don't overlap
        the 2nd — matplotlib has no built-in 'nth parasite axis'
        helper beyond two, this offset-spine approach is the standard
        workaround. No y-axis text labels — requirement: 'you see the legend
        and color, this is clear' — only the tick numbers stay
        color-coded to their series; the series name itself lives
        solely in the legend, not duplicated as an axis title too."""
        ax0 = self.figure.add_subplot(111)
        axes = [ax0]
        for i in range(1, len(y_cols)):
            axi = ax0.twinx()
            if i >= 2:
                axi.spines['right'].set_position(('axes', 1.0 + 0.15 * (i - 1)))
                axi.set_frame_on(True)
                axi.patch.set_visible(False)
            axes.append(axi)

        lines = []
        for ax, col, color in zip(axes, y_cols, self._COLORS):
            y_data = [self._to_plot_float(row[col]) for row in self._matrix]
            label = self._headers[col]
            line, = ax.plot(x_data, y_data, color=color, marker='o', markersize=3, label=label)
            ax.tick_params(axis='y', colors=color)
            lines.append(line)

        ax0.set_xlabel(self._headers[x_col])
        ax0.legend(lines, [ln.get_label() for ln in lines], loc='best')
        ax0.grid(True, alpha=0.3)
        return axes

    def _draw_stacked(self, x_data, x_col, y_cols):
        """One subplot per selected Y, sharex=True — a horizontal
        zoom/pan on any one of them lines up across all the others.
        No y-axis text label — same as overlay mode, the legend alone
        identifies each subplot; the y-axis shows values only."""
        n = len(y_cols)
        first_ax = None
        axes = []
        for i, col in enumerate(y_cols):
            ax = self.figure.add_subplot(n, 1, i + 1, sharex=first_ax)
            first_ax = first_ax or ax
            y_data = [self._to_plot_float(row[col]) for row in self._matrix]
            label = self._headers[col]
            ax.plot(x_data, y_data, color=self._COLORS[i % len(self._COLORS)],
                    marker='o', markersize=3, label=label)
            ax.legend(loc='best')
            ax.grid(True, alpha=0.3)
            if i < n - 1:
                ax.tick_params(labelbottom=False)
            axes.append(ax)
        self.figure.axes[-1].set_xlabel(self._headers[x_col])
        return axes

    def _draw_empty(self):
        if not _HAS_MATPLOTLIB:
            return
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_axis_off()
        ax.text(0.5, 0.5, 'Select X and at least one Y to plot',
                ha='center', va='center', transform=ax.transAxes, color=_wx_colour_to_hex(COLOR_TEXT_MUTED))
        self._force_repaint()

    def _on_screenshot(self, event):
        if not _HAS_MATPLOTLIB:
            return
        if not self._matrix:
            wx.MessageBox('No data loaded yet — nothing to screenshot.',
                          'Screenshot', wx.OK | wx.ICON_WARNING)
            return
        if self._filepath:
            folder = Path(self._filepath).parent
            base = Path(self._filepath).stem
        else:
            folder = Path(load_settings()['data_path'])
            base = 'sweep_run'
        n = 1
        while (folder / f'{base}_{n}.png').exists():
            n += 1
        out_path = folder / f'{base}_{n}.png'
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self.figure.savefig(out_path, dpi=150)
        except Exception as e:
            wx.MessageBox(f'Could not save screenshot:\n{e}', 'Screenshot failed', wx.OK | wx.ICON_ERROR)
            return
        wx.MessageBox(f'Saved: {out_path.name}', 'Screenshot saved', wx.OK | wx.ICON_INFORMATION)


# =========================================================================
# Main frame
# =========================================================================


# ======================================================================
# AI Assistant (View -> AI Assistant): a separate chat window backed by
# a LOCAL Ollama model, with qmeas_knowledge_base.md (same folder as
# qmeas.py) as its system-prompt knowledge. STRICTLY read-only: it
# answers questions and advises on experiment setup; it has no access
# to devices, files, or the task grid. All HTTP work happens on worker
# threads — a slow 72B token stream must never touch the measurement
# engine or block the UI. stdlib http.client only: no new dependency.
# ======================================================================

_OLLAMA_HOST = 'localhost'
_OLLAMA_PORT = 11434
_ASSISTANT_PREFERRED_MODEL = 'llama3.1:8b'   # manual's recommended pull (fits 8 GB, strong instruction-following)
# Ollama's DEFAULT context window is 4096 tokens — smaller than the
# knowledge base alone, so without an explicit num_ctx the KB gets
# silently truncated and the model answers from fragments. 16384
# comfortably holds KB + a long conversation; its KV-cache cost for a
# 7-8B model (~1 GB) still fits an 8 GB GPU alongside the weights.
# Low temperature: this is factual retrieval, not creative writing.
_ASSISTANT_NUM_CTX = 16384
_ASSISTANT_TEMPERATURE = 0.3
# Keep the model resident between questions (Ollama default unloads
# after 5 min idle — every return to the assistant would pay the full
# load again), and pre-warm it when the window opens so the load
# happens while the user types instead of after the first Send.
_ASSISTANT_KEEP_ALIVE = '1h'
_ASSISTANT_SYSTEM_PREFIX = (
    'You are the qmeas AI Assistant, embedded in the qmeas measurement '
    'software (View menu > AI Assistant). Answer questions about qmeas and '
    'help the user set up experiments: which device settings, commands, and '
    'task rows a measurement needs. You CANNOT control anything - you have '
    'no access to instruments, files, or the task list; you only advise, '
    'and the user acts in the qmeas UI. Be concise and concrete. If the '
    'answer is in the reference below, follow it exactly; if not, say you '
    'are unsure rather than inventing qmeas behavior.\n\n'
    '=== qmeas reference ===\n')


def _ollama_request(method, path, body=None, timeout=5.0):
    """One HTTP round trip to the local Ollama. Returns the
    HTTPResponse (caller reads/closes). Raises OSError-family on
    connection problems — callers translate that into ONE friendly
    status line, never a traceback at the user."""
    conn = http.client.HTTPConnection(_OLLAMA_HOST, _OLLAMA_PORT, timeout=timeout)
    payload = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'} if payload else {}
    conn.request(method, path, body=payload, headers=headers)
    return conn, conn.getresponse()


def _ollama_list_models():
    """Model names from GET /api/tags, or raises on unreachable."""
    conn, resp = _ollama_request('GET', '/api/tags')
    try:
        data = json.loads(resp.read().decode())
    finally:
        conn.close()
    return [m.get('name', '') for m in data.get('models', []) if m.get('name')]


def _ollama_chat_stream(model, messages, on_chunk, should_stop):
    """POST /api/chat with stream=True; call on_chunk(text) for every
    content fragment as it arrives (from the WORKER thread — the caller
    wraps UI updates in wx.CallAfter). should_stop() polled between
    chunks so closing the window abandons the stream promptly. Returns
    the full reply text."""
    conn, resp = _ollama_request(
        'POST', '/api/chat',
        {'model': model, 'messages': messages, 'stream': True,
         'keep_alive': _ASSISTANT_KEEP_ALIVE,
         'options': {'num_ctx': _ASSISTANT_NUM_CTX,
                     'temperature': _ASSISTANT_TEMPERATURE}},
        timeout=600.0)   # big models think slowly; per-read, not total
    full = []
    try:
        for raw in resp:
            if should_stop():
                break
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue   # partial/garbage line: skip, keep streaming
            piece = (obj.get('message') or {}).get('content', '')
            if piece:
                full.append(piece)
                on_chunk(piece)
            if obj.get('done'):
                break
    finally:
        conn.close()
    return ''.join(full)


def _ollama_warm(model, system_prompt):
    """Pre-warm = the two real first-question costs, paid early: (1)
    load the model into memory, (2) EVALUATE THE SYSTEM PROMPT (the
    knowledge base, ~9k tokens — the dominant first-question wait on a
    small GPU) so its KV prefix is cached. Sends the system message
    with num_predict=1 (evaluate prompt, generate one throwaway
    token); same num_ctx/keep_alive as real chats so this instance IS
    the one chats hit. Blocking — worker thread only."""
    conn, resp = _ollama_request(
        'POST', '/api/chat',
        {'model': model,
         'messages': [{'role': 'system', 'content': system_prompt}],
         'stream': False,
         'keep_alive': _ASSISTANT_KEEP_ALIVE,
         'options': {'num_ctx': _ASSISTANT_NUM_CTX, 'num_predict': 1}},
        timeout=300.0)
    try:
        resp.read()
    finally:
        conn.close()


USER_NOTES_FILENAME = 'qmeas_users.md'
_USER_NOTES_TEMPLATE = """\
# My qmeas setup notes

Notes about THIS lab's setup, for the AI Assistant to use when answering.
Anything you write here is given to the assistant alongside the general
qmeas reference. Edit freely; plain text / Markdown.

Examples of useful things to record (delete these and write your own):

- How to start each instrument bridge, with the exact command line, e.g.:
  - Laser source bridge (runs on the qmeas PC):
    py toptica_bridge.py --toptica-host 192.168.0.20
  - Cryostat bridge (runs on the cryostat PC 192.168.0.30):
    py opticool_qdi.py --qd-host 192.168.0.30
- Each instrument's address in qmeas, e.g.:
  - laser  -> TCPIP0::localhost::5100::SOCKET
  - magnet -> TCPIP0::192.168.0.10::7180::SOCKET
- Instrument names, ranges and quirks, e.g.:
  - magnet 'magnetz' goes to +/- 14 T; field read-back status word is HOLD.
  - lock-in 'lockin1' readvalues returns X (element 0) and Y (element 1).
- Any lab-specific procedure you want the assistant to remind you of.
"""


def _user_notes_path():
    return app_root() / USER_NOTES_FILENAME


def _ensure_user_notes():
    """Guarantee qmeas_users.md exists (so the 'User notes' button always
    has a file to open) — created from a helpful template on first run,
    never overwritten afterwards. Returns the path."""
    path = _user_notes_path()
    if not path.exists():
        try:
            path.write_text(_USER_NOTES_TEMPLATE, encoding='utf-8')
        except OSError:
            pass
    return path


EXAMPLES_FILENAME = 'qmeas_examples.md'


def _parse_examples(text):
    """Parse the examples file into (questions, answer) groups. Format:
    one or more 'Q: ...' lines (question variants) followed by an
    'A: ...' answer block, blocks separated by lines of dashes. Each
    group keeps ALL its question variants with ONE answer, so the
    few-shot injection can show the variants together without
    duplicating the (long) answer once per phrasing — saving context.
    Robust to blank lines and '---' separators; ignores malformed
    blocks rather than raising."""
    groups = []
    blocks = re.split(r'(?m)^\s*-{3,}\s*$', text)
    for block in blocks:
        lines = block.splitlines()
        questions, answer_lines, in_answer = [], [], False
        for ln in lines:
            if not in_answer and ln.strip().startswith('Q:'):
                questions.append(ln.split(':', 1)[1].strip())
            elif ln.strip().startswith('A:'):
                in_answer = True
                after = ln.split(':', 1)[1]
                if after.strip():
                    answer_lines.append(after.lstrip())
            elif in_answer:
                answer_lines.append(ln)
        answer = '\n'.join(answer_lines).strip()
        if questions and answer:
            groups.append((questions, answer))
    return groups


def _load_examples():
    """Few-shot pairs from qmeas_examples.md next to qmeas.py, or [] if
    absent/empty. These are injected as prior conversation turns (see
    _build_messages) so the model imitates the demonstrated answers —
    far more effective for a small model than rules in the system
    prompt."""
    try:
        text = (app_root() / EXAMPLES_FILENAME).read_text(encoding='utf-8')
    except OSError:
        return []
    return _parse_examples(text)


def _load_assistant_knowledge():
    """Build the assistant system prompt from TWO files next to qmeas.py:
    the general reference (qmeas_knowledge_base.md, shipped, how qmeas
    works) and the user's own notes (qmeas_users.md, this lab's setup —
    bridge start commands, instrument addresses, ranges). Each is a
    clearly-labelled section so the model knows which is general and
    which is this-specific-lab. A missing general file degrades
    gracefully; user notes are always present (auto-created)."""
    kb_path = app_root() / 'qmeas_knowledge_base.md'
    try:
        kb = kb_path.read_text(encoding='utf-8')
        found = True
    except OSError:
        kb = ('(qmeas_knowledge_base.md was NOT found next to qmeas.py — '
              'answer general questions but say clearly that the qmeas '
              'reference is missing.)')
        found = False

    prompt = _ASSISTANT_SYSTEM_PREFIX + kb
    try:
        notes = _ensure_user_notes().read_text(encoding='utf-8').strip()
    except OSError:
        notes = ''
    if notes:
        prompt += ('\n\n=== THIS LAB\'S SETUP (user notes — specific to this '
                   'installation; prefer these for addresses, bridge commands, '
                   'and instrument specifics) ===\n' + notes)
    return prompt, found


# --- Klaus, the quantum bubble ----------------------------------------
# Vector-drawn, timer-animated. States:
#   idle     — breathes (radius oscillates gently at center)
#   thinking — bounces around the panel until the answer arrives
#   error    — pops: a brief burst animation settling into droplets
# Physics/state live in _tick() with plain floats so the behavior is
# unit-testable without wx; painting is a thin layer on top.

_KLAUS_SIZE = 16 * 5              # logical panel side (DIP-scaled in __init__)
_KLAUS_R_BASE = 22.0              # bubble radius
_KLAUS_BREATH_AMPL = 0.09         # +-9% radius when breathing
_KLAUS_BREATH_STEP = 0.12         # phase advance per tick (idle)
_KLAUS_SPEED = 6.0                # px per tick while thinking
_KLAUS_POP_FRAMES = 8             # burst animation length
_KLAUS_COLOR = (40, 200, 180)     # teal, as Klaus's eyes were
_KLAUS_TICK_MS = 50
# Yawn: after a long stretch of uninterrupted idle breathing, Klaus
# opens a mouth and yawns, then goes back to breathing. Frame counts
# are in ticks (_KLAUS_TICK_MS each): ~24 s of calm, then a ~1.6 s yawn.
_KLAUS_YAWN_AFTER = 480          # idle ticks of breathing before a yawn
_KLAUS_YAWN_OPEN = 14            # ticks for the mouth to open
_KLAUS_YAWN_HOLD = 6             # ticks held wide
_KLAUS_YAWN_CLOSE = 12           # ticks to close
_KLAUS_YAWN_TOTAL = _KLAUS_YAWN_OPEN + _KLAUS_YAWN_HOLD + _KLAUS_YAWN_CLOSE
# Reactions: a brief smile (positive) or frown (negative) drawn as a
# curved mouth, layered over whatever Klaus is doing, then fading. In
# ticks (_KLAUS_TICK_MS each): ~2.5 s total.
_KLAUS_REACT_FRAMES = 50

_KLAUS_HAPPY_WORDS = frozenset("""
thanks thank thankyou thx cheers great perfect awesome nice excellent
brilliant helpful wonderful amazing love lovely good works worked working
appreciate appreciated cool fantastic correct exactly superb clever
""".split())
_KLAUS_SAD_WORDS = frozenset("""
stupid useless wrong idiot dumb hate awful terrible horrible bad broken
damn crap rubbish nonsense annoying frustrated frustrating angry sucks
failed fails failing worse worst pointless garbage bullshit
""".split())


def _classify_sentiment(text, allow_sad=True):
    """Very small local sentiment check on the user's own words — no
    model call, deterministic, instant. Returns 'happy', 'sad', or
    None. Whole-word matching on a lowercased copy so 'thanks!' counts
    and 'classic' does not trip a 'sad' substring. allow_sad=False when
    reacting to Klaus's OWN answer (cheer on success, never self-sad on
    a fine reply)."""
    words = set(re.findall(r"[a-z']+", text.lower()))
    if words & _KLAUS_HAPPY_WORDS:
        return 'happy'
    if allow_sad and (words & _KLAUS_SAD_WORDS):
        return 'sad'
    return None


class KlausPanel(wx.Panel):
    """Klaus, visualized as a quantum bubble. set_state() switches
    behavior; _tick() advances the animation state (pure arithmetic —
    tested headless); _on_paint renders whatever the state says."""

    def __init__(self, parent):
        super().__init__(parent)
        side = self.FromDIP(_KLAUS_SIZE)
        self.SetMinSize((side, side))
        self._side = float(_KLAUS_SIZE)   # logical coords; paint scales
        self._state = 'idle'
        self._phase = 0.0
        self._pos = [self._side / 2, self._side / 2]
        self._vel = [_KLAUS_SPEED * 0.8, -_KLAUS_SPEED * 0.6]
        self._pop_frame = _KLAUS_POP_FRAMES   # only meaningful in 'error'
        self._idle_ticks = 0                  # breathing time since last yawn
        self._yawn_frame = None               # None = not yawning; else 0..TOTAL
        self._react_kind = None               # None | 'happy' | 'sad'
        self._react_frame = 0
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._timer.Start(_KLAUS_TICK_MS)

    # --- state ---------------------------------------------------------
    def set_state(self, state):
        if state == 'error' and self._state != 'error':
            self._pop_frame = 0            # (re)play the pop burst
        if state == 'thinking' and self._state != 'thinking':
            if abs(self._vel[0]) + abs(self._vel[1]) < 0.1:
                self._vel = [_KLAUS_SPEED * 0.8, -_KLAUS_SPEED * 0.6]
        if state == 'idle' and self._state != 'idle':
            self._pos = [self._side / 2, self._side / 2]   # settle home
            self._idle_ticks = 0                           # restart the clock
            self._yawn_frame = None
        if state != 'idle':
            self._yawn_frame = None   # only idle Klaus yawns
        self._state = state
        self.Refresh()

    def yawn_openness(self):
        """0 (closed) .. 1 (widest) for the current yawn frame, or 0 when
        not yawning. Ease in over OPEN, hold at 1, ease out over CLOSE —
        pure arithmetic so it's testable headless."""
        f = self._yawn_frame
        if f is None:
            return 0.0
        if f < _KLAUS_YAWN_OPEN:
            return f / float(_KLAUS_YAWN_OPEN)
        if f < _KLAUS_YAWN_OPEN + _KLAUS_YAWN_HOLD:
            return 1.0
        closing = f - _KLAUS_YAWN_OPEN - _KLAUS_YAWN_HOLD
        return max(0.0, 1.0 - closing / float(_KLAUS_YAWN_CLOSE))

    def react(self, kind):
        """Play a brief facial reaction: 'happy' (smile) or 'sad'
        (frown). Layers over the current state; auto-fades. Ignored for
        unknown kinds so callers can pass a classifier result blindly."""
        if kind in ('happy', 'sad'):
            self._react_kind = kind
            self._react_frame = 0
            self.Refresh()

    def reaction_curve(self):
        """(kind, strength) for the current reaction, strength 0..1 with
        a quick rise and a slow fade, or (None, 0.0) when none. Pure
        arithmetic for headless testing; drives the mouth curvature."""
        if self._react_kind is None:
            return None, 0.0
        f, total = self._react_frame, _KLAUS_REACT_FRAMES
        rise = max(1, total // 5)
        if f < rise:
            s = f / float(rise)
        else:
            s = max(0.0, 1.0 - (f - rise) / float(total - rise))
        return self._react_kind, s

    def current_radius(self):
        """Breathing radius (idle/thinking share the gentle pulse)."""
        return _KLAUS_R_BASE * (1.0 + _KLAUS_BREATH_AMPL * math.sin(self._phase))

    def _tick(self):
        """Advance one animation step. Returns True if a repaint is
        needed (always, except a finished pop, which is static)."""
        # Reactions run regardless of state (they layer over it) and
        # always progress, so advance them first — even in 'error',
        # which otherwise returns early.
        reacting = self._react_kind is not None
        if reacting:
            self._react_frame += 1
            if self._react_frame >= _KLAUS_REACT_FRAMES:
                self._react_kind = None
        if self._state == 'error':
            if self._pop_frame < _KLAUS_POP_FRAMES:
                self._pop_frame += 1
                return True
            return reacting   # keep repainting while a reaction plays
        self._phase += _KLAUS_BREATH_STEP
        if self._state == 'idle':
            # Count calm breathing; trigger a yawn after a long stretch,
            # then run it to completion and reset the clock.
            if self._yawn_frame is not None:
                self._yawn_frame += 1
                if self._yawn_frame >= _KLAUS_YAWN_TOTAL:
                    self._yawn_frame = None
                    self._idle_ticks = 0
            else:
                self._idle_ticks += 1
                if self._idle_ticks >= _KLAUS_YAWN_AFTER:
                    self._yawn_frame = 0
        if self._state == 'thinking':
            r = self.current_radius()
            lo, hi = r, self._side - r
            for axis in (0, 1):
                self._pos[axis] += self._vel[axis]
                if self._pos[axis] < lo:
                    self._pos[axis] = lo + (lo - self._pos[axis])
                    self._vel[axis] = abs(self._vel[axis])
                elif self._pos[axis] > hi:
                    self._pos[axis] = hi - (self._pos[axis] - hi)
                    self._vel[axis] = -abs(self._vel[axis])
        return True

    def stop_timer(self):
        """Halt the animation timer before the window/app is destroyed.
        A wx.Timer left running fires its callback into a half-torn-down
        widget during shutdown — a native access violation (Windows
        fatal exception). Called from AssistantFrame teardown and,
        defensively, is idempotent."""
        if self._timer.IsRunning():
            self._timer.Stop()

    def _on_timer(self, event):
        if self._tick():
            self.Refresh()

    # --- painting ------------------------------------------------------
    def _on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if not gc:
            return
        w, h = self.GetClientSize()
        scale = min(w, h) / self._side if self._side else 1.0
        gc.Scale(scale, scale)
        cr, cg, cb = _KLAUS_COLOR
        if self._state == 'error':
            self._paint_pop(gc, cr, cg, cb)
            return
        x, y = (self._pos if self._state == 'thinking'
                else (self._side / 2, self._side / 2))
        r = self.current_radius()
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(wx.Colour(cr, cg, cb, 220), 2)))
        gc.SetBrush(wx.Brush(wx.Colour(cr, cg, cb, 70)))
        gc.DrawEllipse(x - r, y - r, 2 * r, 2 * r)
        # highlight: the little window reflection that makes it a bubble
        hr = r * 0.28
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(wx.Colour(255, 255, 255, 150)))
        gc.DrawEllipse(x - r * 0.45 - hr / 2, y - r * 0.45 - hr / 2, hr, hr)
        # yawn: a mouth that opens low on the bubble during the yawn cycle
        openness = self.yawn_openness()
        if openness > 0.01:
            mouth_w = r * 0.42
            mouth_h = r * 0.5 * openness          # grows as it opens wide
            my = y + r * 0.32                     # sit in the lower half
            gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(
                wx.Colour(20, 90, 80, 200), 1.5)))
            gc.SetBrush(wx.Brush(wx.Colour(20, 70, 66, 180)))
            gc.DrawEllipse(x - mouth_w / 2, my - mouth_h / 2, mouth_w, mouth_h)
        # reaction: a curved mouth — smile (happy) or frown (sad) — that
        # rises quickly and fades, layered over the bubble.
        kind, strength = self.reaction_curve()
        if kind and strength > 0.02:
            self._paint_reaction_mouth(gc, x, y, r, kind, strength)

    def _paint_reaction_mouth(self, gc, x, y, r, kind, strength):
        mw = r * 0.55
        depth = r * 0.32 * strength          # how curved the mouth is
        my = y + r * 0.34
        sign = 1.0 if kind == 'happy' else -1.0   # up-curve vs down-curve
        alpha = int(220 * min(1.0, strength + 0.2))
        gc.SetPen(gc.CreatePen(wx.GraphicsPenInfo(
            wx.Colour(20, 80, 72, alpha), 2.2)))
        path = gc.CreatePath()
        path.MoveToPoint(x - mw / 2, my)
        # quadratic curve: control point below (smile) or above (frown)
        path.AddQuadCurveToPoint(x, my + sign * depth, x + mw / 2, my)
        gc.StrokePath(path)

    def _paint_pop(self, gc, cr, cg, cb):
        cx = cy = self._side / 2
        t = self._pop_frame / float(_KLAUS_POP_FRAMES)   # 0..1
        if t < 1.0:
            # burst: fragments flying outward, fading
            alpha = max(0, int(200 * (1.0 - t)))
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(wx.Colour(cr, cg, cb, alpha)))
            for k in range(8):
                ang = k * math.pi / 4.0
                d = _KLAUS_R_BASE * (0.6 + 1.1 * t)
                fr = 3.5 * (1.0 - 0.5 * t)
                gc.DrawEllipse(cx + d * math.cos(ang) - fr,
                               cy + d * math.sin(ang) - fr, 2 * fr, 2 * fr)
        else:
            # settled: a few sad droplets where the bubble was
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(wx.Brush(wx.Colour(220, 80, 80, 180)))
            for dx, dy, fr in ((-10, 6, 3.0), (8, -4, 2.4), (2, 12, 2.0),
                               (-4, -11, 1.8)):
                gc.DrawEllipse(cx + dx - fr, cy + dy - fr, 2 * fr, 2 * fr)


class AssistantFrame(wx.Frame):
    """The AI Assistant window. Lifecycle mirrors the Graph window: a
    separate top-level frame the View menu opens/raises; closing hides
    it (state kept for the session)."""

    def __init__(self, parent):
        super().__init__(parent, title='qmeas AI Assistant — Klaus',
                         size=parent.FromDIP(wx.Size(560, 640)))
        self._history = []          # [{'role','content'}] — no system here
        self._busy = False
        self._closing = False
        self._system_prompt, kb_found = _load_assistant_knowledge()
        self._examples = _load_examples()   # few-shot (question, answer) pairs

        panel = wx.Panel(self)
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.klaus = KlausPanel(panel)
        top.Add(self.klaus, 0, wx.ALL, panel.FromDIP(6))
        right = wx.BoxSizer(wx.VERTICAL)
        model_row = wx.BoxSizer(wx.HORIZONTAL)
        model_row.Add(wx.StaticText(panel, label='Model:'), 0,
                      wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, panel.FromDIP(4))
        self.model_choice = wx.Choice(panel, choices=['(searching...)'])
        self.model_choice.SetSelection(0)
        model_row.Add(self.model_choice, 1)
        self.btn_notes = wx.Button(panel, label='User notes')
        self.btn_notes.SetToolTip('Open your lab-setup notes (qmeas_users.md) '
                                  'in a text editor. What you write there is '
                                  'given to the assistant, and reloaded before '
                                  'each question.')
        self.btn_notes.Bind(wx.EVT_BUTTON, self._on_user_notes)
        model_row.Add(self.btn_notes, 0, wx.LEFT, panel.FromDIP(4))
        right.Add(model_row, 0, wx.EXPAND | wx.TOP, panel.FromDIP(6))
        top.Add(right, 1, wx.EXPAND | wx.RIGHT, panel.FromDIP(6))

        self.chat = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY
                                | wx.TE_RICH2 | wx.TE_BESTWRAP)
        # Auto-follow the streaming bottom until the user scrolls away;
        # resume when they return to the bottom. Driven by their own
        # input events, not by (unreliable) wx scroll geometry.
        self._follow = True
        self.chat.Bind(wx.EVT_MOUSEWHEEL, self._on_chat_scroll)
        self.chat.Bind(wx.EVT_SCROLLWIN, self._on_chat_scroll)
        entry_row = wx.BoxSizer(wx.HORIZONTAL)
        self.entry = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.entry.SetHint('Ask Klaus about qmeas or your experiment setup...')
        self.btn_send = wx.Button(panel, label='Send')
        entry_row.Add(self.entry, 1, wx.RIGHT, panel.FromDIP(4))
        entry_row.Add(self.btn_send, 0)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(top, 0, wx.EXPAND)
        main.Add(self.chat, 1, wx.EXPAND | wx.ALL, panel.FromDIP(6))
        main.Add(entry_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
                 panel.FromDIP(6))
        panel.SetSizer(main)

        self.entry.Bind(wx.EVT_TEXT_ENTER, self._on_send)
        self.btn_send.Bind(wx.EVT_BUTTON, self._on_send)
        self.model_choice.Bind(wx.EVT_CHOICE, self._on_model_selected)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._warming = False
        self._warmed_model = None
        self._answered_once = False
        self._placeholder_range = None   # (start, end) in the chat ctrl

        if not kb_found:
            self._set_status('qmeas_knowledge_base.md not found next to '
                             'qmeas.py — answers will lack qmeas specifics.')
        self._append_info(
            "Hi, I'm Klaus. Ask me about qmeas, such as commands, sweeps, or "
            "how to set up a measurement.\n\n"
            "[Note: Depending on the selected model, the answers may contain "
            "significant errors and false instructions. Do not blindly follow "
            "the AI-generated text; compare with the manual and your good "
            "judgement.]\n")
        self.refresh_models()

    def refresh_models(self):
        """(Re)query Ollama for the model list — called at creation AND
        every time View -> AI Assistant is invoked, so a model pulled
        while qmeas runs appears without a restart. Skipped mid-reply
        (the dropdown is in use)."""
        if self._busy:
            return
        threading.Thread(target=self._load_models_worker, daemon=True).start()

    # --- helpers (UI thread only) --------------------------------------
    def _set_status(self, text):
        # No status widget anymore (removed by request): anything worth
        # saying goes into the chat as a grey info line.
        if text:
            self._append_info(text)

    def _append_info(self, text):
        self.chat.SetDefaultStyle(wx.TextAttr(wx.Colour(140, 140, 146)))
        self.chat.AppendText(text + '\n')

    def _append_role(self, who, color):
        self.chat.SetDefaultStyle(wx.TextAttr(color, wx.NullColour,
                                              self.chat.GetFont().Bold()))
        self.chat.AppendText(f'{who}: ')
        self.chat.SetDefaultStyle(wx.TextAttr(self.chat.GetForegroundColour(),
                                              wx.NullColour,
                                              self.chat.GetFont()))

    def _on_chat_scroll(self, event):
        """User scrolled the chat. Wheel-up or dragging the thumb up
        pauses auto-follow so streaming text won't yank the view back
        down; scrolling to the very bottom resumes following. Uses the
        event direction (robust) plus a lightweight bottom check that
        only needs to fire on actual user scrolls, not per chunk."""
        event.Skip()   # let the control scroll normally first
        try:
            if event.GetEventType() == wx.EVT_MOUSEWHEEL.typeId:
                if event.GetWheelRotation() > 0:
                    self._follow = False          # wheeled up -> reading
                    return
            # wheel-down, or any scrollbar action: decide by whether the
            # last line is now visible. wx.CallAfter so we read position
            # AFTER the scroll this event triggers is applied.
            wx.CallAfter(self._update_follow_from_position)
        except Exception:
            pass

    def _update_follow_from_position(self):
        try:
            last_visible = self.chat.GetScrollRange(wx.VERTICAL)
            pos = self.chat.GetScrollPos(wx.VERTICAL)
            thumb = self.chat.GetScrollThumb(wx.VERTICAL)
            # At bottom when the thumb's far edge reaches the range end.
            self._follow = (pos + thumb) >= (last_visible - 1)
        except Exception:
            self._follow = True

    def _append_text(self, text):
        # Auto-follow the bottom ONLY while self._follow is True. The
        # user's own scrolling controls that flag (see _on_chat_scroll):
        # scroll/wheel up -> stop following (you're reading); scroll
        # back to the bottom -> resume. We do NOT try to read wx scroll
        # geometry here — it is unreliable for multiline TextCtrl on
        # Windows (an earlier attempt always mis-detected 'at bottom'
        # and forced the view down). Event-driven intent is robust.
        if getattr(self, '_follow', True):
            self.chat.AppendText(text)   # jumps to end, which is what we want
        else:
            self.chat.SetInsertionPointEnd()
            self.chat.WriteText(text)    # append without forcing a scroll

    # --- model list -----------------------------------------------------
    def _load_models_worker(self):
        try:
            models = _ollama_list_models()
        except Exception:
            wx.CallAfter(self._models_failed)
            return
        wx.CallAfter(self._models_loaded, models)

    def _models_failed(self):
        if self._closing:
            return
        self.model_choice.Set(['(Ollama not found)'])
        self.model_choice.SetSelection(0)
        self.klaus.set_state('error')
        self._set_status(f'Ollama not reachable at {_OLLAMA_HOST}:{_OLLAMA_PORT} '
                         '— is it installed and running? (See the manual, '
                         'section "AI Assistant".)')

    def _models_loaded(self, models):
        if self._closing:
            return
        if not models:
            self._models_failed()
            return
        previous = self.model_choice.GetStringSelection()
        self.model_choice.Set(models)
        # Selection priority: what the user had selected (if still
        # available) > the preferred default > the first model.
        idx = self.model_choice.FindString(previous) if previous else wx.NOT_FOUND
        if idx == wx.NOT_FOUND:
            idx = self.model_choice.FindString(_ASSISTANT_PREFERRED_MODEL)
        self.model_choice.SetSelection(idx if idx != wx.NOT_FOUND else 0)
        self.klaus.set_state('idle')
        self._set_status('')
        self._maybe_warm(self.model_choice.GetStringSelection())

    def _on_user_notes(self, event):
        """Open qmeas_users.md (auto-created if missing) in the system
        text editor, and reload the assistant's knowledge so edits take
        effect without reopening the window. The editor opens
        asynchronously, so we can't reload the instant it closes;
        instead we reload now AND offer a Reload button, and tell the
        user plainly that saved edits apply on the next question after
        a reload."""
        path = _ensure_user_notes()
        open_text_editor(str(path))
        self._append_info(
            'Opened your notes (' + USER_NOTES_FILENAME + ') in a text editor. '
            'Save your changes there — they are reloaded automatically before '
            'your next question.')

    def _reload_knowledge(self):
        """Re-read the general KB + user notes into the system prompt.
        Cheap (two small files); called before each question, so saved
        note edits are always reflected without any user action."""
        self._system_prompt, _ = _load_assistant_knowledge()
        self._examples = _load_examples()

    def _on_model_selected(self, event):
        self._maybe_warm(self.model_choice.GetStringSelection())
        event.Skip()

    def _should_warm(self, model):
        """Guard: warm once per selected real model; never in parallel;
        never for placeholder entries."""
        return bool(model) and not model.startswith('(') \
            and not self._warming and model != self._warmed_model

    def _maybe_warm(self, model):
        """Silent by request: no status chatter — the honest wait notice
        lives inside the chat, on the first answer only."""
        if not self._should_warm(model):
            return
        self._warming = True
        threading.Thread(target=self._warm_worker, args=(model,),
                         daemon=True).start()

    def _warm_worker(self, model):
        try:
            _ollama_warm(model, self._system_prompt)
        except Exception:
            wx.CallAfter(self._warm_finished, None)
            return
        wx.CallAfter(self._warm_finished, model)

    def _warm_finished(self, model_or_none):
        self._warming = False
        if model_or_none is not None:
            self._warmed_model = model_or_none

    # --- chat -----------------------------------------------------------
    def _on_send(self, event):
        if self._busy:
            return
        text = self.entry.GetValue().strip()
        if not text:
            return
        model = self.model_choice.GetStringSelection()
        if not model or model.startswith('('):
            self._set_status('No model available — install/start Ollama and '
                             'reopen this window.')
            return
        self.entry.SetValue('')
        self._busy = True
        self._follow = True   # a new question -> follow its answer to the bottom
        self.btn_send.Disable()
        self.klaus.set_state('thinking')
        reaction = _classify_sentiment(text, allow_sad=True)
        if reaction:
            self.klaus.react(reaction)   # smile on thanks, frown on anger
        self._append_role('You', wx.Colour(70, 130, 200))
        self._append_text(text + '\n')
        self._append_role('Klaus', wx.Colour(40, 170, 150))
        if not self._answered_once:
            self._show_placeholder('[the first answer may take up to 2 minutes]')
        self._history.append({'role': 'user', 'content': text})
        self._reload_knowledge()   # pick up any just-saved user-notes edits
        messages = self._build_messages()
        threading.Thread(target=self._chat_worker,
                         args=(model, messages), daemon=True).start()

    def _build_messages(self):
        """system prompt, then the few-shot examples as prior user/
        assistant turns (so the model treats them as demonstrated good
        answers and imitates them), then the real conversation. Each
        answer is shown ONCE; its question variants are listed together
        as the user turn (no duplicating the long answer per phrasing).
        A short framing note precedes the examples."""
        messages = [{'role': 'system', 'content': self._system_prompt}]
        if self._examples:
            messages.append({'role': 'system',
                             'content': 'The following are example exchanges '
                             'showing how to answer common questions well. '
                             'Follow their structure and level of detail. '
                             'Each may list several ways of asking the same '
                             'thing.'})
            for questions, answer in self._examples:
                if len(questions) == 1:
                    user = questions[0]
                else:
                    user = ('(any of these:) '
                            + ' / '.join(questions))
                messages.append({'role': 'user', 'content': user})
                messages.append({'role': 'assistant', 'content': answer})
        return messages + self._history

    def _chat_worker(self, model, messages):
        try:
            reply = _ollama_chat_stream(
                model, messages,
                on_chunk=lambda piece: wx.CallAfter(self._on_chunk, piece),
                should_stop=lambda: self._closing)
        except Exception as e:
            wx.CallAfter(self._on_reply_failed, e)
            return
        wx.CallAfter(self._on_reply_done, reply)

    def _show_placeholder(self, text):
        start = self.chat.GetLastPosition()
        self.chat.SetDefaultStyle(wx.TextAttr(wx.Colour(140, 140, 146)))
        self.chat.AppendText(text)
        self.chat.SetDefaultStyle(wx.TextAttr(self.chat.GetForegroundColour()))
        self._placeholder_range = (start, self.chat.GetLastPosition())

    def _clear_placeholder(self):
        if self._placeholder_range is None:
            return
        start, end = self._placeholder_range
        self._placeholder_range = None
        self.chat.Remove(start, end)
        self.chat.SetDefaultStyle(wx.TextAttr(self.chat.GetForegroundColour()))

    def _on_chunk(self, piece):
        if not self._closing:
            self._clear_placeholder()
            self._append_text(piece)

    def _on_reply_done(self, reply):
        if self._closing:
            return
        self._clear_placeholder()   # empty reply edge: don't leave it behind
        self._answered_once = True
        self._history.append({'role': 'assistant', 'content': reply})
        self._append_text('\n\n')
        self._busy = False
        self.btn_send.Enable()
        self.klaus.set_state('idle')
        # Klaus reacts to his OWN answer too, but only cheers — never a
        # self-frown on an ordinary reply. A reaction already playing
        # from the user's message (e.g. their 'thanks') is not overridden.
        if self.klaus.reaction_curve()[0] is None:
            own = _classify_sentiment(reply, allow_sad=False)
            if own:
                self.klaus.react(own)

    def _on_reply_failed(self, error):
        if self._closing:
            return
        self._clear_placeholder()
        self._history.pop()   # the unanswered user turn: retry cleanly
        self._append_text('\n')
        self._append_info(f'(no answer: {error})')
        self._busy = False
        self.btn_send.Enable()
        self.klaus.set_state('error')
        self._set_status('Request failed — is Ollama still running?')

    def _on_close(self, event):
        # Hide, don't destroy: the conversation survives for the session,
        # and any in-flight stream sees _closing and abandons quietly.
        self._closing = True
        self.Hide()
        wx.CallLater(300, self._reset_closing)

    def _reset_closing(self):
        self._closing = False


class QMeasMain(wx.Frame):

    def __init__(self):
        super().__init__(None, title=f'{APP_NAME}  –  version {APP_VERSION}',
                         size=wx.Size(1100, 700))
        self.SetBackgroundColour(COLOR_APP_BG)

        icon_path = img('qmeas_icon.ico')
        if icon_path:
            try:
                self.SetIcon(wx.Icon(icon_path, wx.BITMAP_TYPE_ICO))
            except Exception:
                pass   # missing/bad icon file shouldn't prevent the app from starting

        self._mgr = aui.AuiManager(self)
        self._style_dock_art()

        self._view_items = {}   # name -> View-menu check item (AUI panes + graph)
        self.settings = load_settings()

        self._build_menu()
        self._build_panes()
        self._build_graph_window()
        self._build_statusbar()

        self._mgr.Update()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Centre()

    # -----------------------------------------------------------------
    def _style_dock_art(self):
        art = self._mgr.GetArtProvider()
        art.SetColour(aui.AUI_DOCKART_BACKGROUND_COLOUR, COLOR_APP_BG)
        art.SetColour(aui.AUI_DOCKART_SASH_COLOUR, COLOR_AUI_SASH)
        art.SetColour(aui.AUI_DOCKART_BORDER_COLOUR, COLOR_AUI_BORDER)
        art.SetColour(aui.AUI_DOCKART_ACTIVE_CAPTION_COLOUR, COLOR_AUI_CAPTION_ACTIVE)
        art.SetColour(aui.AUI_DOCKART_ACTIVE_CAPTION_GRADIENT_COLOUR, COLOR_AUI_CAPTION_ACTIVE)
        art.SetColour(aui.AUI_DOCKART_ACTIVE_CAPTION_TEXT_COLOUR, COLOR_AUI_CAPTION_ACTIVE_TEXT)
        art.SetColour(aui.AUI_DOCKART_INACTIVE_CAPTION_COLOUR, COLOR_AUI_CAPTION_INACTIVE)
        art.SetColour(aui.AUI_DOCKART_INACTIVE_CAPTION_GRADIENT_COLOUR, COLOR_AUI_CAPTION_INACTIVE)
        art.SetColour(aui.AUI_DOCKART_INACTIVE_CAPTION_TEXT_COLOUR, COLOR_AUI_CAPTION_INACTIVE_TEXT)
        art.SetMetric(aui.AUI_DOCKART_GRADIENT_TYPE, aui.AUI_GRADIENT_NONE)

    def _build_menu(self):
        menubar = wx.MenuBar()

        file_menu = wx.Menu()
        settings_item = file_menu.Append(wx.ID_ANY, '&Settings...')
        self.Bind(wx.EVT_MENU, self._on_settings, settings_item)
        file_menu.AppendSeparator()
        exit_item = file_menu.Append(wx.ID_EXIT, '&Exit\tCtrl+Q')
        self.Bind(wx.EVT_MENU, self._on_close, exit_item)
        menubar.Append(file_menu, '&File')

        self.view_menu = wx.Menu()
        menubar.Append(self.view_menu, '&View')

        help_menu = wx.Menu()
        manual_item = help_menu.Append(wx.ID_ANY, '&Open Manual')
        self.Bind(wx.EVT_MENU, self._on_open_manual, manual_item)
        help_menu.AppendSeparator()
        about_item = help_menu.Append(wx.ID_ABOUT, '&About')
        self.Bind(wx.EVT_MENU, self._on_about, about_item)
        menubar.Append(help_menu, '&Help')

        self.SetMenuBar(menubar)

    def _build_panes(self):
        self.tasks_panel    = TasksPanel(self)
        self.devices_panel = DevicesPanel(self, on_add_command=self.tasks_panel.add_task,
                                          on_device_toggled=self.tasks_panel.on_device_toggled)
        self.device_editor_panel = DeviceEditorPanel(self, on_device_saved=self.devices_panel.load_devfile)
        self.device_editor_panel.get_current_devfile = (
            lambda: self.devices_panel.current_devfile)
        self.log_panel      = LogPanel(self)

        # TasksPanel needs to know which .dev file is currently loaded
        # (to find command/device files at execution time), which
        # read/query commands are currently active (to run them after
        # each write), and where to send log text — wired here since
        # both panels now exist.
        self.tasks_panel.get_device_stem = (
            lambda: self.devices_panel.current_devfile.with_suffix('')
            if self.devices_panel.current_devfile else None)
        self.tasks_panel.get_query_specs = self.devices_panel.get_active_query_specs
        self.tasks_panel.get_onhold_devices = self.devices_panel.get_active_onhold_devices
        self.tasks_panel.get_active_devices = self.devices_panel.get_active_device_names
        self.tasks_panel.on_log = self.log_panel.append_log
        # Run freeze: while TaskRunnerThread is alive, the device tree
        # and Device Editor are frozen too, not just the Tasks grid —
        # the thread reads command/device files from disk on every row.
        # Single source of truth: TasksPanel.run_active().
        self.devices_panel.is_run_active = self.tasks_panel.run_active
        self.device_editor_panel.is_run_active = self.tasks_panel.run_active
        # Clicking (or double-clicking) a device in the tree loads its
        # current settings into the Device Editor so they can be
        # edited; double-click additionally brings the pane into view
        # if it's hidden (see DevicesPanel._on_item_activated).
        self.devices_panel.on_device_selected = self.device_editor_panel.load_existing_device
        self.devices_panel.on_device_editor_reveal = lambda: self._reveal_pane('device_editor')
        self.devices_panel.on_new_list_created = self._on_new_list_created

        # FloatingSize on every pane: BestSize only governs the DOCKED
        # size — an undocked (pulled-out) pane floats at wx.aui's own
        # default otherwise, which is near-zero and reads as
        # 'minimized by default'. Sizes chosen to show each
        # pane's content usably on arrival; the user can resize the
        # floating frame afterwards as normal.
        self._add_pane(self.devices_panel, 'devices', 'Devices & Commands',
                       aui.AuiPaneInfo().Left().BestSize(280, -1).MinSize(200, -1)
                       .FloatingSize(wx.Size(320, 600)))
        self._add_pane(self.device_editor_panel, 'device_editor', 'Device Editor',
                       aui.AuiPaneInfo().Left().BestSize(320, -1).MinSize(260, -1)
                       .FloatingSize(wx.Size(360, 600)))
        self._add_pane(self.tasks_panel, 'tasks', 'Tasks',
                       aui.AuiPaneInfo().CenterPane()
                       .FloatingSize(wx.Size(900, 500)))
        self._add_pane(self.log_panel, 'log', 'Log',
                       aui.AuiPaneInfo().Bottom().BestSize(-1, 180).MinSize(-1, 100)
                       .FloatingSize(wx.Size(700, 300)))

        # Hidden by default — v1 only showed its device editor on demand
        # too (a hidden panel toggled visible), not as permanent screen
        # real estate alongside the tree.
        self._mgr.GetPane('device_editor').Show(False)
        self._view_items['device_editor'].Check(False)

        # Closing a pane via its own caption [x] bypasses the View menu
        # handler entirely (AUI handles it directly) — sync the checkbox.
        self.Bind(aui.EVT_AUI_PANE_CLOSE, self._on_pane_close)

        # Stacking presets, appended after the pane checkitems above
        # (this runs inside _build_panes, so they land at the bottom of
        # the View menu). See _stack_panes for what they do.
        self.view_menu.AppendSeparator()
        item_vstack = self.view_menu.Append(wx.ID_ANY, 'Vertical stacking')
        item_hstack = self.view_menu.Append(wx.ID_ANY, 'Horizontal stacking')
        self.Bind(wx.EVT_MENU, lambda evt: self._stack_panes(vertical=True), item_vstack)
        self.Bind(wx.EVT_MENU, lambda evt: self._stack_panes(vertical=False), item_hstack)
        self.view_menu.AppendSeparator()
        item_assistant = self.view_menu.Append(wx.ID_ANY, 'AI Assistant')
        self.Bind(wx.EVT_MENU, self._on_toggle_assistant, item_assistant)
        self.assistant_frame = None   # created lazily on first open

    def _add_pane(self, window, name, caption, pane_info):
        pane_info = (pane_info
                     .Name(name).Caption(caption)
                     .CloseButton(True).MaximizeButton(True)
                     .Floatable(True).Dockable(True))
        self._mgr.AddPane(window, pane_info)

        item = self.view_menu.AppendCheckItem(wx.ID_ANY, caption)
        item.Check(True)
        self._view_items[name] = item
        self.Bind(wx.EVT_MENU, lambda evt, n=name: self._toggle_pane(n), item)

    def _toggle_pane(self, name):
        pane = self._mgr.GetPane(name)
        pane.Show(not pane.IsShown())
        self._mgr.Update()
        self._view_items[name].Check(pane.IsShown())

    def _on_new_list_created(self):
        """A new device list replaced the loaded one: force-close the
        Device Editor (it may still display a device of the OLD list)
        and reset its form to 'add new' state, so reopening starts
        clean instead of resurrecting a stale edit target."""
        self.device_editor_panel._on_new_device(None)
        self._hide_pane('device_editor')

    def _hide_pane(self, name):
        """Counterpart of _reveal_pane: hide a pane (no-op if already
        hidden) and sync the View menu checkbox."""
        pane = self._mgr.GetPane(name)
        if pane.IsShown():
            pane.Show(False)
            self._mgr.Update()
            self._view_items[name].Check(False)

    def _reveal_pane(self, name):
        """Show a pane if it's currently hidden (no-op if already
        shown) and sync the View menu checkbox — used so double-
        clicking a device brings the Device Editor into view even if
        it's docked away, without also toggling it shut if it was
        already open."""
        pane = self._mgr.GetPane(name)
        if not pane.IsShown():
            pane.Show(True)
            self._mgr.Update()
            self._view_items[name].Check(True)
        elif pane.IsFloating():
            pane.window.Raise()   # already open in a floating window, just behind something else

    def _on_pane_close(self, event):
        pane = event.GetPane()
        item = self._view_items.get(pane.name)
        if item is not None:
            item.Check(False)
        event.Skip()   # let AUI still perform the actual close/hide

    def _stack_panes(self, vertical: bool):
        """View -> Vertical/Horizontal stacking: one-click layout
        presets for every AUI pane inside the main frame. The Graph
        window is untouched by construction — it's a separate
        top-level wx.Frame, not an AUI pane ('except plotting' comes
        for free).

        Vertical: everything in one full-width column — devices and
        Device Editor docked Top (each in its own row), Tasks center,
        Log docked Bottom. Horizontal: everything side by side in
        full-height columns — devices and Device Editor as two Left
        columns, Tasks center, Log as a Right column.

        Applies to ALL panes, visible or not: a currently-hidden pane
        (Device Editor is hidden by default) is NOT shown as a side
        effect — its Show state is untouched — but its geometry is
        set, so toggling it visible later drops it into the scheme
        instead of wherever it last was. Floating panes are docked
        back in (.Dock()): 'stack all windows' with one of them still
        floating outside the arrangement wouldn't be a stacking.
        Layer(0) throughout so the docks share the innermost layer
        and actually form the single column/row instead of wrapping
        around each other. The user can drag things around afterwards
        as usual — this is a preset, not a lock."""
        placement = ({'devices': ('Top', 0), 'device_editor': ('Top', 1),
                      'log': ('Bottom', 0)}
                     if vertical else
                     {'devices': ('Left', 0), 'device_editor': ('Left', 1),
                      'log': ('Right', 0)})
        for name, (direction, row_idx) in placement.items():
            pane = self._mgr.GetPane(name)
            if not pane.IsOk():
                continue
            pane.Dock()
            getattr(pane, direction)()   # AuiPaneInfo.Top()/.Bottom()/.Left()/.Right()
            pane.Layer(0).Row(row_idx).Position(0)
        self._mgr.Update()

    def _build_graph_window(self):
        # Deliberately a separate top-level window, not an AUI pane —
        # meant to be moved to a second monitor during a run.
        self.graph_window = GraphWindow(self, get_sweep_data=lambda: self.tasks_panel.last_sweep_data)
        self.graph_window.Bind(wx.EVT_CLOSE, self._on_graph_close)
        # Auto-refresh if a counter/sweep run finishes while this
        # window is already open — see TasksPanel._on_sweep_done.
        self.tasks_panel.on_sweep_data_ready = self.graph_window.refresh_data

        self.view_menu.AppendSeparator()
        item = self.view_menu.AppendCheckItem(wx.ID_ANY, 'Graph (separate window)')
        item.Check(False)
        self._view_items['graph'] = item
        self.Bind(wx.EVT_MENU, self._toggle_graph, item)

    def _toggle_graph(self, event=None):
        shown = not self.graph_window.IsShown()
        self.graph_window.Show(shown)
        if shown:
            self.graph_window.Raise()
            self.graph_window.refresh_data()   # pick up any run that finished while this was hidden
        self._view_items['graph'].Check(shown)

    def _on_graph_close(self, event):
        # Hide rather than destroy, so later plot state isn't lost by
        # closing the window — same convention as the docked panes.
        event.Veto()
        self.graph_window.Hide()
        self._view_items['graph'].Check(False)

    def _build_statusbar(self):
        sb = self.CreateStatusBar(1)
        sb.SetStatusText('Ready. Double-click a command to add it to Tasks.')

    # -----------------------------------------------------------------
    def _on_settings(self, event):
        dlg = SettingsDialog(self, self.settings)
        if dlg.ShowModal() == wx.ID_OK:
            current = load_settings()   # re-read from disk: DevicesPanel writes
            current.update(dlg.get_settings())   # to this file independently too
            save_settings(current)
            self.settings = current
        dlg.Destroy()

    def _on_open_manual(self, event):
        """'qmanual.pdf' next to qmeas.py — app_root(), same convention
        every other bundled-file lookup in this codebase already uses
        (img(), dev_dir(), custom_dir()). Missing file -> a plain
        message, not a crash; open_file_cross_platform() itself already
        swallows launch failures (e.g. no PDF viewer registered)."""
        manual_path = app_root() / 'qmanual.pdf'
        if not manual_path.is_file():
            wx.MessageBox(f"Manual not found at:\n{manual_path}",
                          'Open Manual', wx.OK | wx.ICON_WARNING)
            return
        open_file_cross_platform(str(manual_path))

    def _on_toggle_assistant(self, event):
        """View -> AI Assistant: create on first use, then show/raise —
        same lifecycle as the Graph window. The frame hides on close so
        the conversation survives for the session; the knowledge base is
        re-read on each fresh CREATION (restart qmeas or use a new
        window after app restart to pick up an updated .md)."""
        if self.assistant_frame is None or not self.assistant_frame:
            self.assistant_frame = AssistantFrame(self)
        else:
            self.assistant_frame.refresh_models()   # pick up newly pulled models
            self.assistant_frame._reload_knowledge()  # pick up saved user-notes edits
        self.assistant_frame.Show()
        self.assistant_frame.Raise()

    def _on_about(self, event):
        wx.MessageBox(
            'qmeas 2.0 - a quantum measurement tool\n\n'
            "Developed with Anthropic's Claude.\n\n"
            'Copyright (c) 2026 Project h_e2\n'
            'MIT license: https://opensource.org/licenses/MIT\n\n'
            'qmeas may contain bugs and behaviour in untested configurations can be '
            'unpredictable. No guarantee is made for correct, safe, or reproducible operation '
            'under any circumstances. It is the sole responsibility of the user to (1) '
            'thoroughly test the software with their specific devices and measurement '
            'configurations before relying on it for experiments, (2) verify that all output '
            'values, sweep parameters, and device commands are correct and within safe '
            'operating limits before executing a measurement, (3) operate qmeas under direct '
            'human supervision at all times when controlling hazardous equipment such as '
            'superconducting magnets, cryogenic systems, high-voltage sources, or laser '
            'systems. The authors accept no liability for damage to equipment, samples, or '
            'data arising from the use of this software.',
            'About', wx.OK | wx.ICON_INFORMATION)

    def _on_close(self, event):
        self.devices_panel.save_query_led_states()
        # Deterministic teardown order. Timers MUST stop before native
        # widgets are destroyed — a timer firing its callback into a
        # half-destroyed window is a Windows access violation at exit.
        try:
            self.tasks_panel._gauge_timer.Stop()
            self.tasks_panel._time_timer.Stop()
        except Exception:
            pass
        if self.assistant_frame:
            try:
                self.assistant_frame.klaus.stop_timer()
            except Exception:
                pass
            self.assistant_frame.Destroy()
        self.graph_window.Destroy()
        self._mgr.UnInit()
        self.Destroy()


if __name__ == '__main__':
    app = wx.App(False)
    frame_main = QMeasMain()
    frame_main.Show()
    app.MainLoop()
