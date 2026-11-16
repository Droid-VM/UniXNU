#!/usr/bin/env python3
"""Build the owner-signed Adreno userspace pieces for ARM64/iOS 17: the Metal plugin bundle (drivers/AdrenoMetal/NativeAdrenoMetalPlugin.mm)
and the launchd probe daemon with its job plist (NativeAdrenoProbeDaemon.cpp).

Output (ignored, never committed): build/adreno-metal-plugin/NativeAdrenoMetal.bundle with a 4 KiB-page ad-hoc signature, and
trust.json with its CDHash. The bundle is staged into the Data seed by add-gpu-plugin-data.py and trusted through an extra
owner trust module (make-owner-trust-bundle.py); neither changes the original trust modules, AMFI or any Apple signature."""
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# --target ios27: the arm64e build for iPadOS 27 (24A446), whose render server is an arm64e process.
# --target macos27: the arm64e build for macOS 27, in the macOS bundle layout (Contents/MacOS; upstream e7c359c, d434fc2).
TARGET = sys.argv[2] if sys.argv[1:2] == ['--target'] and len(sys.argv) == 3 else 'ios'
if TARGET not in ('ios', 'ios27', 'macos27'):
    raise SystemExit(f'unknown target {TARGET}')
MACOS = TARGET == 'macos27'
# IOS27 marks the arm64e builds (iPadOS 27 and macOS 27): pointer authentication, the PAC bridge and the arm64e checks.
IOS27 = TARGET in ('ios27', 'macos27')
OUT = ROOT / 'build' / {'ios': 'adreno-metal-plugin', 'ios27': 'adreno-metal-plugin27', 'macos27': 'adreno-metal-plugin-macos27'}[TARGET]
bundle = OUT / 'NativeAdrenoMetal.bundle'
# macOS bundles keep the executable in Contents/MacOS; Turnip stays beside it (turnip::libraryPath).
contents = bundle / 'Contents' if MACOS else bundle
code_dir = contents / 'MacOS' if MACOS else bundle
if OUT.exists():
    shutil.rmtree(OUT)
code_dir.mkdir(parents=True)
sdk = subprocess.check_output(['xcrun', '--sdk', 'macosx' if MACOS else 'iphoneos', '--show-sdk-path'], text=True).strip()
base = ['xcrun', 'clang', '-target', {'ios': 'arm64-apple-ios17.0', 'ios27': 'arm64e-apple-ios27.0', 'macos27': 'arm64e-apple-macos27.0'}[TARGET],
        '-isysroot', sdk, '-O2', '-g', '-Wall', '-Wextra', '-Werror', '-fstack-protector-strong']
VULKAN_INCLUDE = ROOT / '.cache/mesa-work/include'
TURNIP = ROOT / 'build' / ('mesa-tu-' + TARGET) / 'src/freedreno/vulkan/libvulkan_freedreno.dylib'
if not TURNIP.is_file():
    raise SystemExit('Build Turnip first: python3 scripts/build-turnip-darwin.py --target ' + TARGET)
subprocess.run(['python3', ROOT / 'scripts/gen-turnip-smoke-shaders.py'], check=True)
# Turnip's first submission, recorded on the host, for the plugin's prefix replay (build/turnip-smoke/TurnipReplay.h): a test of
# NativeAdreno's own command processor, which runs after the shared user test has passed on it. ADRENO_METAL_REPLAY=0 builds
# without it (NativeAdrenoUserTest.h compiles the replay only when the header is there): a guest whose GPU is not NativeAdreno's
# own never reaches it, and recording it needs the host simulation of that kernel stack (check-turnip-darwin-host.py).
if os.environ.get('ADRENO_METAL_REPLAY', '1') != '0':
    subprocess.run(['python3', ROOT / 'scripts/gen-turnip-replay.py'], check=True)
else:
    (ROOT / 'build/turnip-smoke/TurnipReplay.h').unlink(missing_ok=True)
