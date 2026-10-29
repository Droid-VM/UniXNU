#!/usr/bin/env python3
"""Route the indirect branches of the linked Rust code of an arm64e image through the stripping trampolines.

  rust-pac-rewrite.py IMAGE MAP ARCHIVE_NAME

rustc's arm64e code calls function pointers with a plain blr/br, which faults on the signed pointers the rest of an arm64e process hands
out (drivers/AdrenoMetal/bridge/RustPacBoundary.c). Every blr Xn / br Xn inside a function the link map attributes to an object of
ARCHIVE_NAME becomes bl / b to nmb_pac_call_xN (xpaci; br), in place, before the image is signed. blr/br x30 is left alone and counted."""
import re
import struct
import sys
from pathlib import Path

if len(sys.argv) != 4:
    sys.exit(__doc__)
image, link_map, archive = Path(sys.argv[1]), Path(sys.argv[2]).read_text(errors='replace'), sys.argv[3]
data = bytearray(image.read_bytes())

files, text, symbols, part = set(), None, [], None
for line in link_map.splitlines():
    if line.startswith('# Object files:'): part = 'files'; continue
    if line.startswith('# Sections:'): part = 'sections'; continue
    if line.startswith('# Symbols:'): part = 'symbols'; continue
    if line.startswith('# Dead Stripped'): part = None; continue
    if line.startswith('#') or not line.strip(): continue
    if part == 'files':
        m = re.match(r'\[\s*(\d+)\]\s+(.*)$', line)
        if m and archive in m.group(2): files.add(int(m.group(1)))
    elif part == 'sections':
        f = line.split()
        if len(f) >= 4 and f[2] == '__TEXT' and f[3] == '__text': text = (int(f[0], 16), int(f[1], 16))
    elif part == 'symbols':
        m = re.match(r'(0x[0-9A-Fa-f]+)\s+(0x[0-9A-Fa-f]+)\s+\[\s*(\d+)\]\s+(.*)$', line)
        if m: symbols.append((int(m.group(1), 16), int(m.group(2), 16), int(m.group(3)), m.group(4)))
if not files or not text:
    sys.exit('the map names no object of %s or no __TEXT,__text' % archive)
tramp = {}
for address, size, index, name in symbols:
    m = re.fullmatch(r'_nmb_pac_call_x(\d+)', name)
    if m: tramp[int(m.group(1))] = address
if sorted(tramp) != list(range(30)):
    sys.exit('trampolines missing: %s' % sorted(set(range(30)) - set(tramp)))

# __TEXT segment: virtual address to file offset.
ncmds = struct.unpack_from('<I', data, 16)[0]
at, segment = 32, None
for _ in range(ncmds):
    cmd, size = struct.unpack_from('<II', data, at)
    if cmd == 0x19 and data[at + 8:at + 24].rstrip(b'\0') == b'__TEXT':
        vmaddr, vmsize, fileoff = struct.unpack_from('<QQQ', data, at + 24)
        segment = (vmaddr, vmsize, fileoff)
    at += size
vmaddr, vmsize, fileoff = segment
lo, hi = text[0], text[0] + text[1]
counts = {'blr': 0, 'br': 0, 'x30': 0}
for address, size, index, name in symbols:
    if index not in files or not lo <= address < hi:
        continue
    for pc in range(address, min(address + size, hi), 4):
        off = pc - vmaddr + fileoff
        word = struct.unpack_from('<I', data, off)[0]
        kind = {0xD63F0000: 'blr', 0xD61F0000: 'br'}.get(word & 0xFFFFFC1F)
        if not kind:
            continue
        reg = (word >> 5) & 31
        if reg == 30:
            counts['x30'] += 1
            continue
        delta = (tramp[reg] - pc) >> 2
        if not -(1 << 25) <= delta < (1 << 25):
            sys.exit('trampoline out of branch range at %#x' % pc)
        struct.pack_into('<I', data, off, (0x94000000 if kind == 'blr' else 0x14000000) | (delta & 0x3FFFFFF))
        counts[kind] += 1
image.write_bytes(data)
print('PASS: Rust indirect branches routed through the PAC trampolines: %d blr, %d br, %d x30 left (%d objects)'
      % (counts['blr'], counts['br'], counts['x30'], len(files)))
