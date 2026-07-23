"""opticool.py — qbridge adapter for the Quantum Design OptiCool.

Run this ON (or near) the OptiCool control PC. Prerequisites there:
  1. MultiVu running (as always).
  2. MultiPyVu installed (pip install MultiPyVu) and its Server
     running next to MultiVu:  python -m MultiPyVu
  3. This bridge:              py opticool.py            (port 5100)

In qmeas, add the cryostat PC as an ordinary raw-socket device
(TCPIP0::<cryostat-pc-ip>::5100::SOCKET, write/read terminator \\n)
and define commands from the verb table below. Everything qmeas can do
with a socket instrument then just works: sweeps, verify, while-rows,
green-LED logging of temperature during any other device's sweep.

Verbs (one line in, one line out):
  TEMP?                     -> temperature in K, e.g. '4.2130'
  TSTATE?                   -> temperature status string, e.g. 'Stable'
                               (others: 'Tracking', 'Near', 'Chasing',
                               'Standby')
  AUXTEMP?                  -> OptiCool auxiliary thermometer, K
  FIELD?                    -> field in Oe, e.g. '10000.0'
  FSTATE?                   -> field status string, e.g.
                               'Holding (driven)' (others: 'Ramping',
                               'Charging Error', ...)
  TEMP <K>,<K/min>[,<approach>]
                            -> set temperature. approach: fast_settle
                               (default) or no_overshoot. Returns 'OK'.
  FIELD <Oe>,<Oe/s>[,<approach>]
                            -> set field. approach: linear (default),
                               no_overshoot, or oscillate. Returns 'OK'.
  FHOLD                     -> read the current field and command it as
                               the new setpoint at maximum rate —
                               freezes the magnet where it is. Meant as
                               the command string of a qmeas 'onhold'
                               write, so Stop/abort parks the magnet.
  *IDN?                     -> bridge identification (framework)

Units follow MultiPyVu/MultiVu conventions: Kelvin, K/min for
temperature; Oersted, Oe/s for field (10000 Oe = 1 T).

Waiting for stability is deliberately NOT a bridge verb: do it in
qmeas, where it belongs — a verify on FSTATE? = 'Holding (driven)'
(or TSTATE? = 'Stable'), or a while-row polling TEMP? — so the wait
is visible, abortable, and logged with data, instead of silently
blocking a socket.
"""
import sys

from qbridge_core import BridgeCommandError, run_bridge, standard_argparser

DEFAULT_PORT = 5100
_FHOLD_RATE_OE_S = 110.0   # near the OptiCool's maximum ramp rate


class OptiCoolAdapter:
    name = 'OptiCool-MultiPyVu'

    def __init__(self, mpv_host: str = 'localhost', mpv_port: int = 5000):
        self._host = mpv_host
        self._port = mpv_port
        self._client = None
        self._mpv = None   # the MultiPyVu module, imported lazily in open()

        self.commands = {
            'TEMP?': self._get_temp,
            'TSTATE?': self._get_temp_state,
            'AUXTEMP?': self._get_aux_temp,
            'FIELD?': self._get_field,
            'FSTATE?': self._get_field_state,
            'TEMP': self._set_temp,
            'FIELD': self._set_field,
            'FHOLD': self._field_hold,
        }

    # --- lifecycle -----------------------------------------------------
    def open(self):
        try:
            import MultiPyVu
        except ImportError:
            sys.exit("MultiPyVu is not installed here — run 'pip install MultiPyVu' "
                     "on this PC first (and start its server: python -m MultiPyVu).")
        self._mpv = MultiPyVu
        self._client = MultiPyVu.Client(host=self._host, port=self._port)
        self._client.open()

    def close(self):
        if self._client is not None:
            try:
                self._client.close_client()
            except Exception:
                pass
            self._client = None

    def reconnect(self):
        """The MultiPyVu session can die independently of this bridge
        (its server restarted, network blip). Drop and rebuild it once
        before the framework retries the failed command."""
        self.close()
        self._client = self._mpv.Client(host=self._host, port=self._port)
        self._client.open()

    # --- helpers -------------------------------------------------------
    @staticmethod
    def _parse_args(args: str, verb: str, minimum: int, maximum: int):
        parts = [p.strip() for p in args.split(',')] if args else []
        if not (minimum <= len(parts) <= maximum):
            raise BridgeCommandError(
                f'{verb} takes {minimum}..{maximum} comma-separated values, got {len(parts)}')
        return parts

    @staticmethod
    def _num(text: str, what: str) -> float:
        try:
            return float(text)
        except ValueError:
            raise BridgeCommandError(f'{what} must be a number, got {text!r}')

    def _enum_member(self, enum, text: str, what: str):
        try:
            return enum[text]
        except KeyError:
            options = ', '.join(m.name for m in enum)
            raise BridgeCommandError(f'unknown {what} {text!r} — options: {options}')

    # --- reads ---------------------------------------------------------
    def _get_temp(self, args):
        temperature, _state = self._client.get_temperature()
        return f'{temperature:.4f}'

    def _get_temp_state(self, args):
        _temperature, state = self._client.get_temperature()
        return state

    def _get_aux_temp(self, args):
        temperature, _state = self._client.get_aux_temperature()
        return f'{temperature:.4f}'

    def _get_field(self, args):
        field, _state = self._client.get_field()
        return f'{field:.2f}'

    def _get_field_state(self, args):
        _field, state = self._client.get_field()
        return state

    # --- writes --------------------------------------------------------
    def _set_temp(self, args):
        parts = self._parse_args(args, 'TEMP', 2, 3)
        setpoint = self._num(parts[0], 'temperature setpoint (K)')
        rate = self._num(parts[1], 'rate (K/min)')
        approach = self._client.temperature.approach_mode.fast_settle
        if len(parts) == 3:
            approach = self._enum_member(self._client.temperature.approach_mode,
                                         parts[2], 'temperature approach')
        self._client.set_temperature(setpoint, rate, approach)
        return 'OK'

    def _set_field(self, args):
        parts = self._parse_args(args, 'FIELD', 2, 3)
        setpoint = self._num(parts[0], 'field setpoint (Oe)')
        rate = self._num(parts[1], 'rate (Oe/s)')
        approach = self._client.field.approach_mode.linear
        if len(parts) == 3:
            approach = self._enum_member(self._client.field.approach_mode,
                                         parts[2], 'field approach')
        self._client.set_field(setpoint, rate, approach)
        return 'OK'

    def _field_hold(self, args):
        field, _state = self._client.get_field()
        self._client.set_field(field, _FHOLD_RATE_OE_S,
                               self._client.field.approach_mode.linear)
        return f'OK held at {field:.2f}'


def main():
    parser = standard_argparser(
        'qbridge for the Quantum Design OptiCool (via MultiPyVu).',
        DEFAULT_PORT)
    parser.add_argument('--mpv-host', default='localhost',
                        help='host running the MultiPyVu server (default: localhost)')
    parser.add_argument('--mpv-port', type=int, default=5000,
                        help='MultiPyVu server port (default: 5000)')
    args = parser.parse_args()
    run_bridge(OptiCoolAdapter(args.mpv_host, args.mpv_port),
               port=args.port, bind_host=args.bind)


if __name__ == '__main__':
    main()
