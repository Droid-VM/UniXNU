#!/usr/bin/env python3
"""Build the trust bundle for the boot image: one owner module for the Adreno Metal plugin, then the two original Apple
trust modules byte for byte, then any further original module a board supplies (ORIGINAL_EXTRA, pinned: the App cryptex's).
Output: build/adreno-metal-plugin/owner-trust.bundle (ignored). The original modules are
verified unchanged against build/original-static-system-trust; only the plugin's CDHashes are added, and those a board's own
owner-built tools list in build/owner-trust-extra/*.json ({"cdhashes": [...], "category": N}), which are in the owner module too.
The category is the entry's launch constraint category (0, none, unless the json names one): AMFI kills a program launchd starts
as a system service unless its category allows that, as the one of the program it stands in for does."""
import hashlib
import json
from pathlib import Path
import struct
import uuid

ROOT = Path(__file__).resolve().parents[1]


def im4p_payload(data):
    """The payload of an IM4P (DER sequence: "IM4P", type, version, payload, ...)."""
    def item(at):
        tag, size, at = data[at], data[at + 1], at + 2
        if size & 0x80:
            count = size & 0x7f
            size, at = int.from_bytes(data[at:at + count], 'big'), at + count
        return tag, at, size
    tag, start, length = item(0)
    at, items = start, []
    while tag == 0x30 and at < start + length:
        _, body, size = item(at)
        items.append(data[body:body + size])
        at = body + size
    if len(items) < 4 or items[0] != b'IM4P':
        raise SystemExit('Not an IM4P')
    return items[3]


# Further original Apple trust modules a board adds byte for byte, each the unchanged IM4P of its IPSW in
# build/original-trust-extra/*.trustcache and accepted only with a digest pinned here: the App cryptex's, for a board that shows the
# cryptex's Safari daemons (DroidVM's owner-app-cryptex=1).
ORIGINAL_EXTRA = {'5d9bf23c900cb0b6ea241b1208a08aeef9cd863536452d85e68d1c0ef98e77c4': 'Cryptex1,AppTrustCache of 21H461'}
original = (ROOT / 'build/original-static-system-trust/original-static-system.bundle').read_bytes()
if hashlib.sha256(original).hexdigest() != '7416ca4f8377146626bb284e2133c76bee6cb4e3ee0af5d648b186235a7323a7':
    raise SystemExit('The original static/System trust bundle differs from the unchanged target input')
count = struct.unpack_from('<I', original)[0]
assert count == 2
offsets = struct.unpack_from('<2I', original, 4)
originals = [original[offsets[0]:offsets[1]], original[offsets[1]:]]
trust = json.loads((ROOT / 'build/adreno-metal-plugin/trust.json').read_text())
# The App cryptex (Cryptex1,AppOS) runs from owner Data, so its own trust module's hashes must be trusted too: the bundle
# carries exactly one owner module followed by both original Apple modules, and the cryptex entries join the owner module.
cryptex_trust = ROOT / 'build/app-cryptex/app-trustcache.bin'
cryptex_hashes = []
if cryptex_trust.is_file():
    module = cryptex_trust.read_bytes()
    entries = struct.unpack_from('<I', module, 20)[0]
    if struct.unpack_from('<I', module)[0] != 2 or len(module) != 24 + entries * 24:
        raise SystemExit('The App cryptex trust module is not a complete version-2 module')
    cryptex_hashes = [module[24 + 24 * entry:24 + 24 * entry + 20] for entry in range(entries)]
# DarwinBoot's ValidTrustCaches (and the kernel's binary search) need the hashes of a module in strictly ascending order.
hashes = [bytes.fromhex(trust['cdhash']), bytes.fromhex(trust['daemon_cdhash']), bytes.fromhex(trust['turnip_cdhash'])]
categories = {}
for extra in sorted((ROOT / 'build/owner-trust-extra').glob('*.json')):
    listed = json.loads(extra.read_text())
    for value in listed['cdhashes']:
        hashes.append(bytes.fromhex(value))
        categories[hashes[-1]] = listed.get('category', 0)
if len(set(hashes)) != len(hashes):
    raise SystemExit('Duplicate CDHash')
hashes = sorted(set(hashes) | set(cryptex_hashes))
cdhash = hashes[0]
identifier = uuid.uuid5(uuid.NAMESPACE_URL, 'org.blu-sharky.adreno-metal:' + ''.join(h.hex() for h in hashes))
own = struct.pack('<I16sI', 2, identifier.bytes, len(hashes)) + b''.join(h + bytes([2, 0, categories.get(h, 0), 0]) for h in hashes)
extras = []
for path in sorted((ROOT / 'build/original-trust-extra').glob('*.trustcache')):
    im4p = path.read_bytes()
    if hashlib.sha256(im4p).hexdigest() not in ORIGINAL_EXTRA:
        raise SystemExit(f'{path.name} is not one of the pinned original trust caches')
    module = im4p_payload(im4p)
    if struct.unpack_from('<I', module)[0] != 2 or len(module) != 24 + struct.unpack_from('<I', module, 20)[0] * 24:
        raise SystemExit(f'{path.name} carries no complete version-2 trust module')
    extras.append(module)
modules = [own, *originals, *extras]
cursor, starts = 4 + 4 * len(modules), []
for module in modules:
    starts.append(cursor)
    cursor += len(module)
bundle = struct.pack(f'<{1 + len(modules)}I', len(modules), *starts) + b''.join(modules)
def valid_like_the_loader(data):
    # The same structural checks as drivers/DarwinBoot/DarwinBootPkg/Driver/DarwinBoot.c ValidTrustCaches.
    count = struct.unpack_from('<I', data)[0]
    if not 0 < count <= 128:
        return False
    for index in range(count):
        start = struct.unpack_from('<I', data, 4 + 4 * index)[0]
        end = struct.unpack_from('<I', data, 8 + 4 * index)[0] if index + 1 < count else len(data)
        if start < 4 + 4 * count or start & 3 or end > len(data) or end - start < 24 or struct.unpack_from('<I', data, start)[0] != 2:
            return False
        entries = struct.unpack_from('<I', data, start + 20)[0]
        if end - start != 24 + entries * 24:
            return False
        for entry in range(entries):
            at = start + 24 + entry * 24
            if not 1 <= data[at + 20] <= 4 or (entry and data[at - 24:at - 4] >= data[at:at + 20]):
                return False
    return True


if not valid_like_the_loader(bundle):
    raise SystemExit('The trust bundle would be refused by the boot loader')
out = ROOT / 'build/adreno-metal-plugin/owner-trust.bundle'
out.write_bytes(bundle)
(ROOT / 'build/adreno-metal-plugin/owner-trust.json').write_text(json.dumps(dict(
    bundle_sha256=hashlib.sha256(bundle).hexdigest(), own_cdhashes=[h.hex() for h in hashes], original_modules_unchanged=True,
    original_extra_modules=[hashlib.sha256(m).hexdigest() for m in extras],
    apple_trust_chain_verified=False, purpose='owner trust for the Adreno Metal plugin and the board tools in build/owner-trust-extra'), indent=2) + '\n')
print(f'PASS: owner trust bundle (own module + both original modules + {len(extras)} further original)', hashlib.sha256(bundle).hexdigest())
