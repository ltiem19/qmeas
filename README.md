# qmeas

A desktop application for running measurement sequences against laboratory
instruments — magnets, source-measure units, lock-in amplifiers, cryostats,
frequency sources — built for low-temperature transport experiments but not
limited to them.

Define your instruments and their commands once, then build measurements as an
ordered list of task rows: sweep any writable quantity, nest sweeps up to four
levels deep (e.g. a frequency sweep at every gate voltage at every magnetic
field), link rows through arithmetic expressions, gate steps on hardware
read-back (verify), wait on conditions (while-loops), and record any set of
readings automatically at every point. Data is written as tab-separated files
as it is measured and plotted live.

## Highlights

- **Instrument-agnostic**: VISA (GPIB/USB), raw TCP sockets, and HTTP/JSON
  instruments side by side; per-device terminators and timeouts.
- **Safe by construction**: field validation before a run, Simulate mode
  (walks the whole task list without touching hardware), abort with automatic
  magnet hold, verify methods that wait for slow actuators (magnets,
  temperature) to actually arrive before the run proceeds.
- **qbridge**: instruments that only speak a vendor API (Quantum Design
  OptiCool via MultiVu/QDInstrument, TOPTICA DLC pro/smart via the laser SDK)
  become ordinary socket devices through small bridge processes — three
  adapters included, a template for writing your own.
- **Built-in AI assistant**: an optional, fully local helper (via
  [Ollama](https://ollama.com)) that answers questions about qmeas and helps
  set up measurements. Nothing leaves your machine. It reads a general
  knowledge file plus your own lab notes, and its answers should be treated as
  suggestions, not authority.
- **No cloud, no telemetry, no accounts.** Plain files on your disk.

## Requirements

- Windows 10/11 (primary target; the code is largely cross-platform but only
  Windows is routinely tested)
- Python 3.11+ (3.13 recommended)
- `pip install wxPython matplotlib pyvisa pyvisa-py`
  - `pyvisa-py` is the VISA backend for network/serial instruments; for
    GPIB instruments install NI-VISA as well (pyvisa-py does not cover
    GPIB on Windows)
- Optional, for the AI assistant: [Ollama](https://ollama.com) and
  `ollama pull llama3.1:8b`
- Optional, per bridge: see the header of the adapter in `qbridge/`

## Getting started

```
py qmeas.py
```

Read `qmanual.pdf` — it covers installation, connecting instruments,
defining commands, LEDs, task rows, nesting/linking, while-loops, the AI
assistant, and the bridges, in about a dozen pages.

## Repository layout

| Path | Purpose |
|---|---|
| `qmeas.py` + `qmeas_*.py` | the application |
| `qmanual.pdf` | the manual (Help -> Manual opens exactly this file) |
| `qbridge/` | bridge framework + OptiCool (×2) and TOPTICA adapters |
| `devices/` | example device lists |
| `images/` | application icon |
| `docs/` | the manual's LaTeX source |
| `qmeas_knowledge_base.md` | the AI assistant's general reference |
| `qmeas_examples.md` | curated Q&A the assistant learns answer style from |
| `custom/` | your script-device Python files live here |

Files created at runtime next to `qmeas.py` (settings, device lists, your
`qmeas_users.md` lab notes) are yours and are not part of the repository.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Project h_e2.

Developed with Anthropic's Claude. The software controls laboratory hardware;
review what a task list will do (Simulate) before running it on instruments
that can hurt themselves or you.