BRIDGE_TARGET = TARGET
subprocess.run(['python3', ROOT / 'scripts/build-adreno-metal-bridge.py', '--target', BRIDGE_TARGET], check=True)
BRIDGE = ROOT / 'build/adreno-metal-bridge' / BRIDGE_TARGET / 'libadreno_metal_bridge.a'
# arm64e: the Rust objects' imports that take a callback go through the signing wrappers of RustPacBoundary.c, and their indirect branches
# are routed through its stripping trampolines after the link (scripts/rust-pac-rewrite.py).
link_extra, PAC_RENAMES = [], {'_pthread_create': '_nmb_pac_pthread_create', '__tlv_atexit': '_nmb_pac_tlv_atexit',
                               '_atexit': '_nmb_pac_atexit', '__dyld_register_func_for_add_image': '_nmb_pac_dyld_register_func_for_add_image',
                               '__Unwind_Backtrace': '_nmb_pac_Unwind_Backtrace', '_dlsym': '_nmb_pac_dlsym'}
if IOS27:
    objcopy = next(p for p in ['/opt/homebrew/opt/llvm/bin/llvm-objcopy', shutil.which('llvm-objcopy') or ''] if p and Path(p).exists())
    pac_bridge = OUT / 'libadreno_metal_bridge_pac.a'
    subprocess.run([objcopy, *[f'--redefine-sym={a}={b}' for a, b in PAC_RENAMES.items()], BRIDGE, pac_bridge], check=True)
    BRIDGE = pac_bridge
    boundary = OUT / 'RustPacBoundary.o'
    subprocess.run([*base, '-std=c11', '-I', VULKAN_INCLUDE, '-c', ROOT / 'drivers/AdrenoMetal/bridge/RustPacBoundary.c', '-o', boundary],
                   check=True)
    link_extra = [boundary, '-Wl,-map,' + str(OUT / 'NativeAdrenoMetal.map')]
pm4 = OUT / 'AdrenoPM4.o'
subprocess.run([*base, '-std=c11', '-c', ROOT / 'kits/AdrenoPM4/AdrenoPM4.c', '-o', pm4], check=True)
exe = code_dir / 'NativeAdrenoMetal'
# What the OS frameworks need from the Metal objects and NativeMetal does not do yet; the frontend and the plugin are compiled from
# staged copies with each change placed at its anchor, for as long as NativeMetal lacks it.
# - respondsToSelector: (device) without targetDeviceInfo: the device inherits it from the target's _MTLDevice, which answers it from
#   driver state only the target's IOGPU drivers set (NULL here). Metal's pipeline library loader (MTLPipelineLibraryBuilder::
#   newLibraryWithFile, RenderBox's prebuilt pipelines) asks whether the device has it and then reads through it (a fault at 0x10 in
#   WidgetRenderer_Default); refused, the loader describes the device by its name instead.
# - setDepthClipMode: (render encoder): VectorKit sets it on every encoder (Maps aborted on the unrecognized selector). The runtime has
#   no depth clamp, so Clip, Metal's default, is what it draws; Clamp is drawn as Clip and said once.
ADDED_METHODS = [
    ('- (void)setLineWidth:(float)width {ENCODE([self check:nmb_render_set_line_width(self.handle,width) what:"setLineWidth"]);}\n',
     'setDepthClipMode',
     '- (void)setDepthClipMode:(MTLDepthClipMode)mode {\n'
     ' static std::atomic<bool> told{false};\n'
     ' if(mode!=MTLDepthClipModeClip && !told.exchange(true))NBLOG(@"native-metal: depth clamp requested; drawn with depth clipping");\n'
     '}\n'),
    ('- (BOOL)hasUnifiedMemory {return YES;}\n', 'targetDeviceInfo',
     '- (BOOL)respondsToSelector:(SEL)selector {\n'
     ' if(selector==sel_registerName("targetDeviceInfo"))return NO;   // the inherited _MTLDevice one, NULL without IOGPU\n'
     ' return [super respondsToSelector:selector];\n'
     '}\n'),
]
frontend = (ROOT / 'drivers/AdrenoMetal/NativeBridgeMetalFrontend.mm').read_text()
for anchor, marker, methods in ADDED_METHODS:
    if marker in frontend:
        continue
    if frontend.count(anchor) != 1:
        raise SystemExit(f'NativeBridgeMetalFrontend.mm no longer has exactly one {anchor.strip()!r}')
    frontend = frontend.replace(anchor, anchor + methods)
