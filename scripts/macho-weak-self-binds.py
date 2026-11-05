#!/usr/bin/env python3
"""Bind a Mach-O image's weak flat-namespace imports to the image itself: macho-weak-self-binds.py IMAGE

Mesa declares every Vulkan entrypoint a driver might implement as a weak symbol and takes the address of each, so the ones it
does not implement are NULL. ELF resolves those at link time; ld64 refuses an undefined symbol unless it is allowed
(-undefined dynamic_lookup), and then binds each through the flat namespace: dyld looks the name up in every image loaded in the
process before it settles on NULL. Turnip has 5633 of them, and an app's process has hundreds of images: dlopen spent 1-2 s of
CPU there in every process that opened a Metal device (Settings' launch: PencilKit -> Metal -> the plugin -> dyld
ChainedFixups::forEachBindTarget -> hasExportedSymbol, 2026-11-06). Bound to the image itself (ordinal 0), dyld looks in the one
export trie that cannot hold them (they are hidden) and leaves each NULL as before.

Rewrites the chained-fixups import table in place (any of its three formats); only weak imports with the flat-lookup ordinal
change, so running it twice changes nothing. The image's signature is void afterwards: sign it after this.
"""
import struct
import sys

LC_DYLD_CHAINED_FIXUPS = 0x80000034
FLAT_LOOKUP = -2


def main(path):
    data = bytearray(open(path, 'rb').read())
    if struct.unpack_from('<I', data, 0)[0] != 0xFEEDFACF:
        sys.exit(f'{path}: not a thin 64-bit Mach-O image')
    ncmds = struct.unpack_from('<I', data, 16)[0]
    at, fixups = 32, None
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', data, at)
        if cmd == LC_DYLD_CHAINED_FIXUPS:
            fixups = struct.unpack_from('<I', data, at + 8)[0]
        at += size
    if fixups is None:
        sys.exit(f'{path}: no LC_DYLD_CHAINED_FIXUPS')
    _, _, imports, _, count, form, _ = struct.unpack_from('<7I', data, fixups)
    base = fixups + imports
    changed = strong = 0
    for i in range(count):
        if form in (1, 2):   # DYLD_CHAINED_IMPORT(_ADDEND): lib_ordinal:8, weak_import:1, name_offset:23 (then an int32 addend)
            offset = base + i * (4 if form == 1 else 8)
            value = struct.unpack_from('<I', data, offset)[0]
            ordinal, weak = struct.unpack('<b', bytes([value & 0xFF]))[0], value >> 8 & 1
            if ordinal == FLAT_LOOKUP:
                if weak:
                    struct.pack_into('<I', data, offset, value & ~0xFF)
                    changed += 1
                else:
                    strong += 1
        elif form == 3:      # DYLD_CHAINED_IMPORT_ADDEND64: lib_ordinal:16, weak_import:1, reserved:15, name_offset:32, addend:64
            offset = base + i * 16
            value = struct.unpack_from('<Q', data, offset)[0]
            ordinal, weak = struct.unpack('<h', struct.pack('<H', value & 0xFFFF))[0], value >> 16 & 1
            if ordinal == FLAT_LOOKUP:
                if weak:
                    struct.pack_into('<Q', data, offset, value & ~0xFFFF)
                    changed += 1
                else:
                    strong += 1
        else:
            sys.exit(f'{path}: chained import format {form} unknown')
    open(path, 'wb').write(data)
    print(f'PASS: {changed} weak flat-namespace imports of {count} bound to the image itself'
          + (f'; {strong} non-weak flat imports left as they were' if strong else ''))


if __name__ == '__main__':
    main(sys.argv[1])
