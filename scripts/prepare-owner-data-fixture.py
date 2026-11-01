#!/usr/bin/env python3
"""Create a small mutable offline APFS Data seed from the supplied OS's initial var tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=ROOT / 'build/owner-data-fixture')
parser.add_argument('--launchd-sha256', default='8cec9a3c44a443b08e977ccc5d577850316ad428c575dc01f09b018dbd5b6a25',
                    help='the supplied OS\'s /sbin/launchd (default: 21H461\'s); the caller pins it for another build')
parser.add_argument('--case-sensitive', action='store_true',
                    help='a case-sensitive volume, as iOS\'s Data is (default: the host\'s case-insensitive default)')
parser.add_argument('--expanded-records', action='store_true',
                    help='a volume with APFS expanded records (incompatible feature 0x200, newfs_apfs -x), as the Data of an iPadOS 27 '
                         'device has: without them APFS refuses to mark a compressed file purgeable, and SplashBoard drops every app '
                         'snapshot it cannot mark. An APFS that does not know the feature does not mount the volume')
args = parser.parse_args()
fixture = args.output.resolve()
image = fixture / 'owner-data-32.cdr'
if image.exists():
    raise SystemExit('Refuse to overwrite an existing Data seed')
source = ROOT / '.cache/firmware/target-volumes/os-mount'
if hashlib.sha256((source / 'sbin/launchd').read_bytes()).hexdigest() != args.launchd_sha256:
    raise SystemExit('Require the unchanged supplied original OS mount')
fixture.mkdir(parents=True, exist_ok=True)
raw = []
if args.case_sensitive or args.expanded_records:
    # hdiutil create -fs APFS has neither option: newfs_apfs formats a blank raw image, attached without mounting.
    with open(image, 'wb') as blank:
        blank.truncate(32 * 1024**2)
    raw = ['-imagekey', 'diskimage-class=CRawDiskImage']
    entities = plistlib.loads(subprocess.check_output(['hdiutil', 'attach', '-nomount', *raw, '-plist', str(image)]))
    blank = entities['system-entities'][0]['dev-entry']
    try:
        subprocess.run(['newfs_apfs', *(['-e'] if args.case_sensitive else []), *(['-x'] if args.expanded_records else []),
                        '-v', 'OwnerData', blank], check=True)
    finally:
        subprocess.run(['hdiutil', 'detach', blank], check=True)
else:
    subprocess.run(['hdiutil', 'create', '-size', '32m', '-layout', 'NONE', '-fs', 'APFS',
                    '-volname', 'OwnerData', '-type', 'UDTO', str(image)], check=True)
target = fixture / 'mount'
target.mkdir(exist_ok=True)
attached = plistlib.loads(subprocess.check_output(['hdiutil', 'attach', '-nobrowse', '-owners', 'off', *raw,
                                                  '-mountpoint', str(target), '-plist', str(image)]))
device = attached['system-entities'][0]['dev-entry']
volume_uuid = None
count = total = 0
source = source / 'private/var'
try:
    volumes = [e for e in attached['system-entities'] if e.get('mount-point') == str(target)]
    if len(volumes) != 1:
        raise SystemExit('Require exactly one newly formatted scratch volume')
    info = plistlib.loads(subprocess.check_output(['diskutil', 'info', '-plist', volumes[0]['dev-entry']]))
    volume_uuid = info['VolumeUUID']
    for current, dirs, files in os.walk(source, followlinks=False):
        dirs[:] = [name for name in dirs if name != 'staged_system_apps']
        dest = target / Path(current).relative_to(source)
        dest.mkdir(exist_ok=True)
        os.chmod(dest, stat.S_IMODE(Path(current).stat().st_mode))
        for name in list(dirs) + files:
            src, dst = Path(current) / name, dest / name
            if src.is_symlink():
                os.symlink(os.readlink(src), dst)
                if name in dirs:
                    dirs.remove(name)
            elif src.is_file():
                shutil.copy2(src, dst)
                count += 1
                total += src.stat().st_size
    for name in ('keybags', 'mobile/tmp', 'tmp', 'run'):
        (target / name).mkdir(parents=True, exist_ok=True)
    os.chmod(target / 'tmp', 0o1777)
finally:
    subprocess.run(['hdiutil', 'detach', device], check=True)
data = image.read_bytes()
if len(data) != 32 * 1024**2 or data[32:36] != b'NXSB':
    raise SystemExit('Host formatting did not produce the requested raw APFS container')
report = dict(volume_uuid=volume_uuid, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), source_regular_files=count,
              source_regular_bytes=total, case_sensitive=args.case_sensitive, expanded_records=args.expanded_records,
              guest_mutable=True, initial_seed_signature_verified=False,
              seed='Original private/var excluding staged_system_apps; symlinks preserved; runtime directories added',
              owner_keybag_created=False, os_inputs_modified=False, production_storage_qualified=False)
(fixture / 'seed.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