# NativeMetal itself has, since the macOS line's GPU branch merged in (2026-11-16), what this list once added: DroidVM's
# texture layout and textures over memory in it (DVMLinearTextureLayout), the supportsFamily: answer by caller (RenderBox,
# WebGPU, ANGLE), the render server's surface use counts and the XR10 answer for the poster extensions (macOS: WindowServer).
FRONTEND_REPLACED = [
    # Refusals said in the log, the first 64 of a process: failWith (a library, a pipeline the runtime refused), a function a library
    # does not give, and a pipeline descriptor refused before the runtime sees it (a missing or foreign function). They only went to the
    # problem counters, so AegirPoster's "pipelineState != nil" assertion (the Astronomy wallpaper) left no reason anywhere.
    (' noteProblem([NSString stringWithFormat:@"%@: %@",what,lastError()]);\n'
     ' if(error)*error=nbError([NSString stringWithFormat:@"%@: %@",what,lastError()]);\n',
     ' static std::atomic<int> told{0};\n'
     ' if(told.fetch_add(1)<64)NBLOG(@"native-metal: %@ refused: %@",what,lastError());else noteProblem([NSString stringWithFormat:@"%@: %@",what,lastError()]);\n'
     ' if(error)*error=nbError([NSString stringWithFormat:@"%@: %@",what,lastError()]);\n'),
    (' NmbFunction *handle=name?nmb_function_new(_handle,name.UTF8String):nullptr;gTimeFunction.add(functionBegan);\n'
     ' if(!handle)return nil;\n',
     ' NmbFunction *handle=name?nmb_function_new(_handle,name.UTF8String):nullptr;gTimeFunction.add(functionBegan);\n'
     ' if(!handle) {\n'
     '  static std::atomic<int> told{0};\n'
     '  if(told.fetch_add(1)<64)NBLOG(@"native-metal: function %@ refused: %@",name,lastError());\n'
     '  return nil;\n'
     ' }\n'),
    # (A refused render pipeline descriptor's line is AdrenoMetal's own since a0f0287b, with the pipeline's L row.)
    # IOSurfaces the render server textures are in use (IOSurfaceIncrementUseCount) for as long as the texture lives, as Apple's GPU
    # driver counts a surface while a GPU resource is made of it. An app's Core Animation draws into a backing store in place, or
    # recycles it for another layer, only while it is not in use (IOSurfaceIsInUse); with nothing counted, Settings redrew labels into
    # the surfaces on screen and recycled them for other rows, and the render server's next frames showed torn text and one row's
    # label in another's place. Only the render server counts: an app's own textures (its drawables) would look in use forever.
    ('@property(nonatomic) NSUInteger heapOffset;\n@end\n// A no-copy import keeps its pages',
     '@property(nonatomic) NSUInteger heapOffset;\n@property(nonatomic) BOOL counted;   // its surface counted in use (countUse)\n'
     '- (void)countUse;\n@end\n'
     '// The render server counts the surfaces it textures in use while the textures live (countUse).\n'
     'static const bool gCountsSurfaceUse=nativeIsRenderServer();\n'
     '// A no-copy import keeps its pages'),
    (' if(_surface)view.surface=(IOSurfaceRef)CFRetain(_surface);\n',
     ' if(_surface){view.surface=(IOSurfaceRef)CFRetain(_surface);[view countUse];}\n'),
    (' gSurfaceTextures++;texture.surface=(IOSurfaceRef)CFRetain(surface);return (id<MTLTexture>)texture;\n',
     ' gSurfaceTextures++;texture.surface=(IOSurfaceRef)CFRetain(surface);[texture countUse];return (id<MTLTexture>)texture;\n'),
]
for original, replacement in FRONTEND_REPLACED:
    if replacement in frontend:
        continue
    if frontend.count(original) != 1:
        raise SystemExit(f'NativeBridgeMetalFrontend.mm no longer has exactly one {original.strip()[:80]!r}')
    frontend = frontend.replace(original, replacement)
