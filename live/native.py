"""Small, single-owner ctypes binding to the real C++ paper engine."""

import ctypes as c
from pathlib import Path
import threading


class Snapshot(c.Structure):
    _fields_ = [(name, c.c_int64) for name in (
        'position', 'cash_paise', 'fees_paise', 'pnl_paise', 'bid_paise', 'ask_paise',
        'open_orders', 'killed', 'audit_failed', 'kill_reason')]


class Event(c.Structure):
    _fields_ = [(name, c.c_int64) for name in (
        'time_ns', 'order_id', 'side', 'status', 'price_paise', 'quantity', 'fee_paise',
        'position', 'equity_paise')] + [('event', c.c_char * 16), ('reason', c.c_char * 32)]


class PaperEngine:
    def __init__(self, library: Path, config: Path):
        self.owner = threading.get_ident()
        self.library = c.CDLL(str(library.resolve()))
        declarations = {
            'paper_api_version': (c.c_int, []),
            'paper_create': (c.c_void_p, [c.c_char_p, c.c_void_p, c.c_size_t]),
            'paper_destroy': (None, [c.c_void_p]),
            'paper_tick': (c.c_int, [c.c_void_p, c.c_uint64, c.c_uint64, c.c_int64, c.c_int64,
                                   c.c_int32, c.c_int32, c.c_int32]),
            'paper_heartbeat': (c.c_int, [c.c_void_p, c.c_uint64]),
            'paper_cancel_all': (c.c_int, [c.c_void_p, c.c_uint64]),
            'paper_disconnect': (None, [c.c_void_p]),
            'paper_shutdown': (None, [c.c_void_p]),
            'paper_snapshot': (c.c_int, [c.c_void_p, c.POINTER(Snapshot)]),
            'paper_pop_event': (c.c_int, [c.c_void_p, c.POINTER(Event)]),
        }
        for name, (result, arguments) in declarations.items():
            function = getattr(self.library, name)
            function.restype, function.argtypes = result, arguments
        if self.library.paper_api_version() != 2 or c.sizeof(Snapshot) != 80 or c.sizeof(Event) != 120:
            raise RuntimeError('Incompatible paper engine ABI')
        error = c.create_string_buffer(1024)
        self.handle = self.library.paper_create(str(config.resolve()).encode('utf-8'), error, len(error))
        if not self.handle:
            raise ValueError(error.value.decode('utf-8', errors='replace'))

    def _check_owner(self):
        if threading.get_ident() != self.owner or not self.handle:
            raise RuntimeError('Paper engine must be called by its owning thread while open')

    def tick(self, now_ns, sequence, bid, ask, quantity, session_open=True):
        self._check_owner()
        if not (all(type(value) is int for value in (now_ns, sequence, bid, ask, quantity)) and
                0 <= now_ns <= 2**63 - 1 and 0 < sequence <= 2**63 - 1 and
                0 < bid <= ask <= 100_000_000 and 0 <= quantity <= 1_000_000 and
                isinstance(session_open, bool)):
            self.disconnect()
            raise ValueError('Invalid normalized quote')
        return self.library.paper_tick(self.handle, now_ns, sequence, bid, ask, quantity,
                                       quantity, int(session_open))

    def heartbeat(self, now_ns):
        self._check_owner()
        if type(now_ns) is not int or not 0 <= now_ns <= 2**63 - 1:
            raise ValueError('Invalid monotonic time')
        if self.library.paper_heartbeat(self.handle, now_ns) != 1:
            raise RuntimeError('Engine rejected heartbeat time')

    def cancel_all(self, now_ns):
        self._check_owner()
        if type(now_ns) is not int or not 0 <= now_ns <= 2**63 - 1:
            raise ValueError('Invalid monotonic time')
        if self.library.paper_cancel_all(self.handle, now_ns) != 1:
            raise RuntimeError('Engine rejected cancel-all time or audit output failed')

    def disconnect(self):
        self._check_owner()
        self.library.paper_disconnect(self.handle)

    def shutdown(self):
        self._check_owner()
        self.library.paper_shutdown(self.handle)

    def snapshot(self):
        self._check_owner()
        result = Snapshot()
        if self.library.paper_snapshot(self.handle, c.byref(result)) != 1:
            raise RuntimeError('Unable to read paper state')
        return {name: getattr(result, name) for name, _ in Snapshot._fields_}

    def events(self):
        self._check_owner()
        result = Event()
        while True:
            code = self.library.paper_pop_event(self.handle, c.byref(result))
            if code == 0:
                return
            if code != 1:
                raise RuntimeError('Unable to read audit events')
            yield {name: (getattr(result, name).decode('ascii') if name in ('event', 'reason')
                          else getattr(result, name)) for name, _ in Event._fields_}

    def close(self):
        if self.handle:
            self._check_owner()
            self.library.paper_destroy(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
