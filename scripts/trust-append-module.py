#!/usr/bin/env python3
"""trust-append-module.py IN OUT CDHASH [CDHASH ...]: copy a trust bundle (bundle-ipsw-trust.py layout) and append one version-2
module with the given CDHashes (hash type 2). The IPSW modules stay byte for byte; the kernel loads the new one as an engineering module."""
import struct
import sys
import uuid
from pathlib import Path

if len(sys.argv) < 4:
    sys.exit(__doc__)
data = Path(sys.argv[1]).read_bytes()
count = struct.unpack_from('<I', data)[0]
starts = list(struct.unpack_from(f'<{count}I', data, 4)) + [len(data)]
modules = [data[starts[i]:starts[i + 1]] for i in range(count)]
hashes = sorted({bytes.fromhex(h) for h in sys.argv[3:]})
assert all(len(h) == 20 for h in hashes), 'CDHashes are 20 bytes'
ident = uuid.uuid5(uuid.NAMESPACE_URL, 'org.unixnu.owner:' + ''.join(h.hex() for h in hashes)).bytes
modules.append(struct.pack('<I16sI', 2, ident, len(hashes)) + b''.join(h + bytes([2, 0, 0, 0]) for h in hashes))
cursor, offsets = 4 + 4 * len(modules), []
for m in modules:
    offsets.append(cursor)
    cursor += len(m)
Path(sys.argv[2]).write_bytes(struct.pack(f'<{len(modules) + 1}I', len(modules), *offsets) + b''.join(modules))
print(f'{sys.argv[2]}: {count} -> {len(modules)} modules, last has {len(hashes)} hashes')
