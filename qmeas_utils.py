"""
qmeas_utils.py  –  Shared path and image helpers.
Imported by qmeas.py AND all sub-modules.
Must not import anything from qmeas or sub_*.
"""

import os
import platform
import subprocess
from pathlib import Path


def app_root() -> Path:
    """Directory that contains qmeas.py (or the frozen exe)."""
    return Path(os.path.dirname(os.path.realpath(__file__)))


def img(name: str) -> str:
    """Return absolute path to an image file, or empty string if missing."""
    p = app_root() / 'images' / name
    return str(p) if p.is_file() else ''


def dev_dir() -> Path:
    """Return Path to the devices folder next to the script."""
    return app_root() / 'devices'


def custom_dir() -> Path:
    """Return Path to the 'custom' folder next to the script — where
    user-supplied .py files for the virtual 'script' device live."""
    return app_root() / 'custom'


def open_file_cross_platform(path: str) -> None:
    """Open a file with the system default application (cross-platform)."""
    try:
        if platform.system() == 'Windows':
            os.startfile(path)
        elif platform.system() == 'Darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception:
        pass


def open_text_editor(path: str) -> None:
    """Open a text file in the platform default editor."""
    try:
        if platform.system() == 'Windows':
            subprocess.Popen(['notepad.exe', path])
        elif platform.system() == 'Darwin':
            subprocess.Popen(['open', '-e', path])
        else:
            for editor in ('xdg-open', 'gedit', 'kate', 'nano'):
                if subprocess.run(['which', editor],
                                  capture_output=True).returncode == 0:
                    subprocess.Popen([editor, path])
                    return
    except Exception:
        pass


def default_data_dir() -> Path:
    """Best-effort guess for a sensible default data folder."""
    candidate = Path.home() / 'Desktop' / 'qmeas_data'
    return candidate if candidate.is_dir() else Path.home()


_visa_fallback_warned = False


def get_resource_manager():
    """Return a working pyvisa ResourceManager, or None if none available.

    Tries the default (vendor/IVI, e.g. NI-VISA) backend first. If that fails,
    falls back to the pure-Python backend ('@py') and warns the user once per
    application run. Returns None only if both backends fail.
    """
    global _visa_fallback_warned
    import pyvisa
    try:
        return pyvisa.ResourceManager()
    except Exception:
        pass
    try:
        rm = pyvisa.ResourceManager('@py')
    except Exception:
        return None
    if not _visa_fallback_warned:
        _visa_fallback_warned = True
        try:
            import wx
            wx.MessageBox(
                'The system VISA library (NI-VISA) could not be loaded.\n'
                'qmeas is now using the pure-Python backend (pyvisa-py) instead.\n\n'
                'TCPIP (socket/VXI-11) instruments will work normally.\n'
                'GPIB and USB instruments will NOT work on this backend.\n\n'
                'To restore full support, repair or reinstall NI-VISA.',
                'VISA fallback active', wx.OK | wx.ICON_EXCLAMATION)
        except Exception:
            pass
    return rm
