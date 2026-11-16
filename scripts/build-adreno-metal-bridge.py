#!/usr/bin/env python3
"""Build the native Metal bridge (drivers/AdrenoMetal/bridge/NativeMetalBridge.rs): the pinned metal2vulkan runtime behind a C ABI, as a static library.

--target ios (default) cross-builds for arm64-apple-ios17.0, --target ios27 for arm64e-apple-ios27.0 and --target macos27 for arm64e-apple-macos27.0 (nightly, std built from source), --target host for the macOS host simulation. The runtime's own example library
(runtime_examples.ll) and its function-constant fixture are assembled to AIR with llvm-as and embedded for the self-tests. Only generated build files hold local paths; nothing here
is committed. Output: build/adreno-metal-bridge/<target>/libadreno_metal_bridge.a"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / 'scripts/data/gpu-metal-vulkan-inputs.json').read_text())['translator']['commit']
SOURCE = ROOT / '.cache/gpu-reference/metal2vulkan'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--target', choices=['ios', 'ios27', 'macos27', 'host'], default='ios')
parser.add_argument('--manifest-only', action='store_true',
                    help='write build/adreno-metal-bridge/Cargo.toml (patched source, fixtures) and stop before cargo, for `cargo fetch`')
args = parser.parse_args()
if subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip() != PIN:
    raise SystemExit('Translator revision differs from the qualified source pin')
PATCHES = [ROOT / 'drivers/AdrenoMetal/bridge/patches' / name for name in [
    # Each stacks on the source the ones before it leave.
    '0002-resource-wait-storage-dependencies.patch', '0004-fold-constant-global-field-loads.patch',
    # A draw whose pipeline the driver refused binds nothing (int-170: tu_CmdBindPipeline in the render server).
    '0005-skip-draws-without-a-pipeline.patch',
    # A cache directory a sandboxed process may not write, and a patch function with no control points.
    '0007-optional-shader-cache-directory.patch', '0009-patch-with-no-control-points.patch',
    # A two-plane surface's shadow starts as its memory lies.
    '0011-two-plane-shadow-as-memory-lies.patch',
    # Device::refuse_rebinds: no call logs or per-command holds of no-copy buffers, which only a rebind or a detach reads.
    '0016-refuse-rebinds.patch',
    # A heap texture's own-device answer rounded up to a multiple of its alignment, as Metal's answers are.
    '0017-heap-texture-size-a-multiple-of-its-alignment.patch',
    # The completion and compile threads' quality-of-service classes on iOS too.
    '0023-quality-of-service-on-ios.patch',
    # A cache hit stops refreshing access times after the platform refuses it once.
    '0032-shader-cache-touch-refused-once.patch',
    # The rest of the board's runtime over this pin, in one patch: the priming cache of device entry points and the system shader cache
    # layer; a batch's barrier globals on the stack; pooled blocks that start at 4 MiB and grow, which the runtime's blocks made ahead
    # follow, and every dedicated block kept within the heap's budget; HostPageTexturePolicy::InPlaceWrites (textures over surfaces and
    # padded buffers rendered and written in place where their tile stores stay within their rows); the room thread, which runs the
    # runtime's keep_room and frees kept empty blocks and parked objects by time; memory census bytes, kinds by size and allocation
    # sites; the stage trace with its diagnostics switch and latency stages; specialized links, shared constants and allocation sizes;
    # YCbCr surface textures over their planes alone.
    '0040-unixnu-runtime-on-development-555b13d5.patch',
    # A driver that compiles inside the call: pipeline makings stay on the asking thread (METAL2VULKAN_COMPILE_ON_ASKING_THREAD=1)
    # instead of a thread started for each, which a thread without a quality-of-service class waited for.
    '0041-compile-on-the-asking-thread.patch'
    ]] + [ROOT / 'patches/metal2vulkan' / name for name in [
    # From Droid-VM/macOS-DroidVM (its metal2vulkan-macos/0003-0005, made on this pin): reflection of rasterization rate-map buffers
    # and of opaque indirect pointers (Weather's VFX materials), and no work for an identity texture swizzle.
    '0040-rate-map-buffer-reflection.patch', '0041-opaque-indirect-pointer-reflection.patch', '0042-identity-texture-swizzle.patch',
    # The board's own, in the order they were carried to 555b13d5 (each stacks on the ones before it). Not carried, as 555b13d5 or
    # bridge 0040 does the same: 0016 and 0018 (specialized-name links, constants bound before linking), 0026 (a copy back drops
    # carried shadows: upstream dd986f0a), 0022 (spare command pools: upstream's spares.rs keeps only recent
    # uses' command buffers and trims the pools), 0032 and 0035 (backports of upstream commits), 0039 (planes-alone YCbCr textures: bridge
    # 0040, asked for by the bridge itself). Not carried: 0034 (encoder-level residency), whose cost remains on 555b13d5 but whose
    # changes meet upstream's newer kept draws and executions throughout.
    # The board's own. A rotated texture's turns between its pass's frame and its plane (CoreAnimation's framebuffer, rotation 3
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
    # Still needed on 555b13d5: bridge 0040's room thread frees stale empty blocks but never a pool's last one.
    '0013-trim-kept-memory.patch',
    # A quarter-turned texture over a surface plane held to the plane's row, not its own: the iPad's display framebuffer is a
    # portrait surface behind the render server's landscape texture (see the fork copy for the reason).
    '0014-quarter-turn-plane-row.patch',
    # A [[color(n)]] read of a color the render pipeline has no attachment for reads zero instead of refusing the pipeline
    # (LoweringInputs::unattached_reads): iPadOS 27's QuartzCore makes path_blit_frag_lph (its coverage mask at color 2) and
    # fixed_frag_lph_cph for passes without that attachment, and the render server aborted in create_pipeline_state.
    # On 555b13d5 also past pipeline::compile::color_inputs_attached, upstream's refusal of any [[color(n)]] input without an
    # attachment (measured on macOS's Metal), which ran before the lowering and refused QuartzCore's path_blit_frag_lph again.
    '0015-unattached-color-reads.patch',
    # Diagnostic: /private/var/ngpu/debug/dont-care-store at process start stores every DontCare attachment (STORE), for an
    # A/B of 0021 against CoreAnimation's material backdrops.
    '0023-dont-care-store-switch.patch',
    # Which DontCare attachments keep DONT_CARE, per class (1 single-sampled colors, 2 multisampled resolved, 4 depth/stencil),
    # 7 by default, as 0021 (the dark dock glass was 0026's carried shadow, not DONT_CARE); /private/var/ngpu/debug/dont-care-mask
    # overrides the mask for an A/B.
    '0024-dont-care-kept-per-attachment-class.patch',
    # Turnip hands Metal's DontCare and MultisampleResolve stores DONT_CARE (DriverFact::DontCareStoreIsOneOfMetals, measured on the
    # Adreno 840 by gfxprobe dontcare: each texel the earlier one under GMEM, the stored one under SYSMEM): storing them wrote a 4x
    # attachment's samples to memory from every tile, 37 ms against 9 ms a full-screen pass of 64 blended draws.
    # On 555b13d5 a row beside bridge 0040's Adreno 740 one (read in Mesa's source): any Adreno, measured.
    '0021-turnip-dont-care-stores.patch',
    # A device-address access reaches its words as places of one address: the access's offset splits into its 4 GiB page,
    # added to the root's address once (the high word's sum alone), and a 32-bit word index, and each word is the place
    # words[index + j] of that address (psb::split, PsbMem::indexed). Before, every word had a 64-bit device address of its
    # own (base + 4 * word in u64): on a 32-bit GPU a 32-bit add, a carry compare and a second add per word, which no driver
    # folds into the load. Turnip lowers such a place to a global load with a 64-bit base and a 32-bit offset (ldg.a), and
    # merges a leaf's words into one load. A kernel of deferredRendering's shape (a light array read per light through a bind
    # group's pointer), ir3 over the noop drm shim as an Adreno 830: its light loop 258 -> 217 instructions, the position's
    # three words one vec3 load. On 5566 (a840, 1200 MHz) webgpu-samples' deferredRendering: the light pass 60 -> 44.5 ms a frame
    # (u_trace), 14.5-15 -> 19.8 fps shown, the render server's reads of WebKit's surfaces no longer reach their 100 ms cap;
    # MetalCheck passes. SEMANTICS_VERSION 1924: every cached pipeline is made again.
    # On 555b13d5 the narrow 1- and 2-byte units upstream added are reached the same way. SEMANTICS_VERSION 2022 -> 2023.
    '0036-words-indexed-from-one-address.patch',
    # A cube sample's direction goes through Metal's tie handling (face::metal_ties: ties broken x over y over z, components
    # Metal reads as zero, a zero direction, huge magnitudes) only where it may need it: a direction whose magnitudes are all
    # at least 2^-125, whose largest is at most 2^125 and one component's alone is left unchanged by that handling, and is
    # tested first (a dozen instructions; the exact handling a branch). The direction is made before the sampled image, whose
    # OpSampledImage must stay in the block of the access. ir3 (Adreno 830, noop drm shim), a cube sample of an interpolated
    # direction: 86 -> 16 instructions on the plain path. The face tests (every device reads Metal's face and texel, ties,
    # zero, huge, infinities and NaNs) pass unchanged. GFXBench's shaders: 28 fragment shaders sample cubes; on 5566 Car Chase
    # (car reflections) held 46.4 fps at a thermally capped 578 MHz where 0036 made 47.3 at 726 (~12.3 vs 14.6 Mcycles a frame).
    # SEMANTICS_VERSION 2023 -> 2024 on 555b13d5.
    '0037-plain-cube-directions-skip-the-tie-handling.patch',
    # Diagnostic: /private/var/ngpu/debug/full-barriers at process start puts a full memory barrier before every pass, copy, blit
    # and dispatch (an A/B for a dependency missing between a pass and a later read of what it rendered).
    '0025-full-barriers-switch.patch',
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
    # On 555b13d5 the render pass instance's part is redone over upstream's InstanceShadows (sorted ranges, DrawRoom reuse): an
    # instance is closed only where the new one does not continue it, and not where its room is a DrawRoom's.
    '0033-shared-used-maps-and-plan-lists.patch',
    # An execution of render commands whose buffer an encoder made resident for writing reads its records at commit, as for
    # records the CPU alone wrote, once every command buffer that could write them completed (IndirectCommandBuffer::
    # settled_writes, a count of the GpuWrite holds ever taken) and none was taken since (RenderExecution::writes_unsettled); it
    # is read as the host step reads such records (validated, faults counted) and its host step records it again where a writer
    # was pending or appeared before its batch was ready. An execution that may run on the GPU alone (gpu_commands holds) keeps
    # the old path. gpu_written was for good: WebKit's render bundles took a host step every frame (a GPU round trip, then ~280 ms
    # of CPU recording on the scheduler's thread for 5000 commands) though their index clamp writes the records only until its
    # validation is cached. icb_scale (lavapipe, 5000 commands): every frame writing records as before; only the first writing
    # (WRITE_FRAMES=1), later frames record at commit with no host step; the texels the same in every mode. 5566, webgpu-samples'
    # renderBundles (5000 bundled draws): 2.3 -> 2.6 fps, GPU busy 48 -> 63%; the recording moved from the scheduler's host step
    # to WebKit's commit (its work queue thread 73% busy). The device-executing unit tests fail no test the base does not.
    # On 555b13d5 a commit reads a settled buffer's records whole through written_commands (never from CPU-kept chunks), and every
    # commit read marks read_writes, so a command buffer committed after a writer completed is not skipped.
    '0038-settled-gpu-writes-read-at-commit.patch',
    # The spare dedicated image blocks (bridge patch 0021) held to a sixteenth of the GPU address space (sparseAddressSpaceSize):
    # each of the board's GPU contexts has a slice of the host GPU's, and with spares holding part of it the render server's Turnip
    # found no address range for its next BO and lost the device.
    # On 555b13d5 dedicated blocks are kept inside the pools within the heap's kept budget (an eighth of the heap, at most 2 GiB):
    # on Turnip that budget is held to a sixteenth of the address space, which now bounds everything kept against it.
    '0017-spares-within-the-address-space.patch',
    # The pooled block size from the environment (METAL2VULKAN_BLOCK_MIB), which the Metal plugin sets to 16 MiB: every block is wired
    # guest memory, and each pool grows, keeps room and keeps an empty block a whole block at a time (the render server held 547-616 MiB
    # of empty 64 MiB blocks at the home screen; opening an app grew its blocks by 640 MiB for 187 MiB placed in them).
    # On 555b13d5 a power of two from 4 to 64; a block of images is that and a sixteenth, as upstream sizes those.
    '0019-pooled-block-size-from-the-environment.patch',
    # Device::hold_back_memory: while the Metal plugin reports memory pressure raised (its source fires as the level changes, so it
    # turns this on at warn or critical and off at normal), no room kept ahead of the placements, no empty pooled block and no spare
    # kept: a one-time trim came back with the next frame's room, 547-616 MiB of empty wired blocks in the render server.
    # On 555b13d5 also no block made ahead (those made are freed), and each pool's blocks freed as they empty, as upstream's
    # lost-device path keeps none; bridge 0040 alone gives back the kept memory once, and its room thread makes it again.
    '0020-hold-back-memory-under-pressure.patch',
    # METAL2VULKAN_KEEP_ROOM_MIB caps the room keep_room keeps in each pool: a free range of half a block, which a pool
    # whose free bytes were in pieces made another block for (the render server's pooled blocks half empty).
    # On 555b13d5 the cap also lowers the threshold for a block made ahead and the sweep of those never used.
    '0027-keep-room-cap-from-the-environment.patch',
    # A diagnostic: the live placements by the source line that asked for them (#[track_caller] through the image and buffer
    # helpers), m2v_placement_sites: which of the runtime's own images (shadows, twins, staging) a process's GPU memory holds.
    '0028-placement-sites.patch',
    # A queue's memoryless color backings serve every single-sampled attachment of their kind they cover, a new one as large
    # as the largest kept: Core Animation's many extents each kept one (the render server's 254 MiB of backings).
    '0029-memoryless-backings-cover.patch',
    # The shadows' spare merge scratches go with the idle trim (10 s unused) and on memory pressure: a still wallpaper's
    # drawables kept 64 MiB of them for good.
    # On 555b13d5 the scratches are timed and listed process-wide; the trim drops those unused for 5 x its idle time (at least 10 s).
    '0030-idle-merge-scratch-spares.patch',
    # Diagnostics: METAL2VULKAN_BACKING_COVER=0 turns 0029 off for an A/B of the frames; METAL2VULKAN_POOL_REPORT=1 keeps each
    # pool's blocks (size, used, free ranges, longest) at the idle trim, m2v_pool_report.
    # On 555b13d5 the report names upstream's six pool kinds (B I X D F P) and lists blocks made ahead ("ahead").
    '0031-backing-cover-switch-and-pool-report.patch']]


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
def assemble_fixture(source, target):
    # Preserve an unchanged include_bytes input so a frontend relink keeps its Rust archive.
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.air', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        subprocess.run([llvm_as, '-o', temporary, source], check=True)
        if not target.is_file() or temporary.read_bytes() != target.read_bytes():
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

air = out / 'runtime_examples.air'
assemble_fixture(SOURCE / 'crates/metal2vulkan-validation/fixtures/public/examples/runtime_examples.ll', air)
fc_air = out / 'function_constant.air'
assemble_fixture(SOURCE / 'crates/metal2vulkan-validation/fixtures/public/kernel_function_constant_gate_fold.ll', fc_air)
# The GPU scanout's half-float conversion kernel (GpuScanoutPresenter.h), hand-written AIR.
scanout_air = out / 'scanout_convert.air'
assemble_fixture(ROOT / 'drivers/AdrenoMetal/shaders/scanout_convert.ll', scanout_air)
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
triple = {'ios': 'aarch64-apple-ios', 'ios27': 'arm64e-apple-ios', 'macos27': 'arm64e-apple-darwin', 'host': 'aarch64-apple-darwin'}[args.target]
ARM64E = args.target in ('ios27', 'macos27')
# arm64e has no prebuilt std: it is built from rust-src (RUSTC_BOOTSTRAP for -Z); the iPadOS 27 processes that load the plugin are arm64e.
cargo = ['cargo', '+1.98.0'] + (['-Z', 'build-std=std,panic_unwind'] if ARM64E else [])
# The translator source the runtime is built from (its pin and recorded patches), which keys the shader cache: shader priming keeps what it
# finished while this stays the same (PrimeShaders.h).
runtime_id = hashlib.sha256((PIN + '\n' + states[-1]).encode()).hexdigest()[:32]
env = dict(os.environ, NMB_RUNTIME_ID=runtime_id, NMB_FIXTURE_AIR=str(air), NMB_FC_AIR=str(fc_air), NMB_SCANOUT_AIR=str(scanout_air), IPHONEOS_DEPLOYMENT_TARGET='27.0' if args.target == 'ios27' else '17.0', MACOSX_DEPLOYMENT_TARGET='27.0' if args.target == 'macos27' else '14.0')
if ARM64E:
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
