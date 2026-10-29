#!/usr/bin/env python3
"""Compile the render server's shader priming ahead, on this Mac, into a pipeline pack for the image.

The plugin's own priming (drivers/AdrenoMetal/PrimeShaders.h, through bridge/prime_host.mm) runs over every library of the prime list,
read from the OS image's mounts, through the frontend and the runtime over the tablet's driver: the host build of Turnip for the Adreno
740, its kernel answered by the host copy of the kernel stack (drivers/Adreno740/tests/hostsim). Turnip compiles without running anything
on the GPU. Its pipeline cache identity is the compiler source's (build-turnip-darwin.py -Dtu-build-id), the same in the tablet's build, and
the runtime's is its translator source's, so the tablet's render server finds these entries as its own. The cache directory is packed as a
SHEXP001 pack (drivers/AdrenoMetal/CacheExport.h), which the plugin imports into the render server's cache directory before the device
opens (make-gpu-addon.py --pipeline-pack), with prime-done naming every library finished under the tablet's id for it.

Every library is primed, and the entries of the libraries --pack names (a regular expression over the tablet paths) go into the pack.
The whole set (--pack .) is about 1.2 GB, 350 MB packed: it goes into the userdata import region (UniXNU-sheng
scripts/make-pipeline-import.py), which the plugin reads at its first boot. The default, the render server's own libraries, is under
1 MiB and fits the boot image's addon (make-gpu-addon.py --pipeline-pack). prime-done names every library either way, so the render
server spends no idle time on them; a pipeline of an unpacked library is compiled when a process first asks for it, as before. A rerun
with the same inputs reuses the libraries already primed.

usage: make-pipeline-cache.py OS_MOUNT CRYPTEX_MOUNT OUTPUT.m2vpack [--pack REGEX] [--jobs N]
Run from the resolved build tree after the host bridge, both Turnip builds and the host peer (check-turnip-darwin-host.py) are built."""
import argparse
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / 'drivers/AdrenoMetal'
OUT = ROOT / 'build/pipeline-cache'
parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('os_mount', type=Path)
parser.add_argument('cryptex_mount', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) - 2))
# The render server draws with Core Animation's own library (Liquid Glass's materials among it), Core Image's, CoreUI's and RenderBox's.
parser.add_argument('--pack', default=r'/(QuartzCore|CoreImage|CoreUI|RenderBox)\.framework/')
args = parser.parse_args()
host_turnip = ROOT / 'build/mesa-tu/src/freedreno/vulkan/libvulkan_freedreno.dylib'
tablet_turnip = ROOT / 'build/mesa-tu-ios27/src/freedreno/vulkan/libvulkan_freedreno.dylib'
peer = ROOT / 'build/turnip-darwin-host/libhostiokit.dylib'
bridge = ROOT / 'build/adreno-metal-bridge/host/libadreno_metal_bridge.a'
prime_list = ROOT / 'build/prime-list.txt'
for required in [host_turnip, tablet_turnip, peer, bridge, prime_list, args.os_mount / 'System/Library', args.cryptex_mount / 'System/Library']:
    if not required.exists():
        sys.exit('missing input: %s' % required)
OUT.mkdir(parents=True, exist_ok=True)
producer = OUT / 'prime_host'
subprocess.run(['xcrun', 'clang++', '-std=c++17', '-fobjc-arc', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                '-Wno-unused-function', '-Wno-deprecated-declarations', '-I', KIT, '-I', ROOT / 'drivers/Adreno740', '-I', ROOT / 'kits/AdrenoPM4',
                '-I', ROOT / '.cache/mesa-work/include', '-I', ROOT / 'build/turnip-smoke',
                KIT / 'NativeBridgeMetalFrontend.mm',
                KIT / 'bridge/prime_host.mm', bridge, '-framework', 'Foundation', '-framework', 'Metal', '-framework', 'IOSurface',
                '-framework', 'CoreFoundation', '-o', producer], check=True)
libraries = [l for l in prime_list.read_text().splitlines() if l.startswith('/')]
env = {k: v for k, v in os.environ.items() if not k.startswith(('IR3_', 'FD_MESA_', 'NIR_', 'TU_', 'HOSTSIM_', 'MESA_', 'NMB_', 'METAL2VULKAN_'))}
# TU_DEBUG as the tablet's plugin sets it for every Metal process (NativeAdrenoMetalPlugin.mm: no concurrent binning), so Turnip compiles
# what the tablet's would.
env.update(DYLD_INSERT_LIBRARIES=str(peer), MESA_SHADER_CACHE_DISABLE='true', TU_DEBUG='nocb')
work = OUT / 'work'
# What every entry depends on: the producer, both Turnip builds, the host peer and the list. Another set of inputs primes from scratch.
stamp = '\n'.join([hashlib.sha256(p.read_bytes()).hexdigest() for p in [producer, host_turnip, tablet_turnip, peer, prime_list]] + [env['TU_DEBUG']])
if not (work / 'inputs').is_file() or (work / 'inputs').read_text() != stamp:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    (work / 'inputs').write_text(stamp)



