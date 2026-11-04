"""Real cross-process ownership and crash-release checks for session journals."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from live.recovery import SessionLock

HOLDER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from live.recovery import SessionLock
lock = SessionLock(Path(sys.argv[2]))
lock.acquire()
print('locked', flush=True)
sys.stdin.read(1)
lock.release()
"""

PROBE = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from live.recovery import SessionLock
lock = SessionLock(Path(sys.argv[2]))
try:
    lock.acquire()
except RuntimeError:
    print('blocked')
else:
    lock.release()
    print('acquired')
"""


class SessionLockTests(unittest.TestCase):
    def test_second_process_is_blocked_and_crash_releases_the_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'session.lock'
            holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(ROOT), str(path)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'locked')
                result = subprocess.run([sys.executable, '-c', PROBE, str(ROOT), str(path)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'blocked')
                holder.kill()
                holder.wait(timeout=10)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with SessionLock(path):
                            pass
                        break
                    except RuntimeError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.02)
                self.assertTrue(path.exists())  # File presence is not ownership.
            finally:
                if holder.poll() is None:
                    holder.kill()
                    holder.wait(timeout=10)
                for stream in (holder.stdin, holder.stdout, holder.stderr):
                    stream.close()

    def test_stale_looking_diagnostic_text_cannot_steal_an_active_os_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'session.lock'
            with SessionLock(path) as lock:
                os.lseek(lock.descriptor, 0, os.SEEK_SET)
                os.ftruncate(lock.descriptor, 0)
                os.write(lock.descriptor, b'2147483647 stale-looking diagnostic\n')
                os.fsync(lock.descriptor)
                result = subprocess.run([sys.executable, '-c', PROBE, str(ROOT), str(path)],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'blocked')

    def test_legacy_live_owner_is_preserved_and_dead_owner_can_migrate(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'session.lock'
            path.write_text(f'{os.getpid()} legacy-token\n', encoding='ascii')
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                SessionLock(path).acquire()
            with patch('live.recovery._pid_is_alive', return_value=False):
                with SessionLock(path):
                    pass
            self.assertTrue(path.read_bytes().startswith(b'v2 '))
            with SessionLock(path):
                pass
            path.write_text('unreadable legacy token', encoding='ascii')
            with self.assertRaisesRegex(RuntimeError, 'unreadable'):
                SessionLock(path).acquire()


if __name__ == '__main__':
    unittest.main(verbosity=2)
