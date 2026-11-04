"""Single-owner ctypes binding to the additive native paper-portfolio ABI."""

import ctypes as c
import hashlib
from pathlib import Path
import threading

from .native import Event, Snapshot


class PortfolioSnapshot(c.Structure):
    _fields_ = [(name, c.c_int64) for name in (
        'initial_capital_paise', 'cash_paise', 'equity_paise', 'pnl_paise', 'fees_paise',
        'gross_notional_paise', 'reserved_cash_paise', 'open_orders', 'killed', 'audit_failed',
        'kill_reason', 'instrument_count', 'ready', 'execution_enabled')]


class InstrumentSnapshot(c.Structure):
    _fields_ = [('instrument', c.c_int64), ('ready', c.c_int64), ('state', Snapshot)]


class PortfolioEvent(c.Structure):
    _fields_ = [('instrument', c.c_int64), ('state', Event)]


class PaperPortfolioEngine:
    def __init__(self, library: Path, config: Path):
        self.owner = threading.get_ident()
        self.handle = None
        self.binary_sha256 = hashlib.sha256(library.read_bytes()).hexdigest()
        self.library = c.CDLL(str(library.resolve()))
        declarations = {
            'portfolio_api_version': (c.c_int, []),
            'portfolio_create': (c.c_void_p, [c.c_char_p, c.c_void_p, c.c_size_t]),
            'portfolio_destroy': (None, [c.c_void_p]),
            'portfolio_tick': (c.c_int, [c.c_void_p, c.c_uint64, c.c_uint64, c.c_uint32,
                                       c.c_int64, c.c_int64, c.c_int32, c.c_int32]),
            'portfolio_heartbeat': (c.c_int, [c.c_void_p, c.c_uint64]),
            'portfolio_suspend': (c.c_int, [c.c_void_p, c.c_uint64]),
            'portfolio_resume': (c.c_int, [c.c_void_p, c.c_uint64]),
            'portfolio_disconnect': (None, [c.c_void_p]),
            'portfolio_shutdown': (None, [c.c_void_p]),
            'portfolio_snapshot': (c.c_int, [c.c_void_p, c.POINTER(PortfolioSnapshot)]),
            'portfolio_instrument_snapshot': (c.c_int, [c.c_void_p, c.c_size_t, c.POINTER(InstrumentSnapshot)]),
            'portfolio_pop_event': (c.c_int, [c.c_void_p, c.POINTER(PortfolioEvent)]),
        }
        for name, (result, arguments) in declarations.items():
            try:
                function = getattr(self.library, name)
            except AttributeError as error:
                raise RuntimeError('Portfolio API unavailable; rebuild paper_engine from the updated source') from error
            function.restype, function.argtypes = result, arguments
        if (self.library.portfolio_api_version() != 1 or c.sizeof(PortfolioSnapshot) != 112 or
                c.sizeof(InstrumentSnapshot) != 96 or c.sizeof(PortfolioEvent) != 128):
            raise RuntimeError('Incompatible paper portfolio ABI')
        error = c.create_string_buffer(1024)
        self.handle = self.library.portfolio_create(str(config.resolve()).encode('utf-8'), error, len(error))
        if not self.handle:
            raise ValueError(error.value.decode('utf-8', errors='replace'))

    def _check_owner(self):
        if threading.get_ident() != self.owner or not self.handle:
            raise RuntimeError('Paper portfolio must be called by its owning thread while open')

    def _time(self, now_ns):
        self._check_owner()
        if type(now_ns) is not int or not 0 <= now_ns <= 2**63 - 1:
            self.disconnect()
            raise ValueError('Invalid portfolio monotonic time')

    def tick(self, instrument, now_ns, sequence, bid, ask, quantity, session_open=True):
        self._time(now_ns)
        if (not all(type(value) is int for value in (instrument, sequence, bid, ask, quantity)) or
                not 0 < instrument <= 2**32 - 1 or not 0 < sequence <= 2**63 - 1 or
                not 0 < bid <= ask <= 100_000_000 or not 0 <= quantity <= 1_000_000 or
                type(session_open) is not bool):
            self.disconnect()
            raise ValueError('Invalid normalized portfolio quote')
        return self.library.portfolio_tick(self.handle, now_ns, sequence, instrument, bid, ask,
                                           quantity, int(session_open))

    def heartbeat(self, now_ns):
        self._time(now_ns)
        if self.library.portfolio_heartbeat(self.handle, now_ns) != 1:
            raise RuntimeError('Portfolio rejected heartbeat time')

    def suspend(self, now_ns):
        self._time(now_ns)
        if self.library.portfolio_suspend(self.handle, now_ns) != 1:
            raise RuntimeError('Portfolio suspension failed; inspect audit state')

    def resume_execution(self, now_ns):
        self._time(now_ns)
        code = self.library.portfolio_resume(self.handle, now_ns)
        if code not in (0, 1):
            raise RuntimeError('Portfolio rejected resume time')
        return bool(code)

    def disconnect(self):
        self._check_owner()
        self.library.portfolio_disconnect(self.handle)

    def shutdown(self):
        self._check_owner()
        self.library.portfolio_shutdown(self.handle)

    def snapshot(self):
        self._check_owner()
        result = PortfolioSnapshot()
        if self.library.portfolio_snapshot(self.handle, c.byref(result)) != 1:
            raise RuntimeError('Unable to read portfolio account state')
        report = {name: getattr(result, name) for name, _ in PortfolioSnapshot._fields_}
        report['instruments'] = []
        for index in range(result.instrument_count):
            member = InstrumentSnapshot()
            if self.library.portfolio_instrument_snapshot(self.handle, index, c.byref(member)) != 1:
                raise RuntimeError('Unable to read portfolio instrument state')
            report['instruments'].append(dict(instrument=member.instrument, ready=member.ready,
                **{name: getattr(member.state, name) for name, _ in Snapshot._fields_}))
        return report

    def events(self):
        self._check_owner()
        result = PortfolioEvent()
        while True:
            code = self.library.portfolio_pop_event(self.handle, c.byref(result))
            if code == 0:
                return
            if code != 1:
                raise RuntimeError('Unable to read portfolio audit events')
            yield dict(instrument=result.instrument, **{
                name: getattr(result.state, name).decode('ascii') if name in ('event', 'reason')
                else getattr(result.state, name) for name, _ in Event._fields_})

    def close(self):
        if self.handle:
            self._check_owner()
            self.library.portfolio_destroy(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