def prime(index):
    # One library a process, in its own directory: a function that ends the process costs that library alone, which is primed again
    # with the function listed as bad, as the render server's own priming does (prime-current to prime-bad).
    directory = work / ('%03d' % index)
    for log in sorted(directory.glob('run*.log')):
        if 'PASS: primed' in log.read_text():
            return index, 0, [l for l in log.read_text().splitlines() if l.startswith('PASS')][0]
    for attempt in range(8):
        directory.mkdir(exist_ok=True)
        run = subprocess.run([str(producer), str(host_turnip), str(directory), str(prime_list), str(args.os_mount),
                              str(args.cryptex_mount), str(tablet_turnip), str(index), '1'], env=env, capture_output=True, text=True,
                             timeout=3600)
        (directory / ('run%d.log' % attempt)).write_text(run.stdout + run.stderr)
        if run.returncode == 0 and 'PASS: primed' in run.stdout:
            return index, attempt, [l for l in run.stdout.splitlines() if l.startswith('PASS')][0]
        current = directory / 'prime-current'
        if not current.is_file():
            return index, attempt, 'FAIL exit %d with no function being compiled' % run.returncode
        with (directory / 'prime-bad').open('a') as bad:
            bad.write(current.read_text())
        current.unlink()
    return index, 8, 'FAIL after 8 attempts'


with ThreadPoolExecutor(args.jobs) as pool:
    results = sorted(pool.map(prime, range(len(libraries))))
failed = [r for r in results if r[2].startswith('FAIL')]
for index, attempts, line in results:
    if attempts or line.startswith('FAIL'):
        print('%s: %s (%d retries)' % (libraries[index], line, attempts))
if failed:
    sys.exit('%d libraries failed; see %s' % (len(failed), work))
# One directory: the entries of every run (an entry's name is its content's key, so two runs' entries of one name are the same entry),
# and the runs' prime-done, prime-bad and prime.log in list order.
merged = OUT / 'cache'
shutil.rmtree(merged, ignore_errors=True)
merged.mkdir()
lists = {'prime-done': [], 'prime-bad': [], 'prime.log': []}
for index in range(len(libraries)):
    directory = work / ('%03d' % index)
    for path in sorted(p for p in directory.rglob('*') if p.is_file()):
        relative = path.relative_to(directory).as_posix()
        if relative in lists:
            lists[relative].append(path.read_text())
        elif relative.startswith('v1/') and re.search(args.pack, libraries[index]):
            target = merged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(path, target)
for name, parts in lists.items():
    if parts:
        (merged / name).write_text(''.join(parts))
# The pack: "M2VCACH1", the directory, the file count, then per file its path, mode, owner, size and bytes (CacheExport.h), compressed
# behind "SHEXP001". The render server is mobile's (501), and imports into its own directory with its own owner.
# The entries and prime-done: prime-bad names the host's paths, and prime.log is this run's.
# prime-done last: it is the import's last file, so a library is never named primed before its entries are in place.
files = sorted((p for p in merged.rglob('*') if p.is_file() and (p.name == 'prime-done' or p.relative_to(merged).parts[0] == 'v1')),
               key=lambda p: (p.name == 'prime-done', p.as_posix()))
root = b'/var/mobile/Library/Caches/com.apple.backboardd/m2v'
# Streamed: the whole set is over a GiB.
compressor = zlib.compressobj(9)
raw_bytes, chunks = 0, []
def put(data):
    global raw_bytes
    raw_bytes += len(data)
    chunks.append(compressor.compress(data))
put(b'M2VCACH1' + struct.pack('<I', len(root)) + root + struct.pack('<I', len(files)))
for path in files:
    name = path.relative_to(merged).as_posix().encode()
    data = path.read_bytes()
    put(struct.pack('<IIIIQ', len(name), 0o600, 501, 501, len(data)) + name + data)
chunks.append(compressor.flush())
packed_bytes = sum(len(c) for c in chunks)
if raw_bytes >= 1 << 32 or packed_bytes >= 1 << 32:
    sys.exit('the pack passes the format\'s 4 GiB bounds')
with args.output.open('wb') as out:
    out.write(b'SHEXP001' + struct.pack('<II', raw_bytes, packed_bytes))
    for c in chunks:
        out.write(c)
done = len(lists['prime-done'] and ''.join(lists['prime-done']).splitlines())
packed_libraries = sum(1 for l in libraries if re.search(args.pack, l))
print('PASS: %d libraries primed ahead, entries of %d packed: %d files, %d bytes, %d packed -> %s' % (
    done, packed_libraries, len(files), raw_bytes, packed_bytes + 16, args.output))
