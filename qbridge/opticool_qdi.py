"""opticool_qdi.py — qbridge adapter for the Quantum Design OptiCool via
the QDInstrument .NET remote server (QDInstrument_Server.exe, port
11000) — the same interface the LabVIEW Remote VIs use.

USE THIS VARIANT when the OptiCool software is too old for MultiPyVu's
OLE path (e.g. package 1.0.1: MultiVu OLE lacks GetTemperature — the
verified situation this adapter was written for). opticool.py (the
MultiPyVu variant) is the preferred choice once the OptiCool software
is updated; both serve the SAME verbs, so nothing in qmeas changes
when you switch.

Where to run it: anywhere that can reach the OptiCool PC — including
the qmeas machine itself (recommended: zero extra installs on the
OptiCool PC). Requirements HERE:
    pip install pythonnet
    QDInstrument.dll from Quantum Design (it ships with the LabVIEW
    remote package; on the OptiCool PC it lives at
    C:\\QDOptiCool\\LabVIEW\\QDInstrument.dll — copy it next to this
    file). If Windows flagged the copy as web-downloaded:
    right-click > Properties > Unblock, or the CLR refuses to load it.
On the OptiCool control PC: MultiVu + QDInstrument_Server.exe running
(already true wherever the LabVIEW Remote VIs work).

Run:  py opticool_qdi.py --qd-host 192.168.0.30
Then add to qmeas as TCPIP0::<this-PC's-ip>::5100::SOCKET (or
localhost if qmeas runs on the same machine), terminators \\n.

Verbs (identical to opticool.py, minus AUXTEMP? — the QDInstrument
interface does not expose the auxiliary thermometer):
  TEMP?  TSTATE?  FIELD?  FSTATE?
  TEMP <K>,<K/min>[,<approach>]      approach: fast_settle | no_overshoot
  FIELD <Oe>,<Oe/s>[,<approach>]     approach: linear | no_overshoot | oscillate
  FHOLD                              freeze magnet at current field (onhold)
  *IDN?

NOTE on state strings: TSTATE?/FSTATE? return the .NET enum member's
name from the DLL (e.g. 'TemperatureStable' — spelling is
DLL-version-dependent and NOT identical to the MultiPyVu variant's
strings). Before using one as a verify target, do a Query Once and
copy the exact text.
"""
import sys

from qbridge_core import BridgeCommandError, run_bridge, standard_argparser

DEFAULT_PORT = 5100
_FHOLD_RATE_OE_S = 110.0


def _find_enum_member(enum_type, wanted: str, what: str):
    """Map a user word ('fast_settle', 'no_overshoot', 'linear', ...)
    onto the DLL's enum member, tolerating the DLL's own casing
    (FastSettle vs fast_settle): compare case-insensitively with
    underscores stripped. Errors list the DLL's actual members."""
    import System
    names = list(System.Enum.GetNames(enum_type))
    canon = wanted.replace('_', '').replace(' ', '').lower()
    for name in names:
        if name.replace('_', '').lower() == canon:
            return getattr(enum_type, name)
    raise BridgeCommandError(
        f'unknown {what} {wanted!r} — this DLL offers: {", ".join(names)}')


