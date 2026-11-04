"""Deterministic monitoring-only archives and portable extracted-module smoke checks."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('package_monitoring', ROOT / 'scripts/package-monitoring.py')
package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package)


class MonitoringPackageTests(unittest.TestCase):
    def test_identical_sources_build_identical_complete_allowlisted_archives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / 'one.zip', root / 'two.zip'
            digest = package.build_bundle(first)
            self.assertEqual(digest, package.build_bundle(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(hashlib.sha256(first.read_bytes()).hexdigest(), digest)
            with zipfile.ZipFile(first) as archive:
                expected = set(package.PAYLOAD_PATHS) | {'manifest.json'}
                self.assertEqual(set(archive.namelist()), expected)
                self.assertEqual(archive.namelist(), sorted(expected))
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['scope'], 'monitoring-only')
                self.assertEqual(set(manifest['files']), set(package.PAYLOAD_PATHS))
                for item in archive.infolist():
                    self.assertEqual(item.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(item.compress_type, zipfile.ZIP_STORED)
                    self.assertEqual((item.external_attr >> 16) & 0o777, 0o644)
                    self.assertFalse(item.filename.startswith('/'))
                    self.assertNotIn('..', Path(item.filename).parts)
                for name, digest in manifest['files'].items():
                    self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)
                self.assertFalse(any(name.startswith(('reports/', 'config/', 'tests/')) for name in archive.namelist()))
                self.assertFalse(any(name.endswith(('.dll', '.so', '.dylib', '.pyc')) for name in archive.namelist()))
                self.assertNotIn(str(ROOT), archive.read('manifest.json').decode())

    def test_source_mtimes_do_not_change_bytes_and_unlisted_files_are_excluded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            for name in package.PAYLOAD_PATHS:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / name).read_bytes())
            (source / 'reports').mkdir()
            (source / 'reports' / 'secret.json').write_text('DO NOT PACKAGE')
            (source / '.env').write_text('PRIVATE=DO_NOT_PACKAGE')
            first, second = root / 'one.zip', root / 'two.zip'
            package.build_bundle(first, source_root=source)
            for name in package.PAYLOAD_PATHS:
                os.utime(source / name, (1_000_000, 1_000_000))
            package.build_bundle(second, source_root=source)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertNotIn(b'DO_NOT_PACKAGE', first.read_bytes())

    def test_existing_output_is_preserved_and_missing_source_creates_no_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'existing.zip'
            output.write_bytes(b'original artifact')
            with self.assertRaises(FileExistsError):
                package.build_bundle(output)
            self.assertEqual(output.read_bytes(), b'original artifact')
            missing = root / 'missing.zip'
            with self.assertRaises(ValueError):
                package.build_bundle(missing, source_root=root / 'empty-source')
            self.assertFalse(missing.exists())
            with self.assertRaises(ValueError):
                package.build_bundle(root / 'wrong.txt')

    def test_extracted_bundle_imports_without_native_or_extra_python_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, extracted = root / 'bundle.zip', root / 'unpacked'
            package.build_bundle(output)
            with zipfile.ZipFile(output) as archive:
                archive.extractall(extracted)
            result = subprocess.run([sys.executable, '-I', '-c',
                "import sys;sys.path.insert(0,sys.argv[1]);"
                "from pathlib import Path;"
                "from live.monitoring import SessionExporter;"
                "print(SessionExporter(Path('missing-session')).snapshot().valid)",
                str(extracted)], cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'False')
            help_result = subprocess.run([sys.executable, '-m', 'live.monitoring', '--help'],
                cwd=extracted, capture_output=True, text=True, timeout=10)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn('--session', help_result.stdout)
            for name in ('dashboard.html', 'dashboard.css', 'dashboard.js'):
                self.assertGreater((extracted / 'live' / name).stat().st_size, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
