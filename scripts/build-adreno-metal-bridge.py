#!/usr/bin/env python3
"""Build the native Metal bridge (drivers/AdrenoMetal/bridge/NativeMetalBridge.rs): the pinned metal2vulkan runtime behind a C ABI, as a static library.

--target ios (default) cross-builds for arm64-apple-ios17.0, --target ios27 for arm64e-apple-ios27.0 (nightly, std built from source), --target host for the macOS host simulation. The runtime's own example library
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
parser.add_argument('--target', choices=['ios', 'ios27', 'host'], default='ios')
parser.add_argument('--manifest-only', action='store_true',
                    help='write build/adreno-metal-bridge/Cargo.toml (patched source, fixtures) and stop before cargo, for `cargo fetch`')
args = parser.parse_args()
if subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
    raise SystemExit('Translator revision differs from the qualified source pin')
PATCHES = [ROOT / 'drivers/AdrenoMetal/bridge/patches' / name for name in [
    # Each stacks on the source the ones before it leave.
    '0001-optional-constant-output-members.patch', '0002-resource-wait-storage-dependencies.patch',
    '0003-cache-device-entry-point-priming.patch', '0004-fold-constant-global-field-loads.patch',
    # A draw whose pipeline the driver refused binds nothing (int-170: tu_CmdBindPipeline in the render server).
    '0005-skip-draws-without-a-pipeline.patch',
    # A batch's barrier globals on the stack, and the neutral resources' maps by the runtime's key hash.
    '0006-batch-barriers-and-key-hash-neutral-maps.patch',
    # From the DroidVM board's translator patches: a cache directory a sandboxed process may not write, llvm.bitreverse, a patch
    # function with no control points, and Core Image kernel libraries (file_type 1) loaded as Metal loads them.
    '0007-optional-shader-cache-directory.patch', '0008-llvm-bitreverse.patch', '0009-patch-with-no-control-points.patch',
    '0010-core-image-kernel-libraries.patch',
    # A two-plane surface's shadow starts as its memory lies; the Astronomy wallpaper's extended-range sRGB blends take the rounded
    # model; pooled blocks start at 4 MiB and grow (the render server alone held 1 GiB of wired memory).
    '0011-two-plane-shadow-as-memory-lies.patch', '0012-srgb-extended-range-blend-rounded-model.patch',
    '0013-growing-memory-blocks.patch',
    # HostPageTexturePolicy::InPlaceWrites: textures over surfaces and padded buffers are images over their bytes, rendered and written
    # too, where their tile stores stay within their rows, instead of shadows copied in and out every pass.
    '0014-in-place-written-textures.patch', '0015-in-place-written-host-page-textures.patch',
    # Device::refuse_rebinds: no call logs or per-command holds of no-copy buffers, which only a rebind or a detach reads.
    '0016-refuse-rebinds.patch',
    # A heap texture's own-device answer rounded up to a multiple of its alignment, as Metal's answers are: a heap sized as the sum of
    # its textures' answers then holds them (iconservicesagent's Core Image heaps ran out: OutOfMemory(Heap)).
    '0017-heap-texture-size-a-multiple-of-its-alignment.patch',
    # The runtime's live device memory in bytes by kind (pooled, placed, empty kept, dedicated, imported), for the frontend's MEM row.
    '0018-memory-census-bytes.patch',
    # The call stacks that allocated device memory of 4 MiB or more, by bytes, for the frontend's MA rows.
    '0019-allocation-sites.patch',
    # Not 0020-written-host-page-textures-in-place-at-any-size.patch: it relies on upstream's Turnip render-mode patch
    # (turnip-render-mode-for-attachment-memory.patch), which does not apply on this fork's Mesa (Droid-VM/mesa), nor does
    # turnip-gmem-mutable-swap.patch; both stay out of adreno-compiler-inputs.json until they are ported.
    # A dedicated block of images whose image went stays spare for the next one of its pool (Core Animation's memoryless attachments,
    # backed at their first pass, were each a new 40-112 MiB allocation), freed after 10 s, when a block cannot be made, or under memory
    # pressure; room in the pools is kept by a thread of the device's, not in the creation call that asked for it.
    '0021-spare-image-blocks-and-room-keeper.patch',
    # stage_trace.rs: each command buffer's stages from its commit to its completion (commit, lowering, recording, the scheduler's
    # holds, each submission, the completion waiter) and the scheduler's sleeps and wakes, read by the frontend (nmb_stage_ring_read).
    '0022-command-buffer-stage-trace.patch',
    # The threads that bring command buffers to their handlers run at the user-interactive class and the compile threads at least at
    # the user-initiated class on iOS too, not on macOS alone: on the iPad they ran at the default class and woke up to 50 ms late.
    '0023-quality-of-service-on-ios.patch',
    # The host's diagnostics switch (SHENG_DIAG, from the boot argument sheng-diag=1): without it the stage ring and the allocation sites
    # keep nothing. The stage ring also marks the steps inside a batch's submission (SubmitStep: placed, gathered, vkQueueSubmit, changes).
    '0024-diagnostics-switch-and-submit-steps.patch'
    ]] + [ROOT / 'patches/metal2vulkan' / name for name in [
    # The board's own, on top. A rotated texture's turns between its pass's frame and its plane (CoreAnimation's framebuffer, rotation 3
    # in landscape) on the Adreno 2D engine, through the board's Turnip (patches/mesa/turnip-rotated-copy.patch): one copy each way instead
    # of a staging copy, a compute kernel and a tight-buffer copy (a 1366x1024 pass: 8.8 ms against 0.5 unrotated). Diagnostics read at
    # process start: /private/var/ngpu/debug/rot-kernel (the kernel's turns), rot-swap (the quarter turns the other way), rot-noload (the
    # image turned in once only).
    '0011-rotated-turns-on-the-2d-engine.patch',
    # The queues' global priority from the environment (METAL2VULKAN_QUEUE_PRIORITY), which the Metal plugin sets to high in the
    # render server: on an iPad CoreAnimation asks for a high GPU priority on its queue (setGPUPriority:), but a Vulkan queue's
    # priority is fixed when the device opens, before that call. The board's Turnip moves the process's ring to it
    # (patches/mesa/turnip-darwin-queue-priority.patch), so the compositor's frames no longer wait behind an app's.
    '0012-queue-global-priority.patch',
    # Device::trim_kept_memory: the kept empty pooled blocks that stayed empty a while, each pool's last one included, freed when the
    # Metal plugin asks (its process idle two seconds, or memory pressure): on this board every block is wired guest memory, and a
    # pool keeps its last empty block however long its process stays idle (136 MiB in the render server at the home screen).
    '0013-trim-kept-memory.patch',
    # A quarter-turned texture over a surface plane held to the plane's row, not its own: the iPad's display framebuffer is a
    # portrait surface behind the render server's landscape texture (see the fork copy for the reason).
    '0014-quarter-turn-plane-row.patch',
    # A [[color(n)]] read of a color the render pipeline has no attachment for reads zero instead of refusing the pipeline
    # (LoweringInputs::unattached_reads): iPadOS 27's QuartzCore makes path_blit_frag_lph (its coverage mask at color 2) and
    # fixed_frag_lph_cph for passes without that attachment, and the render server aborted in create_pipeline_state.
    '0015-unattached-color-reads.patch',
    # A linked function made under MTLFunctionDescriptor.specializedName links under that name (LinkedFunction::specialized_name;
    # the AIR linker finds the definition by the module's own name): RenderBox makes IconRendering's clampedPlusL_v1 as custom_blend.
    '0016-specialized-name-links.patch',
    # The spare dedicated image blocks (bridge patch 0021) held to a sixteenth of the GPU address space (sparseAddressSpaceSize):
    # each of the board's GPU contexts has a slice of the host GPU's, and with spares holding part of it the render server's Turnip
    # found no address range for its next BO and lost the device.
    '0017-spares-within-the-address-space.patch',
    # A linked function specialized with function constants links with them bound and its module's static initializers evaluated,
    # as Metal resolves a specialized function's constants before linking (the linker refused any module declaring a constant or a
    # static initializer): RenderBox links displacementMap_v1 specialized with RB::shaderVariant as custom_fn, the icon effect of
    # Settings' rows (iconservicesagent's refused custom_effect pipelines). Also gives 0016's LinkedName.symbol to the unit tests.
    '0018-specialized-linked-functions-closed-before-linking.patch',
    # The pooled block size from the environment (METAL2VULKAN_BLOCK_MIB), which the Metal plugin sets to 16 MiB: every block is wired
    # guest memory, and each pool grows, keeps room and keeps an empty block a whole block at a time (the render server held 547-616 MiB
    # of empty 64 MiB blocks at the home screen; opening an app grew its blocks by 640 MiB for 187 MiB placed in them).
    '0019-pooled-block-size-from-the-environment.patch',
    # Device::hold_back_memory: while the Metal plugin reports memory pressure raised (its source fires as the level changes, so it
    # turns this on at warn or critical and off at normal), no room kept ahead of the placements, no empty pooled block and no spare
    # kept: a one-time trim came back with the next frame's room, 547-616 MiB of empty wired blocks in the render server.
    '0020-hold-back-memory-under-pressure.patch',
    # Turnip hands Metal's DontCare and MultisampleResolve stores DONT_CARE (DriverFact::DontCareStoreIsOneOfMetals, measured on the
    # Adreno 840 by gfxprobe dontcare: each texel the earlier one under GMEM, the stored one under SYSMEM): storing them wrote a 4x
    # attachment's samples to memory from every tile, 37 ms against 9 ms a full-screen pass of 64 blended draws.
    '0021-turnip-dont-care-stores.patch',
    # The spare command pools a busier spell made, released by the kept-memory trims (every 2 s, those spare for 20 s, all but
    # the 8 newest; on memory pressure all but 2): a pool keeps every command buffer it made and Turnip their command stream
    # memory, and the render server held 13,890 blobs (390 MiB of wired guest memory) after windowed apps and Maps.
    '0022-trim-spare-command-pools.patch',
    # Diagnostic: /private/var/ngpu/debug/dont-care-store at process start stores every DontCare attachment (STORE), for an
    # A/B of 0021 against CoreAnimation's material backdrops.
    '0023-dont-care-store-switch.patch',
    # Which DontCare attachments keep DONT_CARE, per class (1 single-sampled colors, 2 multisampled resolved, 4 depth/stencil),
    # 7 by default, as 0021 (the dark dock glass was 0026's carried shadow, not DONT_CARE); /private/var/ngpu/debug/dont-care-mask
    # overrides the mask for an A/B.
    '0024-dont-care-kept-per-attachment-class.patch',
    # Diagnostic: /private/var/ngpu/debug/full-barriers at process start puts a full memory barrier before every pass, copy, blit
    # and dispatch (an A/B for a dependency missing between a pass and a later read of what it rendered).
    '0025-full-barriers-switch.patch',
    # A shadow's copy back leaves the textures other encoders carried over the same truth to copy in again: a render pass's
    # store into a rotated framebuffer left the image its later passes sample stale (CoreAnimation's backdrops).
    '0026-copy-back-drops-carried-shadows.patch',
    # METAL2VULKAN_KEEP_ROOM_MIB caps the room keep_room keeps in each pool: a free range of half a block, which a pool
    # whose free bytes were in pieces made another block for (the render server's pooled blocks half empty).
    '0027-keep-room-cap-from-the-environment.patch',
    # A diagnostic: the live placements by the source line that asked for them (#[track_caller] through the image and buffer
    # helpers), m2v_placement_sites: which of the runtime's own images (shadows, twins, staging) a process's GPU memory holds.
    '0028-placement-sites.patch',
    # A queue's memoryless color backings serve every single-sampled attachment of their kind they cover, a new one as large
    # as the largest kept: Core Animation's many extents each kept one (the render server's 254 MiB of backings).
    '0029-memoryless-backings-cover.patch',
    # The shadows' spare merge scratches go with the idle trim (10 s unused) and on memory pressure: a still wallpaper's
    # drawables kept 64 MiB of them for good.
    '0030-idle-merge-scratch-spares.patch',
    # Diagnostics: METAL2VULKAN_BACKING_COVER=0 turns 0029 off for an A/B of the frames; METAL2VULKAN_POOL_REPORT=1 keeps each
    # pool's blocks (size, used, free ranges, longest) at the idle trim, m2v_pool_report.
    '0031-backing-cover-switch-and-pool-report.patch',
    # Upstream metal2vulkan's resident-uses series (development, 2026-11-03..05), backported: a draw's work no longer grows with
    # what its encoder made resident. Each draw had built its resident set, handle tables, heap elements and uses over every
    # resource the encoder made resident, and noted, gathered and checked them all: a pass of N draws among R resident
    # resources cost N x R. WebKit's WebGPU makes every resource of its render bundles resident, and replays the bundles'
    # indirect commands at a host step, so webgpu-samples' renderBundles (5000 draws, ~10000 resident) took over 30 s a frame
    # and Safari killed the GPU process. Its draws take several meshes in turn, so their plans alternate: the kept uses and
    # shapes must hold across plans too (ba3e45f1, 537667df, fa99ed2c, dc7b3d5d). Each of its bind groups makes a texture view
    # of its own, all resident: a draw's heap part (handle tables, heap elements, view checks) and its look at the resident
    # textures are kept while what they are made of holds (493178c82, 2282779a7, 01a902edd, 7089c6ddb, a1d400d1, f5d7b958, and
    # 81681968's shadow, pvrtc and residency parts). Commits af7da0a0, 5b5f1847, 81531d6c, 626bd347, 8da041d6, 3b5badc4,
    # ec85bf89, 1ec132c75, f7e7dc2f, 669b2df5, 1181ebe7, 5a86608e, 6be8045e, 2456ca75 (+ 793516031's name), ba3e45f1,
    # 537667df, fa99ed2c, dc7b3d5d, 81681968, a1d400d1, f5d7b958, 493178c82, 2282779a7, 01a902edd, 7089c6ddb, each without its
    # counters (no-ops in work_counts.rs where the code calls them), its tests and the kept-draw (draw_again) and compute
    # live-dispatch paths this tree lacks. Their debug assertions check every kept answer against one worked out whole.
    # icb_scale (lavapipe, indexed ICB draws): 5000 among 10000 resident 44.2 s -> 0.20 s a frame; 2000 over 4 meshes, each
    # with a texture view of its own, 2.5 s -> 0.13 s; the same pixels.
    '0032-resident-uses-once-per-identity.patch',
    # What an encoder made resident, held once instead of once per shape, plan and render pass instance. WebKit's GPU process held
    # 342-418 MiB of the runtime's heap for webgpu-samples' renderBundles (5000 draws, 15038 buffers resident), measured with the
    # plugin's sampled heap sites (client-mem): ~15 shapes of one encoder (one per mesh's buffer length classes) each copied the
    # 20000-entry used map (BindShape::used) into the settled shape, two plans' memo keys and the plans' own keys, and each shape's
    # two plans (base and replay variant) listed the resources again. Now: one shared map per used set (UsedShapes: a new shape takes
    # its recent shapes' map, which is the state's while they stay; its hash made once, its one-level form once), a kept plan holds
    # the very lists a live plan holds where equal (BindingPlan::share_lists, content-interned above 64 items), and a variant's memo
    # keeps one plan of shapes alike but for their used resources (BindShape::is_beside_used: without render bundles WebKit makes a
    # draw's bind group resident before it, so each draw's shape is its own). A render pass instance reserves a buffer's 17 ranges
    # at its second range, not its first, and one the pass went past packs its gathered bytes and lets go of its last uses
    # (InstanceShadows::close: WebKit's memory barrier between such draws splits an instance per draw). renderBundles' GPU process
    # 444 -> 150-175 MiB footprint (runtime heap 82-117 MiB), two of its pages at once 153-175; without bundles it lived 5 s
    # (743 MiB) and now ~50 s (each instance still gathers the resident set, which grows with the draws). icb_scale: the same
    # pixels in every mode, its debug assertions in 9 combos; per-draw residency 2000 draws 156 -> 19 MiB while encoding, 2.8 -> 2.2 s.
    '0033-shared-used-maps-and-plan-lists.patch',
    # What a render encoder made resident as the encoder's, not each draw's. Its bind state lists none of it (`resident_apart`):
    # a render plan states how its functions reach it (BindingPlan::resident, ResidentReach) and lists no use of it, so no
    # shape, plan or heap part is of the residency, and a draw's uses of it are made from the reach and the residency
    # (binding::apart_uses_into), extended by the tokens the residency added since where it only added (Residency::lineage,
    # rewritten), under an identity that extends the last (ResidentPrefix::extends). Their consumers take an extended
    # prefix's tail alone (tracker, noted-apart, pass-start reads, geometry writes, attachment reads, heaps touched), keep a
    # few lines of identities (TakenLines: WebKit's index clamp and its draw are two pipeline states, each a line), skip a
    # prefix with no access of their kind (its stages), and each pipeline state's uses go aside with its bind state. A render
    # pass instance holds the uses it began with by the encoder's logs of them (ResidentLogs), gathered only where the pass
    # plans shadow copies; a memory barrier over buffers or textures after no command that may have written memory splits
    # nothing. icb_scale (lavapipe): per-draw residency 1000/2000/4000 draws 473/2212/13966 -> 17/33/68 ms encoding; WebKit's
    # clamp + barrier + draw pattern 536/2240/10416 -> 40/83/180 ms; direct 2000 draws 113 -> 39 ms; the same pixels, the
    # debug assertions (every kept and extended answer against one made whole) in 12 combos. renderBundles on 5566: GPU
    # process 121 -> 68-88 MiB, ~1.75x the frames; without bundles it lives (peak 467 MiB in its first frame of 5000 clamp
    # barriers, then 258 MiB) and animates at the bundle mode's rate.
    '0034-encoder-level-residency.patch',
    # An access through a wide pointer (one loaded from memory: WebKit's bind groups are argument buffers) reads no root entry
    # (upstream ce204a121, backported): the pointer is (root, at, host, size), the entry read once where the pointer is formed,
    # where before every access read four entry fields again, under no decoration, beside the miss counter's atomics (no driver
    # could merge or move them). Every read of the root table and the resident set is through a NonWritable place
    # (FunctionBuilder::device_place_read_only), so a driver may merge and move those too. webgpu-samples' deferredRendering loops
    # over 128 lights per pixel through such a pointer: its light loop read 20 entry fields per light; ir3 (Adreno 830 over the
    # noop drm shim) 496 instructions + 363 nops, 37 memory ops, 11 (sy) waits per light -> 398 + 182, 17 (7 loads, the rest the
    # miss counter's atomics in untaken branches), 5; on 5566 the frame's GPU time 73 -> 60 ms at 1200x1200 and 1.2 GHz (Android
    # Chrome on the same phone: ~10 ms). The used-uses tests take the reach (0034 left them building against the old signature).
    '0035-wide-pointers-carry-their-root-entry.patch']]


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
# The GPU scanout's half-float conversion kernel (GpuScanoutPresenter.h), hand-written AIR.
scanout_air = out / 'scanout_convert.air'
subprocess.run([llvm_as, '-o', scanout_air, ROOT / 'drivers/AdrenoMetal/shaders/scanout_convert.ll'], check=True)
manifest = out / 'Cargo.toml'
manifest.write_text('\n'.join([
    '[package]', 'name = "adreno-metal-bridge"', 'version = "0.1.0"', 'edition = "2024"', 'rust-version = "1.98"', '[workspace]',
    '[lib]', 'crate-type = ["staticlib"]', 'path = ' + json.dumps(str(ROOT / 'drivers/AdrenoMetal/bridge/NativeMetalBridge.rs')),
    '[dependencies]',
    'metal2vulkan = { path = ' + json.dumps(str(SOURCE / 'crates/metal2vulkan')) + ', features = ["ash"] }',
    'metal2vulkan-runtime = { path = ' + json.dumps(str(SOURCE / 'crates/metal2vulkan-runtime')) + ' }',
    'libc = "0.2"',
    # The runtime hashes every shader-cache key and entry; without asm, sha2 0.10 on aarch64 is the portable software rounds, not the
    # ARMv8 SHA-256 instructions every target CPU has (cpufeatures answers sha2 true on Apple platforms).
    'sha2 = { version = "0.10", features = ["asm"] }',
    # The render server's display thread spends most of a frame in the runtime, much of it in small functions across the
    # translator's crates: one optimisation unit with link-time optimisation lets them inline across crate boundaries.
    '[profile.release]', 'panic = "unwind"', 'debug = 1', 'lto = "fat"', 'codegen-units = 1', '',
]))
if args.manifest_only:
    print(manifest)
    raise SystemExit(0)
triple = {'ios': 'aarch64-apple-ios', 'ios27': 'arm64e-apple-ios', 'host': 'aarch64-apple-darwin'}[args.target]
# arm64e has no prebuilt std: it is built from rust-src (RUSTC_BOOTSTRAP for -Z); the iPadOS 27 processes that load the plugin are arm64e.
cargo = ['cargo', '+1.98.0'] + (['-Z', 'build-std=std,panic_unwind'] if args.target == 'ios27' else [])
# The translator source the runtime is built from (its pin and recorded patches), which keys the shader cache: shader priming keeps what it
# finished while this stays the same (PrimeShaders.h).
runtime_id = hashlib.sha256((PIN + '\n' + states[-1]).encode()).hexdigest()[:32]
env = dict(os.environ, NMB_RUNTIME_ID=runtime_id, NMB_FIXTURE_AIR=str(air), NMB_FC_AIR=str(fc_air), NMB_SCANOUT_AIR=str(scanout_air), IPHONEOS_DEPLOYMENT_TARGET='27.0' if args.target == 'ios27' else '17.0', MACOSX_DEPLOYMENT_TARGET='14.0')
if args.target == 'ios27':
    env['RUSTC_BOOTSTRAP'] = '1'
target_dir = ROOT / 'build/m2v-target'
log = out / ('build-%s.log' % args.target)
with log.open('w') as stream:
    result = subprocess.run(cargo + ['build', '--release', '--manifest-path', manifest, '--target', triple,
                             '--target-dir', target_dir], env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
if result.returncode:
    raise SystemExit(log.read_text()[-6000:])
library = target_dir / triple / 'release/libadreno_metal_bridge.a'
final = out / args.target / 'libadreno_metal_bridge.a'
final.parent.mkdir(exist_ok=True)
shutil.copyfile(library, final)
print('PASS: native Metal bridge static library for %s (%d bytes); translator %s' % (args.target, final.stat().st_size, PIN[:12]))
