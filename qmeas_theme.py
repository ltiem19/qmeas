"""
qmeas_theme.py  –  Central visual constants for qmeas.

Single source of truth for color and spacing, so every window looks
consistent and a palette change doesn't require grepping the whole
codebase. Deliberately starts minimal: only constants actually in use
are defined here; new ones are added as each window is converted from
absolute-position layout to sizers, not speculatively ahead of need.

Usage:
    from qmeas_theme import COLOR_BG, COLOR_DIAGNOSE_BG, PAD, PAD_SMALL

Spacing constants are in logical pixels; multiply by
self.FromDIP(1) (or pass through self.FromDIP((w, h)) for sizes)
at the point of use so layouts scale correctly on HiDPI displays.
This module intentionally does not import wx itself beyond wx.Colour,
so it stays a plain data module.
"""

import wx

# --- Backgrounds (v1, still used by sub_diagnose.py) --------------------
COLOR_BG           = wx.Colour(255, 255, 255)   # standard window background
COLOR_DIAGNOSE_BG  = wx.Colour(204, 255, 204)   # diagnose window: deliberate
                                                 # visual flag that this is a
                                                 # debug/diagnose window, kept
                                                 # as-is from v1

# --- Spacing (logical px; scale with self.FromDIP() at point of use) ---
PAD        = 8    # standard border/gap between grouped controls
PAD_SMALL  = 4    # tight spacing within a control group
PAD_LARGE  = 16   # separation between unrelated sections

# =========================================================================
# v2 palette — for qmeas_shell_v2.py and everything built on it.
# First-pass values, trivially revisable (named constants only, nothing
# downstream depends on the numbers). Flat/light, muted slate-blue accent,
# desaturated status colors so they read clearly but don't strain on an
# unattended monitor left running for hours.
# =========================================================================

# --- Core surfaces -------------------------------------------------------
COLOR_APP_BG        = wx.Colour(246, 247, 249)   # frame/background behind panes
COLOR_PANEL_BG      = wx.Colour(255, 255, 255)   # content area of each pane
COLOR_BORDER        = wx.Colour(210, 213, 218)   # pane/sash borders
COLOR_PASSED_OVER   = wx.Colour(237, 238, 240)   # Tasks grid: temporarily blocked while a run scans past it
                                                  # (lighter than COLOR_BORDER, which marks permanent completion)
COLOR_ACTIVE_ROW    = wx.Colour(255, 235, 130)   # Tasks grid row currently executing (yellow)

# --- Text ------------------------------------------------------------
COLOR_TEXT          = wx.Colour(32, 34, 38)      # primary text
COLOR_TEXT_MUTED    = wx.Colour(120, 124, 132)   # placeholders, secondary text

# --- Accent ----------------------------------------------------------
COLOR_ACCENT        = wx.Colour(237, 148, 66)    # light orange, active elements

# --- Status (idle/running/warning/error — used for device & task state) --
COLOR_STATUS_IDLE     = wx.Colour(69, 143, 92)    # muted green — also used as LED "on"
COLOR_LED_OFF          = wx.Colour(196, 199, 204)  # LED "off" — distinct from borders/text
COLOR_STATUS_RUNNING  = wx.Colour(58, 110, 165)   # deliberately NOT = COLOR_ACCENT:
                                                    # accent is now orange (UI chrome);
                                                    # keeping "running" a separate blue
                                                    # avoids conflating pane-active with
                                                    # measurement-running
COLOR_STATUS_WARNING  = wx.Colour(191, 138, 39)   # muted amber
COLOR_STATUS_ERROR    = wx.Colour(178, 58, 58)    # muted red

# --- AUI dock art (dockable pane captions, sashes, borders) -------------
COLOR_AUI_CAPTION_ACTIVE        = COLOR_ACCENT
COLOR_AUI_CAPTION_ACTIVE_TEXT   = wx.Colour(255, 255, 255)
COLOR_AUI_CAPTION_INACTIVE      = wx.Colour(223, 225, 229)
COLOR_AUI_CAPTION_INACTIVE_TEXT = wx.Colour(70, 73, 79)
COLOR_AUI_SASH                  = COLOR_APP_BG
COLOR_AUI_BORDER                = COLOR_BORDER
