"""toptica_bridge.py — qbridge adapter for TOPTICA DLC pro / DLC smart
controllers (TeraScan et al.) via the official toptica_lasersdk.

(The file is NOT called toptica.py on purpose: that name would shadow
the SDK's own 'toptica' package for any Python started in this folder,
making the SDK unimportable — a bug that shipped in the first version
of this adapter and cost a lab morning.)

Why a bridge (probe-verified on the lab's DLC smart, fw 3.1.1): the
raw command line on TCP 1998 sends a telnet-negotiation banner on
every connect AND starts sessions at a restricted access level
('Error: -22 no access' until change-ul) — a session-oriented
protocol that qmeas's one-line-per-connection socket model cannot
speak directly. This adapter holds ONE SDK session and exposes the
whole DeCoF parameter tree through three generic verbs, so qmeas
command strings carry the parameter names and NO adapter change is
ever needed for a new parameter.

Requirements here (any PC that reaches the controller — the qmeas PC
is fine):  pip install toptica_lasersdk
Run:       py toptica_bridge.py --toptica-host <controller-ip>
           (add --port 5101 if an OptiCool bridge already uses 5100)
qmeas:     TCPIP0::<this-pc-ip>::5100::SOCKET, terminators \\n.

Verbs:
  GET <param>            -> the parameter's value, e.g.
                            'GET uptime-txt', 'GET laser1:ctl:wavelength-act'
                            (booleans come back as 1/0 — data-friendly)
  SET <param> <value>    -> 'OK'. Value typing: '#t'/'#f'/'true'/'false'
                            -> boolean; contains '.' or exponent ->
                            float; plain digits -> integer; else
                            string. DeCoF parameters are TYPED: if the
                            device rejects an integer for a real-typed
                            parameter, write '10.0' instead of '10' —
                            the error line will say so.
  EXEC <cmd> [args...]   -> run a DeCoF command (scan start/stop etc.);
                            returns its result if any, else 'OK'.
  *IDN?                  -> bridge identification (framework).

qmeas command-string examples:
  query 'GET laser1:ctl:wavelength-act'      (green LED -> logged)
  write 'SET laser1:ctl:wavelength-set [%]'  ([%] = task-row value)

TeraScan parameter names are system-specific: take them from the
TeraScan control software's parameter reference or the specialized
device class on the TOPTICA USB stick (its attribute paths map 1:1
onto DeCoF names).
"""
import sys

from qbridge_core import BridgeCommandError, run_bridge, standard_argparser

DEFAULT_PORT = 5100


def _parse_decof_value(text: str):
    """User text -> typed DeCoF value. Deliberately simple and
    documented rather than clever: booleans by keyword, float if it
    looks fractional/exponential, int if plain digits, else string.
    A type mismatch is the DEVICE's call to reject — the SDK error
    comes back as a visible ERR line naming the parameter."""
    low = text.strip().lower()
    if low in ('#t', 'true', 'on'):
        return True
    if low in ('#f', 'false', 'off'):
        return False
    try:
        if any(c in low for c in ('.', 'e')) and not low.startswith('"'):
            return float(text)
        return int(text)
    except ValueError:
        return text.strip('"')


class TopticaAdapter:
    name = 'Toptica-DeCoF'

    def __init__(self, host: str, command_port: int = 1998):
        self._host = host
        self._cmd_port = command_port
        self._client = None
        self.commands = {
            'GET': self._get,
            'SET': self._set,
            'EXEC': self._exec,
        }

    # --- lifecycle -----------------------------------------------------
    def open(self):
        try:
            from toptica.lasersdk.client import (Client, NetworkConnection,
                                                 UserLevel)
        except ImportError as e:
            # Show the REAL error: 'not installed' is only one of the
            # ways this import fails (a file shadowing the 'toptica'
            # package is another — see the module docstring), and
            # masking the cause behind a guessed message cost real
            # debugging time once already.
            sys.exit(f"Could not import the TOPTICA SDK: {e}\n"
                     f"If it's genuinely missing: py -m pip install toptica_lasersdk\n"
                     f"If the message mentions 'toptica' is not a package: a file "
                     f"named toptica.py is shadowing the SDK — rename it.")
        self._sdk = (Client, NetworkConnection, UserLevel)
        self._client = Client(NetworkConnection(self._host,
                                                command_line_port=self._cmd_port))
        self._client.open()
        # Sessions start restricted ('Error: -22 no access' — observed
        # on the lab's DLC smart): elevate to NORMAL (empty password,
        # the standard user level). Harmless if already there.
        try:
            self._client.change_ul(UserLevel.NORMAL, '')
        except Exception:
            pass   # some firmware may pre-elevate; real access problems
                   # surface per-command as visible ERR lines

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def reconnect(self):
        Client, NetworkConnection, UserLevel = self._sdk
        self.close()
        self._client = Client(NetworkConnection(self._host,
                                                command_line_port=self._cmd_port))
        self._client.open()
        try:
            self._client.change_ul(UserLevel.NORMAL, '')
        except Exception:
            pass

    # --- verbs ---------------------------------------------------------
    @staticmethod
    def _format(value):
        if isinstance(value, bool):
            return '1' if value else '0'
        return str(value)

    def _get(self, args):
        name = args.strip()
        if not name:
            raise BridgeCommandError("GET needs a parameter name, e.g. "
                                     "'GET uptime-txt'")
        return self._format(self._client.get(name))

    def _set(self, args):
        name, _, value_text = args.strip().partition(' ')
        if not name or not value_text.strip():
            raise BridgeCommandError("SET needs a parameter name and a value, "
                                     "e.g. 'SET laser1:ctl:wavelength-set 1550.1'")
        value = _parse_decof_value(value_text)
        status = self._client.set(name, value)
        if isinstance(status, int) and status < 0:
            raise BridgeCommandError(
                f'device rejected SET {name} = {value!r} (status {status}) — '
                f'for a real-typed parameter write e.g. 10.0 instead of 10')
        return 'OK'

    def _exec(self, args):
        parts = args.strip().split()
        if not parts:
            raise BridgeCommandError("EXEC needs a command name, e.g. "
                                     "'EXEC laser1:scan:start'")
        name, cmd_args = parts[0], [_parse_decof_value(p) for p in parts[1:]]
        result = self._client.exec(name, *cmd_args)
        return 'OK' if result is None else self._format(result)


def main():
    parser = standard_argparser(
        'qbridge for TOPTICA DLC pro / DLC smart controllers (generic '
        'DeCoF GET/SET/EXEC via the official toptica_lasersdk).',
        DEFAULT_PORT)
    parser.add_argument('--toptica-host', required=True,
                        help="the controller's IP address")
    parser.add_argument('--toptica-port', type=int, default=1998,
                        help='DeCoF command-line port (default: 1998)')
    args = parser.parse_args()
    run_bridge(TopticaAdapter(args.toptica_host, args.toptica_port),
               port=args.port, bind_host=args.bind)


if __name__ == '__main__':
    main()
