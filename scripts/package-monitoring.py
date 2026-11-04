"""Build a deterministic, standalone monitoring-only ZIP without session reports."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATHS = (
    'docs/monitoring-runbook.md',
    'docs/monitoring.md',
    'live/__init__.py',
    'live/dashboard.css',
    'live/dashboard.html',
    'live/dashboard.js',
    'live/monitoring.py',
    'monitoring/alerts.yml',
    'monitoring/prometheus.yml',
    'scripts/run-monitoring.ps1',
)
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def build_bundle(output: Path, *, source_root=ROOT):
    """Package only the fixed monitoring allowlist; refuse to replace an output."""
    output, source_root = Path(output), Path(source_root).resolve()
    if output.suffix.lower() != '.zip':
        raise ValueError('Output must have a .zip extension')
    payload = {}
    for name in PAYLOAD_PATHS:
        path = source_root / name
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(source_root) or not path.is_file():
            raise ValueError(f'Missing or unsafe monitoring source: {name}')
        with path.open('rb') as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError(f'Monitoring source exceeds size limit: {name}')
        payload[name] = data
    manifest = dict(format_version=1, package='paper-monitoring', mode='paper',
                    scope='monitoring-only',
                    files={name: hashlib.sha256(data).hexdigest()
                           for name, data in sorted(payload.items())})
    payload['manifest.json'] = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(payload.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.compress_type = zipfile.ZIP_STORED
            archive.writestr(entry, data)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(archive_bytes.getvalue())
    return hashlib.sha256(archive_bytes.getvalue()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        digest = build_bundle(args.output)
    except (OSError, ValueError) as error:
        print(f'Packaging failed: {error}', file=sys.stderr)
        return 2
    print(f'Monitoring-only bundle: {args.output}')
    print(f'SHA-256: {digest}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