class OptiCoolQdiAdapter:
    name = 'OptiCool-QDInstrument'

    def __init__(self, qd_host: str, qd_port: int = 11000, dll_path: str = ''):
        self._host = qd_host
        self._port = qd_port
        self._dll_path = dll_path
        self._qd = None
        self._base = None   # QDInstrumentBase, for the enum types

        self.commands = {
            'TEMP?': self._get_temp,
            'TSTATE?': self._get_temp_state,
            'FIELD?': self._get_field,
            'FSTATE?': self._get_field_state,
            'TEMP': self._set_temp,
            'FIELD': self._set_field,
            'FHOLD': self._field_hold,
        }

    # --- lifecycle -----------------------------------------------------
    def open(self):
        try:
            import clr
        except ImportError:
            sys.exit("pythonnet is not installed — run 'pip install pythonnet' "
                     "on this machine first.")
        if self._dll_path:
            from System.Reflection import Assembly
            clr.AddReference(Assembly.LoadFrom(self._dll_path))
        else:
            try:
                clr.AddReference('QDInstrument')   # next to this file / CWD / GAC
            except Exception:
                sys.exit('QDInstrument.dll not found — copy it next to this '
                         'script (and Unblock it in its file Properties), or '
                         'pass --dll with the full path.')
        from QuantumDesign.QDInstrument import (QDInstrumentBase,
                                                QDInstrumentFactory)
        self._base = QDInstrumentBase
        itype = _find_enum_member(QDInstrumentBase.QDInstrumentType,
                                  'OptiCool', 'instrument type')
        self._qd = QDInstrumentFactory.GetQDInstrument(
            itype, True, self._host, self._port)

    def close(self):
        self._qd = None   # no explicit close in the QDInstrument API

    def reconnect(self):
        """The .NET handle can go stale if QDInstrument_Server restarts;
        rebuild it once before the framework retries the command."""
        from QuantumDesign.QDInstrument import QDInstrumentFactory
        itype = _find_enum_member(self._base.QDInstrumentType,
                                  'OptiCool', 'instrument type')
        self._qd = QDInstrumentFactory.GetQDInstrument(
            itype, True, self._host, self._port)

    # --- .NET call helpers ---------------------------------------------
    # GetTemperature/GetField take ref args; pythonnet 3.x returns them
    # in the result tuple — value second-to-last, status last (position
    # holds whether or not an int error code is also returned first).
    # The bare-int-into-ref-enum coercion does NOT work in pythonnet 3
    # ('No method matches'): a real enum instance must be passed.
    def _ref_read(self, method, status_enum):
        import System
        status0 = System.Enum.ToObject(status_enum, 0)
        result = method(0.0, status0)
        return float(result[-2]), str(result[-1])

    # --- reads ---------------------------------------------------------
    def _get_temp(self, args):
        value, _state = self._ref_read(self._qd.GetTemperature,
                                       self._base.TemperatureStatus)
        return f'{value:.4f}'

    def _get_temp_state(self, args):
        _value, state = self._ref_read(self._qd.GetTemperature,
                                       self._base.TemperatureStatus)
        return state

    def _get_field(self, args):
        value, _state = self._ref_read(self._qd.GetField,
                                       self._base.FieldStatus)
        return f'{value:.2f}'

    def _get_field_state(self, args):
        _value, state = self._ref_read(self._qd.GetField,
                                       self._base.FieldStatus)
        return state

    # --- writes --------------------------------------------------------
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

    def _set_temp(self, args):
        parts = self._parse_args(args, 'TEMP', 2, 3)
        setpoint = self._num(parts[0], 'temperature setpoint (K)')
        rate = self._num(parts[1], 'rate (K/min)')
        approach = _find_enum_member(self._base.TemperatureApproach,
                                     parts[2] if len(parts) == 3 else 'fast_settle',
                                     'temperature approach')
        self._qd.SetTemperature(setpoint, rate, approach)
        return 'OK'

    def _set_field(self, args):
        parts = self._parse_args(args, 'FIELD', 2, 3)
        setpoint = self._num(parts[0], 'field setpoint (Oe)')
        rate = self._num(parts[1], 'rate (Oe/s)')
        approach = _find_enum_member(self._base.FieldApproach,
                                     parts[2] if len(parts) == 3 else 'linear',
                                     'field approach')
        mode = _find_enum_member(self._base.FieldMode, 'driven', 'field mode')
        self._qd.SetField(setpoint, rate, approach, mode)
        return 'OK'

    def _field_hold(self, args):
        value, _state = self._ref_read(self._qd.GetField,
                                       self._base.FieldStatus)
        approach = _find_enum_member(self._base.FieldApproach, 'linear',
                                     'field approach')
        mode = _find_enum_member(self._base.FieldMode, 'driven', 'field mode')
        self._qd.SetField(value, _FHOLD_RATE_OE_S, approach, mode)
        return f'OK held at {value:.2f}'


def main():
    parser = standard_argparser(
        'qbridge for the Quantum Design OptiCool via the QDInstrument '
        '.NET remote server (LabVIEW interface) — for OptiCool software '
        'too old for MultiPyVu.', DEFAULT_PORT)
    parser.add_argument('--qd-host', required=True,
                        help="the OptiCool control PC's IP (e.g. 192.168.0.30)")
    parser.add_argument('--qd-port', type=int, default=11000,
                        help='QDInstrument_Server port (default: 11000)')
    parser.add_argument('--dll', default='',
                        help='full path to QDInstrument.dll (default: search '
                             'next to this script / CWD / GAC)')
    args = parser.parse_args()
    run_bridge(OptiCoolQdiAdapter(args.qd_host, args.qd_port, args.dll),
               port=args.port, bind_host=args.bind)


if __name__ == '__main__':
    main()
