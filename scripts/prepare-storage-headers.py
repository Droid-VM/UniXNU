#!/usr/bin/env python3
"""Prepare pinned public storage headers; this does not qualify a binary family ABI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LEGACY_REVISION = 'e45fbe30a7879e0b16e26dddb78538f1b00e0bff'


def pinned_source(source, revision):
    """Require the explicit source commit and unmodified tracked inputs."""
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if len(revision) != 40 or actual != revision:
        raise SystemExit(f'{source}: expected full commit {revision}, found {actual}')
    dirty = subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=all'], text=True)
    if dirty:
        raise SystemExit(f'{source}: source checkout must be clean')
    return actual


def prepare(source, revision, out):
    pinned_source(source, revision)
    sdk = Path(subprocess.check_output(['xcrun', '--sdk', 'macosx', '--show-sdk-path'], text=True).strip())
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='headers-', dir=out) as temporary:
        include = Path(temporary) / 'include'
        headers = include / 'IOKit/storage'
        headers.mkdir(parents=True)
        copied = {}
        for path in sorted(source.glob('*.h')):
            target = headers / path.name
            shutil.copyfile(path, target)
            copied[path.name] = hashlib.sha256(target.read_bytes()).hexdigest()
        if not copied:
            raise SystemExit(f'{source}: no public IOStorageFamily headers')
        target = include / 'TargetConditionals.h'
        shutil.copyfile(sdk / 'usr/include/TargetConditionals.h', target)
        report = dict(source=str(source.resolve()), source_revision=revision, headers=copied,
                      sdk=str(sdk), target_conditionals_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                      qualification=False)
        # Only replace this builder's generated include tree after all inputs validate.
        if (out / 'include').exists():
            shutil.rmtree(out / 'include')
        shutil.move(str(include), out / 'include')
    (out / 'headers.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'refer/IOStorageFamily')
    parser.add_argument('--revision', default=os.environ.get('UNIXNU_IOSTORAGEFAMILY_COMMIT', LEGACY_REVISION))
    parser.add_argument('--output', type=Path, default=ROOT / 'build/storage-abi')
    args = parser.parse_args()
    report = prepare(args.source.resolve(), args.revision, args.output.resolve())
    print(f'PASS: {len(report["headers"])} public IOStorageFamily headers prepared at {report["source_revision"]}')


if __name__ == '__main__':
    main()
