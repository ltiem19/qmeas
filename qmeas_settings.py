"""
qmeas_settings.py  –  Persistent user settings for qmeas (v2).

Plain ASCII INI-format text file (via configparser) — human-readable
and hand-editable, matching the general preference for clean text
files over binary/JSON formats. Lives next to qmeas.py itself (same
convention as dev_dir() in qmeas_utils) rather than the user's home
directory, so the whole install stays self-contained in one folder.
"""

import configparser
from pathlib import Path

from qmeas_utils import app_root

SETTINGS_FILE = app_root() / '.qmeas_settings.ini'

DEFAULTS = {
    'data_path':    str(Path.home() / 'qmeas_data'),
    'file_suffix':  '',
    'last_devfile': '',
}


def load_settings() -> dict:
    """Returns DEFAULTS with any values found in the settings file
    overlaid. Falls back silently to defaults if the file is missing,
    unreadable, or malformed — a broken settings file shouldn't prevent
    the app from starting."""
    settings = dict(DEFAULTS)
    if SETTINGS_FILE.exists():
        parser = configparser.ConfigParser()
        try:
            parser.read(SETTINGS_FILE, encoding='ascii')
            if 'general' in parser:
                for key in DEFAULTS:
                    if key in parser['general']:
                        settings[key] = parser['general'][key]
        except Exception:
            pass
    return settings


def save_settings(settings: dict) -> None:
    parser = configparser.ConfigParser()
    parser['general'] = {key: str(settings.get(key, DEFAULTS[key])) for key in DEFAULTS}
    with open(SETTINGS_FILE, 'w', encoding='ascii', errors='replace') as f:
        parser.write(f)