# - the plugin's clients list (NativeAdrenoMetal.clients, written from droidvm-gpu-clients): upstream gives every process its own
#   device, as Apple's plugin does. A board may name the processes instead (when every unsandboxed process had one, Maps' VectorKit
#   lost its device within seconds and wedged the render server's frames until a reboot); the iPadOS 27 board names "*" again since
#   2026-11-03, when that no longer happened. The list takes a name ending in '*' for every
#   process whose name begins with the rest: com.apple.WebKit.* gives WebKit's WebContent processes a device as well as its GPU process.
#   CoreGraphics draws a layer's wide-color backing store ('b3a8' RGB10A8, 'w30r' RGB10) only through the GPU; without a device
#   it draws nothing into one, and Safari's pages stayed black. The command line has no room to name each process. "*X*" names
#   every process whose name holds X: *Poster* gives PosterBoard and each wallpaper's poster extension (AegirPoster, the Kaleidoscope
#   and Extragalactic watch faces) a device; without one they crashed rendering the gallery's previews. "@apps" names every app: a
#   process whose executable lies in an app bundle container (/var/containers/Bundle/Application/), as any installed app's and its
#   extensions' do. On an iPad every app may use the GPU; the list's names stay for the system's resident processes, which each keep a
#   device and its memory pools here. The kernel's grant (patch zzw-native-adreno-sandbox-grant, and the board's
#   droidvm-gpu-grant-apps for "@apps") matches the same forms.
# - a client that may write in neither shader-cache directory -- WebKit's GPU process, whose sandbox refuses both -- got no device
#   ("no-cachedir"), so CoreGraphics drew the page's wide-color layers nowhere: it now opens one whose cache is never on disk (the
#   runtime's METAL2VULKAN_SHADER_CACHE_OPTIONAL, now AdrenoMetal's bridge patch 0007).
# - Turnip's flags: AdrenoMetal's default (TU_DEBUG=nocb: no concurrent binning, autotune choosing GMEM or system memory per pass)
#   unless the first line of /private/var/ngpu/tu_debug, read by each process that may read it, names others. DroidVM once forced
#   GMEM here: system-memory-only rendering (the earlier sysmem,nocb) left stale runs and garbled labels in composited layers once
#   several apps had run.
PLUGIN_REPLACED = [
    # (/private/var/ngpu/tu_debug is read by AdrenoMetal itself since a0f0287b, before its sheng-tu-debug= and sheng-fb-bgra=1.)
    ('static id clientDevice(unsigned port) {\n    char turnip[1024];\n',
     '// Whether NativeAdrenoMetal.clients names this process (names separated by blanks, commas or new lines; "*" names every process; a\n'
     '// name that ends in "*" names every process whose name starts with it, so "com.apple.WebKit.*" covers the WebContent, Networking\n'
     '// and GPU service processes; "*X*" names every process whose name holds X; "@apps" names every process run from an app bundle\n'
     '// container, as every installed app is).\n'
     'static bool appProcess() {\n'
     '    NSString *path = NSBundle.mainBundle.executablePath;\n'
     '    return [path hasPrefix:@"/private/var/containers/Bundle/Application/"] || [path hasPrefix:@"/var/containers/Bundle/Application/"];\n'
     '}\n'
     'static bool listedClient() {\n'
     '    char path[1024];\n'
     '    if (!requestedPath("NativeAdrenoMetal.clients", path, sizeof path)) return false;\n'
     '    FILE *file = fopen(path, "r");\n'
     '    if (!file) return false;\n'
     '    char text[1024] = {};\n'
     '    const size_t length = fread(text, 1, sizeof text - 1, file);\n'
     '    fclose(file);\n'
     '    text[length] = 0;\n'
     '    const char *self = getprogname();\n'
     '    for (char *save = nullptr, *name = strtok_r(text, " ,\\t\\r\\n", &save); name; name = strtok_r(nullptr, " ,\\t\\r\\n", &save)) {\n'
     '        if (!strcmp(name, "*") || !strcmp(name, self) || (!strcmp(name, "@apps") && appProcess())) return true;\n'
     '        const size_t nameLength = strlen(name);\n'
     '        if (nameLength > 2 && name[0] == \'*\' && name[nameLength - 1] == \'*\') {\n'
     '            name[nameLength - 1] = 0;\n'
     '            if (strstr(self, name + 1)) return true;\n'
     '        } else if (nameLength && name[nameLength - 1] == \'*\' && !strncmp(name, self, nameLength - 1)) return true;\n'
     '    }\n'
     '    return false;\n'
     '}\n'
     'static id clientDevice(unsigned port) {\n    if (!listedClient()) return nil;\n    char turnip[1024];\n'),
    ('    if (!device) clientFailure(port, usable ? "no-device" : "no-cachedir", lastError);\n',
     '    if (!device && !usable) {\n'
     '        setenv("METAL2VULKAN_SHADER_CACHE_OPTIONAL", "1", 1);\n'
     '        const char *path = candidates[0][0] ? candidates[0] : candidates[1][0] ? candidates[1] : "/var/mobile/tmp/m2v";\n'
     '        @try {\n'
     '            device = native_bridge_metal_open(turnip, path, registryID, nullptr);\n'
     '        } @catch (NSException *exception) {\n'
     '            device = nil;\n'
     '            lastError = "exception";\n'
     '        }\n'
     '        if (!device && !lastError[0]) lastError = nmb_last_error();\n'
     '    }\n'
     '    if (!device) clientFailure(port, usable ? "no-device" : "no-cachedir", lastError);\n'),
]
plugin = (ROOT / 'drivers/AdrenoMetal/NativeAdrenoMetalPlugin.mm').read_text()
for original, replacement in PLUGIN_REPLACED:
    if replacement in plugin:
        continue
    if plugin.count(original) != 1:
        raise SystemExit(f'NativeAdrenoMetalPlugin.mm no longer has exactly one {original.strip()!r}')
    plugin = plugin.replace(original, replacement)
