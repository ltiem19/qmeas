"""qbridge_core.py — the generic half of qbridge.

qbridge makes devices WITHOUT a network instrument interface (OLE-only,
vendor-DLL-only, Python-SDK-only, ...) look like an ordinary raw-socket
SCPI instrument, so qmeas (or anything else that speaks "text line in,
text line out over TCP") can use them like any other device.

This file is the device-independent framework: a small threaded TCP
server that accepts line-based commands and dispatches them to an
ADAPTER — a plain object supplying the device-specific verbs. One
adapter file per weird device; this file never changes.

Wire contract (deliberately the least surprising one possible):
  - client connects over TCP (any number of sequential connections;
    connect-per-command and long-lived connections both work)
  - one request  = one line, terminated '\n' ('\r\n' tolerated)
  - one response = one line, terminated '\n'  — ALWAYS, including for
    set/write commands ('OK'), so a human on telnet/PuTTY sees what
    happened and a draining client is never left hanging
  - errors are a single line starting 'ERR: ' — visible in recorded
    data rather than silently absent
  - '*IDN?' is answered by the framework itself, so an instrument-
    tester that probes with *IDN? (qmeas's Test Connection does) gets
    a meaningful reply without adapter involvement

Adapter contract (see opticool.py for the worked example):
  name        : str — reported in *IDN?
  commands    : dict mapping VERB (uppercase, no args) -> callable.
                The callable receives the argument string that followed
                the verb ('' if none) and returns the response string
                (without terminator). Raise BridgeCommandError for a
                user-facing problem ('bad argument'); any other
                exception is reported as an internal error with its
                message.
  open()      : called once at startup (connect to the real backend).
  close()     : called at shutdown.
  reconnect() : optional; called after a command raised, before ONE
                retry of that command — for backends whose session can
                die (network hiccup to an upstream server). Omit it to
                disable the retry.
"""
import argparse
import logging
import socket
import socketserver
import sys
import threading
import time

log = logging.getLogger('qbridge')


class BridgeCommandError(Exception):
    """A problem the CALLER caused (unknown verb, malformed argument) —
    reported as 'ERR: <message>' with no reconnect/retry attempted."""


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = self.client_address[0]
        log.info('connection from %s', peer)
        while True:
            try:
                raw = self.rfile.readline()
            except (ConnectionError, OSError):
                break
            if not raw:
                break   # peer closed
            line = raw.decode('ascii', errors='replace').strip()
            if not line:
                continue
            response = self.server.bridge.dispatch(line)
            try:
                self.wfile.write((response + '\n').encode('ascii', errors='replace'))
            except (ConnectionError, OSError):
                break
        log.info('connection from %s closed', peer)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Bridge:
    """Owns the adapter and serializes access to it: commands from all
    connections funnel through one lock, because the typical backend
    (an OLE server, a single-session upstream client) is not
    concurrency-safe. At measurement poll rates this costs nothing."""

    def __init__(self, adapter):
        self.adapter = adapter
        self._lock = threading.Lock()

    def dispatch(self, line: str) -> str:
        verb, _, args = line.partition(' ')
        verb = verb.strip().upper()
        args = args.strip()
        if verb == '*IDN?':
            return f'qbridge,{self.adapter.name},line-protocol,1.0'
        handler = self.adapter.commands.get(verb)
        if handler is None:
            known = ' '.join(sorted(self.adapter.commands))
            return f'ERR: unknown command {verb!r} — known: *IDN? {known}'
        with self._lock:
            try:
                return str(handler(args))
            except BridgeCommandError as e:
                return f'ERR: {e}'
            except Exception as e:
                # Backend trouble (upstream session died, ...): one
                # reconnect + retry if the adapter supports it, so a
                # transient upstream hiccup doesn't require anyone to
                # restart the bridge.
                reconnect = getattr(self.adapter, 'reconnect', None)
                if reconnect is None:
                    log.exception('command %s failed', verb)
                    return f'ERR: {e}'
                log.warning('command %s failed (%s) — reconnecting and retrying once', verb, e)
                try:
                    reconnect()
                    return str(handler(args))
                except BridgeCommandError as e2:
                    return f'ERR: {e2}'
                except Exception as e2:
                    log.exception('command %s failed again after reconnect', verb)
                    return f'ERR: {e2}'


def run_bridge(adapter, port: int, bind_host: str = '0.0.0.0'):
    """Open the adapter, serve forever, close on Ctrl+C. Blocking."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        datefmt='%H:%M:%S')
    log.info('opening adapter %r ...', adapter.name)
    adapter.open()
    log.info('adapter ready; serving on %s:%d', bind_host, port)
    server = _Server((bind_host, port), _Handler)
    server.bridge = Bridge(adapter)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('shutting down')
    finally:
        server.server_close()
        try:
            adapter.close()
        except Exception:
            pass


def standard_argparser(description: str, default_port: int) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument('--port', type=int, default=default_port,
                   help=f'TCP port qmeas connects to (default {default_port})')
    p.add_argument('--bind', default='0.0.0.0',
                   help='interface to listen on (default: all)')
    return p
