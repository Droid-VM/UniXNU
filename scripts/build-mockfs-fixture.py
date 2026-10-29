#!/usr/bin/env python3
"""Compile XNU's minimal executable-root filesystem for offline tests."""
import json
from pathlib import Path
import subprocess

from kernel_target import check_object

ROOT = Path(__file__).resolve().parents[1]
recipe = ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/bsd/DEVELOPMENT/vfs_conf.o.json'
record = json.loads(recipe.read_text().strip().rstrip(','))
out = ROOT / 'build/mockfs-fixture'
out.mkdir(parents=True, exist_ok=True)
objects = []
for name in ('mockfs_fsnode', 'mockfs_vfsops', 'mockfs_vnops'):
    source = ROOT / '.cache/xnu-work/bsd/miscfs/mockfs' / (name + '.c')
    destination = out / (name + '.o')
    arguments = record['arguments']
    command = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == record['file']:
            command.append(str(source))
        elif value == '-o':
            command.extend(['-o', str(destination)])
            index += 1
        elif value in ('-MF', '-MJ'):
            index += 1
        elif value not in ('-MD', '-MP') and not value.startswith(('-DMOCKFS=', '-UMOCKFS')):
            command.append(value)
        index += 1
    command += ['-DMOCKFS=1']
    result = subprocess.run(command, cwd=record['directory'], capture_output=True, text=True)
    (out / (name + '.log')).write_text(result.stdout + result.stderr)
    if result.returncode:
        raise SystemExit(result.stderr[-4500:])
    check_object(destination.read_bytes(), destination.name)
    objects.append(destination)
archive = out / 'libmockfs_fixture.a'
archive.unlink(missing_ok=True)
subprocess.run(['xcrun', 'ar', 'rcs', archive, *objects], check=True)
print('3 XNU mockfs sources compiled (typed-allocation correction retained); ordinary ARM64 / iOS 17 audited')
print('Offline minimal-root fixture only; not an APFS driver or iPadOS filesystem')
