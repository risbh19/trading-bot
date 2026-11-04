"""Durable paper-session journal, manifest and single-process lock."""

import errno
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
from datetime import datetime, timezone

FORMAT_VERSION = 1
API_VERSION = 2


def _json_bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode('utf-8')


def atomic_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    _replace_report(temporary, path)


def _replace_report(source, target):
    # Windows MoveFileEx can reject replacement while a brief reader holds the
    # target, even with delete sharing. Preserve atomic replacement semantics;
    # retry only these transient sharing/access errors for at most 155 ms.
    delays = (0.005, 0.01, 0.02, 0.04, 0.08)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32) or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def config_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_manifest(output: Path, symbol: str, config_data: bytes) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    config_path = output / 'config.conf'
    with config_path.open('xb') as stream:
        stream.write(config_data)
        stream.flush()
        os.fsync(stream.fileno())
    manifest = {
        'format_version': FORMAT_VERSION,
        'session_id': uuid.uuid4().hex,
        'symbol': symbol,
        'config_sha256': config_digest(config_data),
        'paper_api_version': API_VERSION,
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'running',
        'resume_count': 0,
    }
    atomic_json(output / 'session.json', manifest)
    return manifest


def load_manifest(output: Path, symbol: str, config_data: bytes) -> dict:
    try:
        manifest = json.loads((output / 'session.json').read_text(encoding='utf-8'))
        saved_config = (output / 'config.conf').read_bytes()
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'Cannot read resumable paper-session metadata: {error}') from error
    if manifest.get('format_version') != FORMAT_VERSION or manifest.get('paper_api_version') != API_VERSION:
        raise ValueError('Unsupported paper-session format or engine ABI; refusing automatic recovery')
    if manifest.get('status') != 'running':
        raise ValueError('This paper session is already finished and cannot be resumed')
    if manifest.get('symbol') != symbol:
        raise ValueError('Resume symbol does not match the saved paper session')
    digest = config_digest(saved_config)
    if manifest.get('config_sha256') != digest or digest != config_digest(config_data) or saved_config != config_data:
        raise ValueError('Resume configuration differs from the saved paper session')
    return manifest


def _pid_is_alive(pid: int) -> bool:
    if os.name == 'nt':
        # os.kill(pid, 0) is not a process-existence check on Windows: signal
        # zero is routed through GenerateConsoleCtrlEvent and can return
        # ERROR_INVALID_PARAMETER even for a live process. Query a SYNCHRONIZE
        # handle instead. Any error we cannot identify as "process absent"
        # fails closed so an uncertain lock is never stolen.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = open_process(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() not in (87, 1168)  # invalid PID / not found
        try:
            return wait_for_single_object(handle, 0) != 0  # WAIT_OBJECT_0 means exited
        finally:
            close_handle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH or getattr(error, 'winerror', None) in (87, 1168):
            return False
        return True


class SessionLock:
    """OS-owned lease. A persistent diagnostic file is never unlinked on takeover."""

    def __init__(self, path: Path):
        self.path = path
        self.token = f'v2 {os.getpid()} {uuid.uuid4().hex}\n'.encode('ascii')
        self.held = False
        self.descriptor = None

    @staticmethod
    def _lease(descriptor, acquire):
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)

    def acquire(self) -> None:
        if self.held:
            raise RuntimeError('Paper session is already running under this owner')
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._lease(descriptor, True)
        except OSError as error:
            os.close(descriptor)
            raise RuntimeError('Paper session is already running or its owner lease is unavailable') from error
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            owner = os.read(descriptor, 4096)
            if owner and not owner.startswith(b'v2 '):
                try:
                    pid = int(owner.decode('ascii').split()[0])
                    if pid <= 0:
                        raise ValueError('Invalid legacy owner PID')
                except (UnicodeError, ValueError, IndexError) as error:
                    raise RuntimeError('Existing legacy session lock is unreadable; refusing recovery') from error
                if _pid_is_alive(pid):
                    raise RuntimeError(f'Legacy paper session is already running under process {pid}')
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            remaining = memoryview(self.token)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError('Short write to session owner lease')
                remaining = remaining[written:]
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)  # Closing releases the OS lease even on failed metadata writes.
            raise
        self.descriptor, self.held = descriptor, True

    def release(self) -> None:
        if not self.held:
            return
        descriptor = self.descriptor
        try:
            self._lease(descriptor, False)
        finally:
            os.close(descriptor)
            self.descriptor, self.held = None, False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


class SessionJournal:
    """Append-only JSONL log. Operations are durable before engine calls."""

    def __init__(self, path: Path, *, resume: bool, symbol: str, config_sha256: str, header=None):
        self.path = path
        self.records = []
        expected = header if header is not None else {
            'kind': 'header', 'format_version': FORMAT_VERSION,
            'paper_api_version': API_VERSION, 'symbol': symbol,
            'config_sha256': config_sha256,
        }
        if resume:
            self.records = self._read_and_repair_tail()
            if not self.records or self.records[0] != expected:
                raise ValueError('Session journal header does not match its manifest/configuration')
            self.transactions = self._transactions(self.records[1:])
            self.fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        else:
            self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                self._write(expected)
            except Exception:
                os.close(self.fd)
                self.fd = None
                raise
            self.transactions = []

    @staticmethod
    def _transactions(records):
        result = []
        pending = None
        expected_id = 1
        for record in records:
            if not isinstance(record, dict):
                raise ValueError('Session journal records must be JSON objects')
            if record.get('kind') == 'operation':
                if pending is not None or record.get('id') != expected_id:
                    raise ValueError('Session journal operation sequence is invalid')
                pending = record
            elif record.get('kind') == 'checkpoint':
                if pending is None or record.get('id') != pending['id']:
                    raise ValueError('Session journal checkpoint does not match an operation')
                result.append((pending, record))
                pending = None
                expected_id += 1
            else:
                raise ValueError('Session journal contains an unknown record')
        if pending is not None:
            result.append((pending, None))
        return result

    def _read_and_repair_tail(self):
        data = self.path.read_bytes()
        last_newline = data.rfind(b'\n')
        complete = data[:last_newline + 1] if last_newline >= 0 else b''
        if len(complete) != len(data):
            with self.path.open('r+b') as stream:
                stream.truncate(len(complete))
                stream.flush()
                os.fsync(stream.fileno())
        records = []
        for line_number, line in enumerate(complete.splitlines(), 1):
            try:
                records.append(json.loads(line.decode('utf-8')))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f'Session journal is corrupt at line {line_number}') from error
        return records

    def _write(self, record) -> None:
        data = _json_bytes(record)
        view = memoryview(data)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError('Short write to paper-session journal')
            view = view[written:]
        os.fsync(self.fd)
        self.records.append(record)

    def append_operation(self, record) -> None:
        if self.transactions and self.transactions[-1][1] is None:
            raise RuntimeError('A prepared paper-session operation still needs recovery')
        self._write(record)
        self.transactions.append((record, None))

    def append_checkpoint(self, record) -> None:
        if not self.transactions or self.transactions[-1][1] is not None or self.transactions[-1][0]['id'] != record.get('id'):
            raise RuntimeError('Checkpoint does not follow its prepared operation')
        self._write(record)
        operation, _ = self.transactions[-1]
        self.transactions[-1] = (operation, record)

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
