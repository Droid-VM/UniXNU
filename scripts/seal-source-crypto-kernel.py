#!/usr/bin/env python3
"""Fill the source provider's normal POST HMAC field after final kernel link.

Implements the source fipspost_get_hmac/hmacfiletool build contract: HMAC-SHA256
with a one-byte zero key over the first __TEXT[ _EXEC ]/__text section.
No selftest or integrity check is disabled. Only locally compiled input is used.

static_if_init rewrites __text before the POST runs. It stores absolute instructions (b or nop), so the
sites of keys that boot flips are written here in their final state and the boot rewrite leaves the
hashed bytes unchanged. --kernel-args names the boot arguments the kernel starts with.
"""
import argparse
import hashlib
import hmac
import json
from pathlib import Path
import struct

from kernel_target import ARCH, SUBTYPE

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('kernel', type=Path)
parser.add_argument('--kernel-args', default='', help='boot arguments, to refuse ones that flip more static_if keys')
args = parser.parse_args()
# kern_feature_override_apply flips assert and sched debug keys only for these arguments.
for argument in args.kernel_args.split():
    if argument.lstrip('-').split('=', 1)[0] in ('validation_disables', 'serverperfmode', 'lcks'):
        raise SystemExit(f'{argument} flips static_if keys at boot that this seal does not model')
data = bytearray(args.kernel.read_bytes())
magic, cpu, subtype, kind = struct.unpack_from('<4I', data)
header = 0
if (magic, cpu, subtype & 0xff) == (0xfeedfacf, 0x100000c, SUBTYPE) and kind == 12:
    # A boot kernel collection: seal its com.apple.kernel entry. Section offsets are collection file offsets.
    # The emitter moves the native text, so the HMAC is computed here, after emission. The static_if sites
    # were prepatched when the native kernel was sealed before emission.
    offset = 32
    for _ in range(struct.unpack_from('<I', data, 16)[0]):
        cmd, length = struct.unpack_from('<II', data, offset)
        if cmd == 0x80000035:
            name = struct.unpack_from('<I', data, offset + 24)[0]
            if data[offset + name:data.index(b'\0', offset + name)] == b'com.apple.kernel':
                header = struct.unpack_from('<Q', data, offset + 16)[0]
        offset += length
    if not header:
        raise SystemExit('Kernel collection has no com.apple.kernel entry')
    magic, cpu, subtype, kind = struct.unpack_from('<4I', data, header)
if (magic, cpu, subtype & 0xff, kind) != (0xfeedfacf, 0x100000c, SUBTYPE, 2):
    raise SystemExit(f'Expected a source-built {ARCH} MH_EXECUTE kernel or a collection of one')
if b'_source_corecrypto14_bootstrap\0' not in data:
    raise SystemExit('Input does not contain the explicit source-provider bootstrap')
offset = header + 32
code = None
integrity = None
segments = []
sections = {}
symtab = None
chained = False
for _ in range(struct.unpack_from('<I', data, header + 16)[0]):
    cmd, length = struct.unpack_from('<II', data, offset)
    if cmd == 0x80000034:
        chained = True
    if cmd == 0x2:
        symtab = struct.unpack_from('<4I', data, offset + 8)
    if cmd == 0x19:
        name, va, _, fileoff, _ = struct.unpack_from('<16s4Q', data, offset + 8)
        name = name.rstrip(b'\0')
        segments.append((name, va, fileoff))
        for index in range(struct.unpack_from('<I', data, offset + 64)[0]):
            section = offset + 72 + index * 80
            section_name, segment_name, address, size, position = struct.unpack_from('<16s16sQQI', data, section)
            sections[(segment_name.rstrip(b'\0'), section_name.rstrip(b'\0'))] = (address, size, position)
            if section_name.rstrip(b'\0') == b'__text' and segment_name.rstrip(b'\0') in (b'__TEXT', b'__TEXT_EXEC') and code is None:
                code = (position, size, address)
            if section_name.rstrip(b'\0') == b'__fips_hmacs':
                if integrity is not None or size != 32:
                    raise SystemExit('Expected exactly one 32-byte POST HMAC field')
                integrity = position
    offset += length
if code is None or integrity is None:
    raise SystemExit('Missing real provider code or POST HMAC field')
base = next(va for name, va, _ in segments if name == b'__TEXT')
if code[2] - base != code[0] - header:
    raise SystemExit('Runtime header-relative text mapping differs from the POST file-offset contract')
if code[0] <= integrity < code[0] + code[1]:
    raise SystemExit('HMAC field must be outside the hashed instructions')


def symbols(names):
    found = {}
    if symtab:
        symoff, nsyms, stroff, _ = symtab
        for index in range(nsyms):
            strx, kind, _, _, value = struct.unpack_from('<IBBHQ', data, symoff + index * 16)
            name = bytes(data[stroff + strx:data.index(b'\0', stroff + strx)])
            if name in names and kind & 0x0e == 0x0e:
                found[name] = value
    return found


# osfmk/tests/kernel_tests.c static_if_tests_setup: unconditional, one disable and one enable.
flips = [b'_static_if_test_key_true_to_false_jump_key', b'_static_if_test_key_false_to_true_jump_key']
present = symbols({b'_static_if_tests_setup', *flips})
patched = 0
if b'_static_if_tests_setup' in present and not header:
    # Threaded rebases keep the key's low address bits; chained kernel-cache pointers keep a 30-bit offset.
    target_mask = 0x3fffffff if chained else 0x7ffffffffff
    keys = {(present[name] - (base if chained else 0)) & target_mask for name in flips if name in present}
    if len(keys) != len(flips):
        raise SystemExit('static_if test initializer present without its keys')
    entries = sections.get((b'__DATA_CONST', b'__static_if')) or sections.get((b'__BOOTDATA', b'__static_if'))
    if entries is None:
        raise SystemExit('static_if test initializer present without the entry section')
    address, size, position = entries
    for entry in range(0, size, 16):
        delta, target, link = struct.unpack_from('<iiQ', data, position + entry)
        # The link is a rebase of key + flags; bit 0 says the site was emitted as a branch.
        if (link & target_mask & ~3) not in keys:
            continue
        point = address + entry + delta
        site = code[0] + point - code[2]
        if not code[2] <= point < code[2] + code[1]:
            continue
        current = struct.unpack_from('<I', data, site)[0]
        branch = 0x14000000 | ((target >> 2) & 0x03ffffff)
        expected, final = (branch, 0xd503201f) if link & 1 else (0xd503201f, branch)
        if current == final:
            # Sealed before: the kernel build seals, and packaging seals again with the boot arguments.
            patched += 1
            continue
        if current != expected:
            raise SystemExit(f'static_if site {point:#x} holds {current:#010x}, not the emitted instruction')
        struct.pack_into('<I', data, site, final)
        patched += 1
mac = hmac.digest(b'\0', data[code[0]:code[0] + code[1]], 'sha256')
data[integrity:integrity + 32] = mac
args.kernel.write_bytes(data)
report = {'kernel_sha256': hashlib.sha256(data).hexdigest(), 'code_file_offset': hex(code[0]),
          'code_bytes': code[1], 'hmac_file_offset': hex(integrity), 'hmac_sha256': mac.hex(),
          'static_if_sites_prepatched': patched, 'runtime_selftests_executed': False}
args.kernel.with_suffix(args.kernel.suffix + '.crypto-hmac.json').write_text(json.dumps(report, indent=2) + '\n')
print('Source kernel POST integrity field populated; runtime selftests are still required')