staged_plugin = OUT / 'NativeAdrenoMetalPlugin.mm'
staged_plugin.write_text(plugin)
staged_frontend = OUT / 'NativeBridgeMetalFrontend.mm'
staged_frontend.write_text(frontend)
# The OS image's .metallib paths (build/prime-list.txt, one per line), carried in the executable's __DATA,__primelist section as well: the
# render server's sandbox may refuse to read the list file beside the bundle, and the plugin then primes from this copy (PrimeShaders.h).
PRIME_LIST = ROOT / 'build/prime-list.txt'
prime_section = [f'-Wl,-sectcreate,__DATA,__primelist,{PRIME_LIST}'] if PRIME_LIST.is_file() else []
subprocess.run([*(['xcrun', 'clang++'] + base[2:]), '-std=c++17', '-fobjc-arc', '-Wno-deprecated-declarations', '-bundle',
                '-I', ROOT / 'drivers/Adreno740', '-I', ROOT / 'kits/AdrenoPM4', '-I', VULKAN_INCLUDE, '-I', ROOT / 'build/turnip-smoke',
                '-I', ROOT / 'drivers/AdrenoMetal',
                '-x', 'objective-c++', staged_plugin,
                staged_frontend, '-x', 'none', pm4, BRIDGE,
                *link_extra, '-Wl,-dead_strip', *prime_section, '-lobjc', '-framework', 'Foundation', '-framework', 'Metal', '-framework', 'IOSurface',
                # macOS: the device class subclasses IOGPU's IOGPUMetalDevice (the SDK's private IOGPU.tbd exports it) and the
                # IOGPU adapter (IOGPUMetalDeviceShim.h) keeps the accelerator port with IOKit (upstream d434fc2).
                *(['-F', f'{sdk}/System/Library/PrivateFrameworks', '-framework', 'IOGPU', '-framework', 'IOKit'] if MACOS else []), '-o', exe],
               check=True)
