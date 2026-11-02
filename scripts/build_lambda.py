"""Build a Linux/x86_64 Lambda asset from an allowlist, pinned wheels and verified Gitleaks.

No Docker, source distributions, install hooks, local config or credentials are included.
"""
import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.build/lambda'
LOCK = ROOT / 'infrastructure/runtime-requirements.lock'
GITLEAKS_VERSION = '8.30.1'
GITLEAKS_ARCHIVE_SHA256 = '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources():
    return sorted([*ROOT.glob('autosde/*.py'), *ROOT.glob('autosde/prompts/**/*.md')])


def extract_gitleaks(archive, destination):
    if sha256(archive) != GITLEAKS_ARCHIVE_SHA256:
        raise ValueError('Gitleaks archive checksum mismatch')
    with tarfile.open(archive, 'r:gz') as bundle:
        member = bundle.getmember('gitleaks')
        if not member.isfile() or member.size > 64 * 1024 * 1024:
            raise ValueError('Unexpected Gitleaks archive member')
        binary = bundle.extractfile(member).read()
    # ELF64 little-endian x86_64, rather than an accidentally bundled host executable.
    if binary[:5] != b'\x7fELF\x02' or binary[5] != 1 or struct.unpack('<H', binary[18:20])[0] != 62:
        raise ValueError('Gitleaks must be Linux x86_64 ELF')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(binary)
    destination.chmod(0o755)


def verify(output=OUTPUT):
    manifest = json.loads((output / 'asset-manifest.json').read_text())
    current = {str(path.relative_to(ROOT)): sha256(path) for path in sources()}
    if manifest['sources'] != current or manifest['requirements_sha256'] != sha256(LOCK):
        raise ValueError('Runtime sources changed: rebuild with scripts/build_lambda.py')
    actual = {str(path.relative_to(output)): sha256(path) for path in sorted(output.rglob('*'))
              if path.is_file() and path.name != 'asset-manifest.json'}
    if actual != manifest['files']:
        raise ValueError('Runtime asset contents do not match the manifest')
    if (output / 'autosde/prompts/agents.json').exists():
        raise ValueError('Local agent configuration must not be packaged')


def build(wheelhouse=None, archive=None):
    OUTPUT.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lambda-', dir=OUTPUT.parent) as tmp:
        target = Path(tmp) / 'asset'
        command = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                   '--require-hashes', '--only-binary=:all:', '--platform', 'manylinux2014_x86_64',
                   '--implementation', 'cp', '--python-version', '3.12', '--abi', 'cp312',
                   '--no-compile', '--target', str(target), '-r', str(LOCK)]
        if wheelhouse:
            command += ['--no-index', '--find-links', str(wheelhouse)]
        subprocess.run(command, check=True)
        for path in sources():
            destination = target / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        if archive is None:
            archive = Path(tmp) / 'gitleaks.tar.gz'
            url = f'https://github.com/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}/gitleaks_{GITLEAKS_VERSION}_linux_x64.tar.gz'
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError('Unexpected Gitleaks archive size')
            archive.write_bytes(data)
        extract_gitleaks(archive, target / 'bin/gitleaks')
        manifest = {
            'platform': 'linux-x86_64-python3.12', 'gitleaks_version': GITLEAKS_VERSION,
            'gitleaks_archive_sha256': GITLEAKS_ARCHIVE_SHA256, 'requirements_sha256': sha256(LOCK),
            'sources': {str(path.relative_to(ROOT)): sha256(path) for path in sources()},
            'files': {str(path.relative_to(target)): sha256(path) for path in sorted(target.rglob('*')) if path.is_file()},
        }
        (target / 'asset-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        if OUTPUT.exists():
            shutil.rmtree(OUTPUT)
        shutil.move(str(target), OUTPUT)
    verify()
    print('Verified Lambda asset: .build/lambda (Linux x86_64, no local config)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--gitleaks-archive', type=Path)
    args = parser.parse_args()
    if args.verify_only:
        verify()
        print('Runtime asset matches current sources and dependency lock.')
    else:
        build(args.wheelhouse, args.gitleaks_archive)
