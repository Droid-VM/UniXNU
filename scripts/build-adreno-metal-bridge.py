#!/usr/bin/env python3
"""Build the native Metal bridge (drivers/AdrenoMetal/bridge/NativeMetalBridge.rs): the pinned metal2vulkan runtime behind a C ABI, as a static library.

--target ios (default) cross-builds for arm64-apple-ios17.0, --target host for the macOS host simulation. The runtime's own example library
(runtime_examples.ll) and its function-constant fixture are assembled to AIR with llvm-as and embedded for the self-tests. Only generated build files hold local paths; nothing here
is committed. Output: build/adreno-metal-bridge/<target>/libadreno_metal_bridge.a"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / 'scripts/data/gpu-metal-vulkan-inputs.json').read_text())['translator']['commit']
SOURCE = ROOT / '.cache/gpu-reference/metal2vulkan'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--target', choices=['ios', 'host'], default='ios')
parser.add_argument('--manifest-only', action='store_true',
                    help='write build/adreno-metal-bridge/Cargo.toml (patched source, fixtures) and stop before cargo, for `cargo fetch`')
args = parser.parse_args()
if subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
    raise SystemExit('Translator revision differs from the qualified source pin')
PATCHES = [ROOT / 'drivers/AdrenoMetal/bridge/patches' / name for name in [
    '0001-optional-constant-output-members.patch', '0002-resource-wait-storage-dependencies.patch',
    '0003-cache-device-entry-point-priming.patch', '0004-fold-constant-global-field-loads.patch',
    '0005-keep-module-decodes-and-profile-fingerprints.patch', '0006-skip-draws-without-a-pipeline.patch',
    '0007-accept-two-plane-rgb-surfaces.patch', '0008-alias-padded-rows-of-written-textures.patch',
    # The rest stack in order, each on the source the ones before it leave.
    '0009-keep-final-pass.patch', '0010-fewer-allocations-per-draw.patch', '0011-keep-draw-commands.patch',
    '0012-keep-draw-lists.patch', '0013-skip-unchanged-rebinds.patch', '0014-map-tracker-keys.patch',
    '0015-two-plane-shadow-as-memory-lies.patch', '0016-tile-stores-within-rows.patch']] + [ROOT / 'patches/metal2vulkan' / name for name in [
    # The board's own: a shader-cache directory a sandboxed process cannot write opens anyway when the environment asks
    # (METAL2VULKAN_SHADER_CACHE_OPTIONAL, which the Metal plugin sets for WebKit's GPU process); every lookup is then a miss.
    '0006-optional-shader-cache-directory.patch',
    # An `_sRGB` extended-range form's blend the translator's fast-math model does not hold takes its rounded model (Metal's own
    # without fast math) instead of refusing the pipeline: NanoUniverse's starfield, the Astronomy wallpaper, had no pipeline.
    '0007-srgb-extended-range-blend-rounded-model.patch',
    # `llvm.bitreverse` (what MSL's `reverse_bits` compiles to) lowers to the translator's own ReverseBits: GFXBench's Aztec
    # Ruins and Car Chase sample with a bit-reversed (Hammersley) sequence, and their pipelines were refused (UnknownIntrinsic).
    '0008-llvm-bitreverse.patch',
    # Pooled memory blocks start at 4 MiB and grow with their pool (up to 64 MiB), rather than 64 MiB each from the first
    # resource: on the iPadOS guest every block is wired guest memory, and its home screen's GPU clients held 2 GiB of them.
    '0009-growing-memory-blocks.patch',
    # A `[[patch(quad, 0)]]` function (no control points: GFXBench Car Chase's Bezier car body) answers its patch type and 0, as
    # Metal does, instead of being refused as unmeasured: the frontend took the refusal for no patch, so the pipeline lost the
    # descriptor's tessellation state (winding, partitioning, the factors' step function) and the car drew black and torn.
    '0010-patch-with-no-control-points.patch',
    # A rotated texture's turns between its pass's frame and its plane (CoreAnimation's framebuffer, rotation 3 in landscape)
    # on the Adreno 2D engine, through the board's Turnip (patches/mesa/turnip-rotated-copy.patch): one copy each way instead of
    # a staging copy, a compute kernel and a tight-buffer copy (a 1366x1024 pass: 8.8 ms against 0.5 unrotated). Diagnostics
    # read at process start: /private/var/ngpu/debug/rot-kernel (the kernel's turns), rot-swap (the quarter turns the other
    # way), rot-noload (the image turned in once only).
    '0011-rotated-turns-on-the-2d-engine.patch']]


def git_tree(*steps):
    # The source tree a scratch index holds after HEAD and the given commands, without touching the source's own index.
    index = ROOT / 'build/adreno-metal-bridge-index'
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    subprocess.run(['git', '-C', str(SOURCE), 'read-tree', 'HEAD'], env=env, check=True)
    for step in steps:
        subprocess.run(['git', '-C', str(SOURCE)] + step, env=env, check=True)
    tree = subprocess.check_output(['git', '-C', str(SOURCE), 'write-tree'], env=env, text=True).strip()
    index.unlink()
    return tree


# The tree after each prefix of the recorded patches; the source must be one of them.
states = [git_tree(*[['apply', '--cached', str(patch)] for patch in PATCHES[:count]]) for count in range(len(PATCHES) + 1)]
current = git_tree(['add', '-A'])
if current not in states:
    raise SystemExit('Translator source differs from the pinned revision plus its recorded patches')
for patch in PATCHES[states.index(current):]:
    subprocess.run(['git', '-C', str(SOURCE), 'apply', str(patch)], check=True)
if git_tree(['add', '-A']) != states[-1]:
    raise SystemExit('Translator patch result differs from its exact recorded source')
out = ROOT / 'build/adreno-metal-bridge'
out.mkdir(parents=True, exist_ok=True)
llvm_as = next((p for p in ['/opt/homebrew/opt/llvm/bin/llvm-as', shutil.which('llvm-as') or ''] if p and Path(p).exists()), None)
if not llvm_as:
    raise SystemExit('llvm-as (LLVM 21 or newer) is required to assemble the fixture')
air = out / 'runtime_examples.air'
subprocess.run([llvm_as, '-o', air, SOURCE / 'crates/metal2vulkan-validation/fixtures/public/examples/runtime_examples.ll'], check=True)
fc_air = out / 'function_constant.air'
subprocess.run([llvm_as, '-o', fc_air, SOURCE / 'crates/metal2vulkan-validation/fixtures/public/kernel_function_constant_gate_fold.ll'], check=True)
manifest = out / 'Cargo.toml'
manifest.write_text('\n'.join([
    '[package]', 'name = "adreno-metal-bridge"', 'version = "0.1.0"', 'edition = "2024"', 'rust-version = "1.98"', '[workspace]',
    '[lib]', 'crate-type = ["staticlib"]', 'path = ' + json.dumps(str(ROOT / 'drivers/NativeMetal/bridge/NativeMetalBridge.rs')),
    '[dependencies]',
    'metal2vulkan = { path = ' + json.dumps(str(SOURCE / 'crates/metal2vulkan')) + ', features = ["ash"] }',
    'metal2vulkan-runtime = { path = ' + json.dumps(str(SOURCE / 'crates/metal2vulkan-runtime')) + ' }',
    'libc = "0.2"',
    # The runtime hashes every shader-cache key and entry; without asm, sha2 0.10 on aarch64 is the portable software rounds, not the
    # ARMv8 SHA-256 instructions every target CPU has (cpufeatures answers sha2 true on Apple platforms).
    'sha2 = { version = "0.10", features = ["asm"] }',
    '[profile.release]', 'panic = "unwind"', 'debug = 1', '',
]))
if args.manifest_only:
    print(manifest)
    raise SystemExit(0)
triple = 'aarch64-apple-ios' if args.target == 'ios' else 'aarch64-apple-darwin'
# The translator source the runtime is built from (its pin and recorded patches), which keys the shader cache: shader priming keeps what it
# finished while this stays the same (PrimeShaders.h).
runtime_id = hashlib.sha256((PIN + '\n' + states[-1]).encode()).hexdigest()[:32]
env = dict(os.environ, NMB_RUNTIME_ID=runtime_id, NMB_FIXTURE_AIR=str(air), NMB_FC_AIR=str(fc_air), IPHONEOS_DEPLOYMENT_TARGET='17.0', MACOSX_DEPLOYMENT_TARGET='14.0')
target_dir = ROOT / 'build/m2v-target'
log = out / ('build-%s.log' % args.target)
with log.open('w') as stream:
    result = subprocess.run(['cargo', '+1.98.0', 'build', '--release', '--manifest-path', manifest, '--target', triple,
                             '--target-dir', target_dir], env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
if result.returncode:
    raise SystemExit(log.read_text()[-6000:])
library = target_dir / triple / 'release/libadreno_metal_bridge.a'
final = out / args.target / 'libadreno_metal_bridge.a'
final.parent.mkdir(exist_ok=True)
shutil.copyfile(library, final)
print('PASS: native Metal bridge static library for %s (%d bytes); translator %s' % (args.target, final.stat().st_size, PIN[:12]))