if IOS27:
    subprocess.run(['python3', ROOT / 'scripts/rust-pac-rewrite.py', exe, OUT / 'NativeAdrenoMetal.map', 'libadreno_metal_bridge_pac.a'],
                   check=True)
subprocess.run(['strip', '-x', exe], check=True)
# The linker writes the debug symbols of the Rust bridge next to the executable; keep them beside the bundle, not in the image.
symbols = OUT / 'NativeAdrenoMetal.dSYM'
shutil.rmtree(symbols, ignore_errors=True)
if (code_dir / 'NativeAdrenoMetal.dSYM').exists():
    shutil.move(code_dir / 'NativeAdrenoMetal.dSYM', symbols)
# Turnip (the real Mesa Vulkan driver with the native Darwin kernel backend) travels inside the plugin bundle, signed on its own with 4 KiB pages so its
# CDHash can be trusted like the plugin's. It is signed before the bundle so the bundle's resource seal covers the final bytes.
turnip_lib = code_dir / 'libvulkan_freedreno.dylib'
shutil.copyfile(TURNIP, turnip_lib)
subprocess.run(['strip', '-x', turnip_lib], check=True)
subprocess.run(['codesign', '--force', '--sign', '-', '--pagesize', '4096', '--identifier', 'org.blu-sharky.turnip', turnip_lib], check=True)
turnip_detail = subprocess.run(['codesign', '--display', '--verbose=4', turnip_lib], capture_output=True, text=True, check=True).stderr
turnip_cdhash = re.search(r'^CDHash=([a-f0-9]{40})$', turnip_detail, re.M)[1]
assert re.search(r'^Page size=4096$', turnip_detail, re.M)
(contents / 'Info.plist').write_bytes(plistlib.dumps(dict(
    CFBundleIdentifier='org.blu-sharky.NativeAdrenoMetal', CFBundleExecutable='NativeAdrenoMetal', CFBundlePackageType='BNDL',
    CFBundleVersion='1', CFBundleShortVersionString='1', NSPrincipalClass='NativeAdrenoMetalDevice',
    **({'LSMinimumSystemVersion': '27.0'} if MACOS else {'MinimumOSVersion': '27.0' if IOS27 else '17.0'})), fmt=plistlib.FMT_XML))
subprocess.run(['codesign', '--force', '--sign', '-', '--pagesize', '4096', '--identifier', 'org.blu-sharky.NativeAdrenoMetal',
                bundle], check=True)
subprocess.run(['codesign', '--verify', '--strict', bundle], check=True)
detail = subprocess.run(['codesign', '--display', '--verbose=4', bundle], capture_output=True, text=True, check=True).stderr
cdhash = re.search(r'^CDHash=([a-f0-9]{40})$', detail, re.M)[1]
assert re.search(r'^Page size=4096$', detail, re.M), 'the signature must use 4 KiB pages'
header = exe.read_bytes()[:16]
magic, cputype, subtype = int.from_bytes(header[0:4], 'little'), int.from_bytes(header[4:8], 'little'), int.from_bytes(header[8:12], 'little')
assert (magic, cputype, subtype & 0xffffff) == (0xfeedfacf, 0x100000c, 2 if IOS27 else 0), 'expected an arm64e (27) or ARM64 (17) Mach-O'
# The probe daemon (launchd starts it as an owner job). The original platform sandbox policy refuses IOServiceOpen for any user-client class
# a process has not been granted (the same refusal an unentitled consumer got in the offline owner tests), so it carries exactly the grant that
# worked there: com.apple.security.iokit-user-client-class naming the one class it opens, plus no-container.
entitlements = OUT / 'adrenoprobed-entitlements.plist'
# The logging grants let it read the whole unified log for the export (NativeOsLogExport.mm).
entitlements.write_bytes(plistlib.dumps({'com.apple.private.security.no-container': True,
                                         'com.apple.security.iokit-user-client-class': ['AGXDeviceUserClient'],
                                         'com.apple.private.logging.admin': True, 'com.apple.private.logging.diagnostic': True,
                                         'com.apple.private.logging.stream': True}))
