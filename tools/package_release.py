#!/usr/bin/env python3
"""Build the portable Linux ZIP without shipping or replacing runtime settings."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import zipfile


def release_files(root):
    names = {'.gitattributes', '.gitignore', 'README.md', '시작안내.txt', 'ui.html',
             'weather_regions.json', 'recording.example.ini', 'recording.example.json',
             'recording.weather.example.json', 'recording.portable.example.json', 'bin/ffmpeg'}
    for pattern in ('*.py', '*.sh', 'components/*.py', 'tools/*.py', 'tools/*.sh',
                    'manual/*.html', 'manual/*.pdf', 'third_party/ffmpeg/*'):
        names.update(path.relative_to(root).as_posix() for path in root.glob(pattern) if path.is_file())
    for report in ('validation/validation_summary.json', 'validation/ip_filename_patch.json'):
        if (root / report).is_file():
            names.add(report)
    paths = []
    for name in sorted(names):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Release file must be a regular file: ' + name)
        paths.append((name, path))
    return paths


def package(root, output, version):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}-v\d+(?:\.\d+)*', version):
        raise ValueError('Version must look like 2026-11-05-v2.2')
    template = json.loads((root / 'recording.portable.example.json').read_text(encoding='utf-8'))
    if any(urlsplit(camera.get('url', '')).password for camera in template.get('cameras', [])):
        raise ValueError('Never include a camera password in the release template')
    files = release_files(root)
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files}
    manifest = {'version': version, 'target': 'Linux x86_64, glibc >= 2.35',
                'runtime_settings_included': False, 'camera_password_included': False,
                'test_video_included': False, 'default_launch': 'browser',
                'configuration': 'Create recording.json from recording.portable.example.json only when missing; preserve existing settings.',
                'validation': 'validation/validation_summary.json' if 'validation/validation_summary.json' in hashes else None,
                'sha256': hashes}
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    year, month, day = map(int, version[:10].split('-'))
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        entries = [(name, path.read_bytes()) for name, path in files]
        entries.append(('PACKAGE_MANIFEST.json', manifest_bytes))
        for name, content in entries:
            entry = zipfile.ZipInfo('RTSP_recorder/' + name, (year, month, day, 0, 0, 0))
            entry.create_system = 3
            executable = name.endswith('.sh') or name in ('record.py', 'bin/ffmpeg')
            entry.external_attr = ((0o100755 if executable else 0o100644) << 16)
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert 'RTSP_recorder/recording.json' not in archive.namelist()
        assert 'RTSP_recorder/recording.ini' not in archive.namelist()
        for name, digest in hashes.items():
            assert hashlib.sha256(archive.read('RTSP_recorder/' + name)).hexdigest() == digest
    (root / 'PACKAGE_MANIFEST.json').write_bytes(manifest_bytes)
    return {'path': str(output), 'version': version, 'files': len(files) + 1,
            'bytes': output.stat().st_size, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'runtime_settings_included': False, 'verified': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='2026-11-05-v2.2')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    filename = 'RTSP_recorder_' + args.version[:10].replace('-', '') + '_' + args.version[11:] + '_linux_x86_64.zip'
    print(json.dumps(package(root, args.output or root / 'dist' / filename, args.version), ensure_ascii=False))


if __name__ == '__main__':
    main()
