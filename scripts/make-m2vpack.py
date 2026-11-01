#!/usr/bin/env python3
"""Merge pipeline-cache exports (read-sheng-log.py run dirs: <run>/export/manifest.json + files/) into one SHEXP001 pack for
make-gpu-addon.py --pipeline-pack. Later directories win on equal paths. usage: make-m2vpack.py OUT EXPORT_DIR..."""
import json, struct, sys, zlib
from pathlib import Path
out, dirs = Path(sys.argv[1]), [Path(d) for d in sys.argv[2:]]
merged, root = {}, '/'
for d in dirs:
    m = json.loads((d / 'manifest.json').read_text())
    root = m['directory']
    for f in m['files']:
        merged[f['path']] = (f['mode'], f['uid'], f['gid'], (d / 'files' / f['path']).read_bytes())
stream = bytearray(b'M2VCACH1' + struct.pack('<I', len(root.encode())) + root.encode() + struct.pack('<I', len(merged)))
for path, (mode, uid, gid, data) in sorted(merged.items()):
    pb = path.encode()
    stream += struct.pack('<IIIIQ', len(pb), mode, uid, gid, len(data)) + pb + data
z = zlib.compress(bytes(stream), 6)
out.write_bytes(b'SHEXP001' + struct.pack('<II', len(stream), len(z)) + z)
print(f'{out}: {len(merged)} files, {len(stream)} bytes, {len(z)} compressed')