daemon = OUT / 'adrenoprobed'
subprocess.run(['xcrun', 'clang++', *base[2:], '-std=c++17', '-I', ROOT / 'drivers/Adreno740', '-I', ROOT / 'kits/AdrenoPM4', '-I', VULKAN_INCLUDE,
                '-I', ROOT / 'build/turnip-smoke',
                ROOT / 'drivers/AdrenoMetal/NativeAdrenoProbeDaemon.cpp', '-x', 'objective-c++', '-fobjc-arc',
                ROOT / 'drivers/AdrenoMetal/NativeOsLogExport.mm', '-x', 'none', pm4, '-framework', 'Foundation', '-lz', '-o', daemon], check=True)
subprocess.run(['codesign', '--force', '--sign', '-', '--pagesize', '4096', '--identifier', 'org.blu-sharky.adrenoprobed',
                '--entitlements', entitlements, daemon], check=True)
actual = plistlib.loads(subprocess.check_output(['codesign', '-d', '--entitlements', ':-', str(daemon)], stderr=subprocess.DEVNULL))
assert actual == plistlib.loads(entitlements.read_bytes()), 'the signed entitlement set differs'
subprocess.run(['codesign', '--verify', '--strict', daemon], check=True)
detail = subprocess.run(['codesign', '--display', '--verbose=4', daemon], capture_output=True, text=True, check=True).stderr
daemon_cdhash = re.search(r'^CDHash=([a-f0-9]{40})$', detail, re.M)[1]
assert re.search(r'^Page size=4096$', detail, re.M)
(OUT / 'org.blu-sharky.adrenoprobed.plist').write_bytes(plistlib.dumps(dict(
    Label='org.blu-sharky.adrenoprobed', ProgramArguments=['/private/var/ngpu/bin/adrenoprobed'], RunAtLoad=True,
    KeepAlive=False, ThrottleInterval=30, UserName='root', StandardErrorPath='/dev/null'), fmt=plistlib.FMT_XML))
# launchd may enumerate the directory without our overlay's new names, so the same job is also offered under the name of an existing,
# non-essential RunAtLoad job (the display FDR loader: root, no MachServices): the overlay answers a lookup of that name first.
(OUT / 'com.apple.iomfb_fdr_loader.plist').write_bytes(plistlib.dumps(dict(
    Label='com.apple.iomfb_fdr_loader', ProgramArguments=['/private/var/ngpu/bin/adrenoprobed'], RunAtLoad=True,
    KeepAlive=False, LimitLoadToSessionType='System', ProcessType='Interactive', StandardErrorPath='/dev/null'), fmt=plistlib.FMT_XML))
# Bluetooth is unused on this board; its job is RunAtLoad and was seen to be exec'd on every boot (the exec observer lists bluetoothd).
(OUT / 'com.apple.bluetoothd.plist').write_bytes(plistlib.dumps(dict(
    Label='com.apple.bluetoothd', ProgramArguments=['/private/var/ngpu/bin/adrenoprobed'], RunAtLoad=True, KeepAlive=False,
    ProcessType='Interactive', StandardErrorPath='/dev/null'), fmt=plistlib.FMT_XML))
(OUT / 'trust.json').write_text(json.dumps(dict(cdhash=cdhash, daemon_cdhash=daemon_cdhash, turnip_cdhash=turnip_cdhash, bundle='NativeAdrenoMetal.bundle',
    executable_bytes=exe.stat().st_size, purpose='owner-built plugin and probe daemon; trusted only through an extra owner trust module'),
    indent=2) + '\n')
print('PASS: Adreno plugin bundle and probe daemon built and 4K-signed, CDHashes', cdhash, daemon_cdhash, turnip_cdhash)
