#!/usr/bin/env python3
"""bundle-ipsw-trust.py OUT STATIC [MORE ...]: bundle unchanged IPSW trust caches for DarwinBoot.

Each input is an IM4P (or a raw module). Module 0 is the static trust cache; the kernel loads later ones
as engineering modules. Modules are copied byte for byte and must be exact version-2 modules."""
import hashlib, json, struct, sys
from pathlib import Path


def der(data, at):
    tag, size = data[at:at + 2]; at += 2
    if size & 128:
        n = size & 127; size = int.from_bytes(data[at:at + n], 'big'); at += n
    if at + size > len(data):
        raise SystemExit('DER exceeds input')
    return tag, data[at:at + size], at + size


def module(path):
    data = Path(path).read_bytes()
    if data[:1] == b'\x30':
        tag, seq, end = der(data, 0)
        fields, at = [], 0
        while at < len(seq):
            kind, value, at = der(seq, at); fields.append((kind, value))
        if fields[0][1] != b'IM4P' or fields[3][0] != 4:
            raise SystemExit(f'{path}: not an IM4P')
        data = fields[3][1]
    version, = struct.unpack_from('<I', data)
    count, = struct.unpack_from('<I', data, 20)
    if version != 2 or len(data) != 24 + count * 24:
        raise SystemExit(f'{path}: not an exact version-2 trust module (version {version}, {len(data)} bytes)')
    return data, count


out, inputs = sys.argv[1], sys.argv[2:]
modules = [module(p) for p in inputs]
header = 4 + 4 * len(modules)
offsets, cursor = [], header
for data, _ in modules:
    offsets.append(cursor); cursor += len(data)
bundle = struct.pack(f'<{1 + len(modules)}I', len(modules), *offsets) + b''.join(d for d, _ in modules)
Path(out).write_bytes(bundle)
Path(out + '.json').write_text(json.dumps({'inputs': [str(Path(p).resolve()) for p in inputs],
    'entries': [c for _, c in modules], 'modules_sha256': [hashlib.sha256(d).hexdigest() for d, _ in modules],
    'bundle_sha256': hashlib.sha256(bundle).hexdigest(), 'modified': False}, indent=2) + '\n')
print(out, len(bundle), 'bytes', [c for _, c in modules])
