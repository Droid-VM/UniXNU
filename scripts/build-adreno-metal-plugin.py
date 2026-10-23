#!/usr/bin/env python3
"""Build the owner-signed Adreno userspace pieces for ARM64/iOS 17: the Metal plugin bundle (drivers/NativeMetal/NativeAdrenoMetalPlugin.mm)
and the launchd probe daemon with its job plist (NativeAdrenoProbeDaemon.cpp).

Output (ignored, never committed): build/adreno-metal-plugin/NativeAdrenoMetal.bundle with a 4 KiB-page ad-hoc signature, and
trust.json with its CDHash. The bundle is staged into the Data seed by add-gpu-plugin-data.py and trusted through an extra
owner trust module (make-owner-trust-bundle.py); neither changes the original trust modules, AMFI or any Apple signature."""
import json
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/adreno-metal-plugin'
bundle = OUT / 'NativeAdrenoMetal.bundle'
if OUT.exists():
    shutil.rmtree(OUT)
bundle.mkdir(parents=True)
sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()
base = ['xcrun', 'clang', '-target', 'arm64-apple-ios17.0', '-isysroot', sdk, '-O2', '-g', '-Wall', '-Wextra', '-Werror',
        '-fstack-protector-strong']
VULKAN_INCLUDE = ROOT / '.cache/mesa-work/include'
TURNIP = ROOT / 'build/mesa-tu-ios/src/freedreno/vulkan/libvulkan_freedreno.dylib'
if not TURNIP.is_file():
    raise SystemExit('Build Turnip first: python3 scripts/build-turnip-darwin.py --target ios')
subprocess.run(['python3', ROOT / 'scripts/gen-turnip-smoke-shaders.py'], check=True)
# Turnip's first submission, recorded on the host, for the plugin's prefix replay (build/turnip-smoke/TurnipReplay.h).
subprocess.run(['python3', ROOT / 'scripts/gen-turnip-replay.py'], check=True)
subprocess.run(['python3', ROOT / 'scripts/build-native-metal-bridge.py', '--target', 'ios'], check=True)
BRIDGE = ROOT / 'build/native-metal-bridge/ios/libnative_metal_bridge.a'
pm4 = OUT / 'AdrenoPM4.o'
subprocess.run([*base, '-std=c11', '-c', ROOT / 'kits/AdrenoPM4/AdrenoPM4.c', '-o', pm4], check=True)
exe = bundle / 'NativeAdrenoMetal'
# Methods the OS frameworks call on the Metal objects and NativeMetal does not define yet; an unrecognized selector aborts the
# caller. The frontend is compiled from a staged copy with each one placed after its anchor, for as long as NativeMetal lacks it.
# - sharedMemorySize, dedicatedMemorySize (device): Core Image reads both for every Metal context it makes (CIMetalDeviceGetLimits);
#   iconservicesagent aborted on every SF Symbol it drew, and Settings, waiting on its icons, never drew. Unified memory, as on the
#   target hardware: the system's RAM, none of its own.
# - getGPUPriority, getBackgroundGPUPriority (command queue): RenderBox reads both when it makes its queue (RB::Device::command_queue)
#   and only compares them with the priorities it then sets; WidgetRenderer_Default aborted there, so the widgets stayed white. The
#   setters apply nothing (one hardware queue), so none is applied: 0.
# - respondsToSelector: (device) without targetDeviceInfo: the device inherits it from the target's _MTLDevice, which answers it from
#   driver state only the target's IOGPU drivers set (NULL here). Metal's pipeline library loader (MTLPipelineLibraryBuilder::
#   newLibraryWithFile, RenderBox's prebuilt pipelines) asks whether the device has it and then reads through it (a fault at 0x10 in
#   WidgetRenderer_Default); refused, the loader describes the device by its name instead.
ADDED_METHODS = [
    ('- (BOOL)hasUnifiedMemory {return YES;}\n', 'targetDeviceInfo',
     '- (BOOL)respondsToSelector:(SEL)selector {\n'
     ' if(selector==sel_registerName("targetDeviceInfo"))return NO;   // the inherited _MTLDevice one, NULL without IOGPU\n'
     ' return [super respondsToSelector:selector];\n'
     '}\n'),
    ('- (BOOL)hasUnifiedMemory {return YES;}\n', 'sharedMemorySize',
     '- (uint64_t)sharedMemorySize {return NSProcessInfo.processInfo.physicalMemory;}\n'
     '- (uint64_t)dedicatedMemorySize {return 0;}\n'),
    ('- (BOOL)setBackgroundGPUPriority:(NSUInteger)priority {(void)priority;return NO;}\n', 'getGPUPriority',
     '- (NSUInteger)getGPUPriority {return 0;}\n'
     '- (NSUInteger)getBackgroundGPUPriority {return 0;}\n'),
]
# Methods replaced in the staged copy, for as long as NativeMetal has the original:
# - newTextureWithDescriptor:iosurface:plane: (device) refused every surface with planes and said nothing; the render server
#   (backboardd) logged "Failed to allocate MTLTexture from IOSurface" thousands of times, and what it could not texture stayed
#   blank. A plane is now a texture at its offset in the imported surface, with its own row bytes -- over a surface with planes
#   through the staged bridge's nmb_texture_new_on_iosurface, with the facts a two-plane format reads its second plane by -- and
#   each refusal names the surface and why (the first 64).
REPLACED_METHODS = [
    ('- (id<MTLTexture>)newTextureWithDescriptor:(MTLTextureDescriptor *)d iosurface:(IOSurfaceRef)surface plane:(NSUInteger)plane {\n'
     ' if(!surface || !d || plane || IOSurfaceGetPlaneCount(surface))return nil;\n'
     ' void *base=IOSurfaceGetBaseAddress(surface);const size_t page=(size_t)getpagesize(),allocation=IOSurfaceGetAllocSize(surface);\n'
     ' if(!page || (page&(page-1)) || !base || ((uintptr_t)base&(page-1)) || !allocation || allocation>SIZE_MAX-(page-1))return nil;\n'
     ' const size_t bytes=(allocation+page-1)&~(page-1);\n'
     ' NBBuffer *buffer=surfaceBuffer(surface,base,bytes);if(!buffer)return nil;\n'
     ' NBTexture *texture=textureOnBuffer(buffer,d,0,IOSurfaceGetBytesPerRow(surface),true);if(!texture)return nil;\n'
     ' texture.surface=(IOSurfaceRef)CFRetain(surface);return (id<MTLTexture>)texture;\n'
     '}\n',
     '// The staged bridge\'s (scripts/build-native-metal-bridge.py): a texture over a surface with planes, with the surface\'s facts.\n'
     'extern "C" NmbTexture *nmb_texture_new_on_iosurface(NmbBuffer *,const NmbTextureDescriptor *,uint64_t offset,uint64_t bytes_per_row,\n'
     '                                                    uint32_t pixel_format,uint64_t plane,uint64_t width,uint64_t height,\n'
     '                                                    uint64_t plane1_offset,uint64_t plane1_bytes_per_row);\n'
     'static void surfaceRefused(IOSurfaceRef surface,NSUInteger plane,const char *why) {\n'
     ' static std::atomic<int> logged{0};\n'
     ' if(logged.fetch_add(1)>=64)return;\n'
     ' const OSType f=IOSurfaceGetPixelFormat(surface);\n'
     ' NBLOG(@"native-metal: iosurface %#x refused (%s): %c%c%c%c %zux%zu planes %zu plane %lu row %zu alloc %zu base %p",\n'
     '       IOSurfaceGetID(surface),why,(char)(f>>24),(char)(f>>16),(char)(f>>8),(char)f,IOSurfaceGetWidth(surface),\n'
     '       IOSurfaceGetHeight(surface),IOSurfaceGetPlaneCount(surface),(unsigned long)plane,IOSurfaceGetBytesPerRow(surface),\n'
     '       IOSurfaceGetAllocSize(surface),IOSurfaceGetBaseAddress(surface));\n'
     '}\n'
     '- (id<MTLTexture>)newTextureWithDescriptor:(MTLTextureDescriptor *)d iosurface:(IOSurfaceRef)surface plane:(NSUInteger)plane {\n'
     ' if(!surface || !d)return nil;\n'
     ' const size_t planes=IOSurfaceGetPlaneCount(surface);\n'
     ' if(planes?plane>=planes:plane!=0){surfaceRefused(surface,plane,"no such plane");return nil;}\n'
     ' // vm_page_size, not getpagesize(): WebKit\'s GPU process may not read the hw.pagesize sysctl that getpagesize() asks.\n'
     ' uint8_t *base=(uint8_t *)IOSurfaceGetBaseAddress(surface);const size_t page=(size_t)vm_page_size,allocation=IOSurfaceGetAllocSize(surface);\n'
     ' if(!page || (page&(page-1)) || !base || ((uintptr_t)base&(page-1)) || !allocation || allocation>SIZE_MAX-(page-1)) {\n'
     '  surfaceRefused(surface,plane,"no page-aligned mapping");return nil;\n'
     ' }\n'
     ' size_t offset=0,row=IOSurfaceGetBytesPerRow(surface);\n'
     ' if(planes) {\n'
     '  const uint8_t *at=(const uint8_t *)IOSurfaceGetBaseAddressOfPlane(surface,plane);\n'
     '  if(!at || at<base || at>=base+allocation){surfaceRefused(surface,plane,"plane outside the surface");return nil;}\n'
     '  offset=(size_t)(at-base);row=IOSurfaceGetBytesPerRowOfPlane(surface,plane);\n'
     ' }\n'
     ' const size_t bytes=(allocation+page-1)&~(page-1);\n'
     ' NBBuffer *buffer=surfaceBuffer(surface,base,bytes);if(!buffer){surfaceRefused(surface,plane,"import");return nil;}\n'
     ' {  // Each OSType and pixel format a surface is first textured as (the first 32 pairs).\n'
     '  static std::mutex seenLock;static uint64_t seen[32];static unsigned seenCount;\n'
     '  const uint64_t pair=(uint64_t)IOSurfaceGetPixelFormat(surface)<<32|(uint32_t)d.pixelFormat;bool fresh=false;\n'
     '  {std::lock_guard<std::mutex> lock(seenLock);fresh=seenCount<32 && std::find(seen,seen+seenCount,pair)==seen+seenCount;if(fresh)seen[seenCount++]=pair;}\n'
     '  if(fresh) {\n'
     '   const OSType f=IOSurfaceGetPixelFormat(surface);\n'
     '   NBLOG(@"native-metal: iosurface %#x %c%c%c%c %zux%zu planes %zu textured as format %lu",IOSurfaceGetID(surface),(char)(f>>24),\n'
     '         (char)(f>>16),(char)(f>>8),(char)f,IOSurfaceGetWidth(surface),IOSurfaceGetHeight(surface),planes,(unsigned long)d.pixelFormat);\n'
     '  }\n'
     ' }\n'
     ' if(!planes) {\n'
     '  NBTexture *texture=textureOnBuffer(buffer,d,offset,row,true);if(!texture){surfaceRefused(surface,plane,"texture");return nil;}\n'
     '  texture.surface=(IOSurfaceRef)CFRetain(surface);return (id<MTLTexture>)texture;\n'
     ' }\n'
     ' // A surface with planes: the runtime reads a two-plane format\'s second plane from the facts (WebKit\'s \'b3a8\' backing stores).\n'
     ' NmbTextureDescriptor descriptor;if(!textureDescriptor(d,&descriptor))return nil;\n'
     ' const uint8_t *second=planes>1?(const uint8_t *)IOSurfaceGetBaseAddressOfPlane(surface,1):nullptr;\n'
     ' const bool secondInside=second && second>=base && second<base+allocation;\n'
     ' NmbTexture *handle=nmb_texture_new_on_iosurface(buffer.handle,&descriptor,offset,row,IOSurfaceGetPixelFormat(surface),plane,\n'
     '  IOSurfaceGetWidth(surface),IOSurfaceGetHeight(surface),secondInside?(uint64_t)(second-base):0,\n'
     '  secondInside?IOSurfaceGetBytesPerRowOfPlane(surface,1):0);\n'
     ' if(!handle){NBLOG(@"native-metal: texture over surface refused: %@",lastError());surfaceRefused(surface,plane,"texture");return nil;}\n'
     ' NBTexture *texture=[NBTexture new];texture.handle=handle;nmb_texture_describe(handle,&descriptor);texture.descriptor=descriptor;\n'
     ' texture.buffer=buffer;texture.offset=offset;texture.rowBytes=row;\n'
     ' texture.surface=(IOSurfaceRef)CFRetain(surface);return (id<MTLTexture>)texture;\n'
     '}\n'),
    # - supportsFamily: (device), asked by RenderBox: on MTLGPUFamilyApple3 and up RenderBox draws an opaque layer -- a widget, a
    #   poster -- into its own two-plane 4:4:4 format (MTLPixelFormatRGB8_444_2P over an '_RB4' IOSurface, 3 bytes a pixel), which
    #   the bridge cannot make a texture of over an IOSurface (the texture is refused, "RBDrawable: no surface", and the render
    #   server could not composite what was drawn): white widgets, a black wallpaper. RenderBox learns its tier by asking
    #   supportsFamily: from Apple1 up until the first NO (RB::Device's constructor), so it alone is told Apple2 and draws them in
    #   BGRA8; every other caller keeps the bridge's answer.
    ('static void installAnswers(Class cls) {\n',
     'static BOOL (*gFamilyAnswer)(id,SEL,NSInteger);\n'
     'static BOOL familyForCaller(id self,SEL _cmd,NSInteger family) {\n'
     ' if(family>=1003 && family<2000) {   // MTLGPUFamilyApple3 and up\n'
     '  Dl_info caller;\n'
     '  if(dladdr(__builtin_return_address(0),&caller) && caller.dli_fname && strstr(caller.dli_fname,"/RenderBox.framework/"))return NO;\n'
     ' }\n'
     ' return gFamilyAnswer(self,_cmd,family);\n'
     '}\n'
     'static void installAnswers(Class cls) {\n'),
    ('  case NMB_ANSWER_FLAG_ARG: class_addMethod(cls,sel,(IMP)flagArgAnswer,"B24@0:8Q16");break;\n'
     '  }\n'
     ' }\n'
     '}\n',
     '  case NMB_ANSWER_FLAG_ARG: class_addMethod(cls,sel,(IMP)flagArgAnswer,"B24@0:8Q16");break;\n'
     '  }\n'
     ' }\n'
     ' if(Method family=class_getInstanceMethod(cls,@selector(supportsFamily:))) {\n'
     '  gFamilyAnswer=(BOOL (*)(id,SEL,NSInteger))method_getImplementation(family);\n'
     '  method_setImplementation(family,(IMP)familyForCaller);\n'
     ' }\n'
     '}\n'),
]
frontend = (ROOT / 'drivers/NativeMetal/NativeBridgeMetalFrontend.mm').read_text()
for anchor, marker, methods in ADDED_METHODS:
    if marker in frontend:
        continue
    if frontend.count(anchor) != 1:
        raise SystemExit(f'NativeBridgeMetalFrontend.mm no longer has exactly one {anchor.strip()!r}')
    frontend = frontend.replace(anchor, anchor + methods)
for original, replacement in REPLACED_METHODS:
    if replacement in frontend:
        continue
    if frontend.count(original) != 1:
        raise SystemExit(f'NativeBridgeMetalFrontend.mm no longer has exactly one {original.splitlines()[0]!r}')
    frontend = frontend.replace(original, replacement)
# The plugin's own source, staged the same way while NativeMetal lacks the change:
# - its clients list (NativeAdrenoMetal.clients, written from droidvm-gpu-clients) also takes a name ending in '*' for every process
#   whose name begins with the rest: com.apple.WebKit.* gives WebKit's WebContent processes a device as well as its GPU process.
#   CoreGraphics draws a layer's wide-color backing store ('b3a8' RGB10A8, 'w30r' RGB10) only through the GPU; without a device
#   it draws nothing into one, and Safari's pages stayed black. The kernel's grant compares the first 16 characters of a name
#   (com.apple.WebKit), and the command line has no room to name each process.
plugin = (ROOT / 'drivers/NativeMetal/NativeAdrenoMetalPlugin.mm').read_text()
PLUGIN_REPLACED = [
    ('// Whether NativeAdrenoMetal.clients names this process (names separated by blanks, commas or new lines; "*" names every process).\n',
     '// Whether NativeAdrenoMetal.clients names this process (names separated by blanks, commas or new lines; "*" names every process,\n'
     '// a name ending in "*" every process whose name begins with the rest).\n'),
    ('        if (!strcmp(name, "*") || !strcmp(name, self)) return true;\n',
     '        if (!strcmp(name, "*") || !strcmp(name, self)) return true;\n'
     '        else if (const size_t length = strlen(name); length > 1 && name[length - 1] == \'*\' && !strncmp(name, self, length - 1)) return true;\n'),
]
for original, replacement in PLUGIN_REPLACED:
    if replacement in plugin:
        continue
    if plugin.count(original) != 1:
        raise SystemExit(f'NativeAdrenoMetalPlugin.mm no longer has exactly one {original.strip()!r}')
    plugin = plugin.replace(original, replacement)
# - a client that may write in neither shader-cache directory -- WebKit's GPU process, whose sandbox refuses both -- got no device and
#   said nothing, so CoreGraphics drew the page's wide-color layers nowhere: it now opens one whose cache is never on disk (the
#   runtime's METAL2VULKAN_SHADER_CACHE_OPTIONAL, patches/metal2vulkan/0006).
PLUGIN_REPLACED.append((
    '            device = nil;\n'
    '        }\n'
    '    }\n'
    '    return device;\n'
    '}\n',
    '            device = nil;\n'
    '        }\n'
    '    }\n'
    '    if (!device) {\n'
    '        setenv("METAL2VULKAN_SHADER_CACHE_OPTIONAL", "1", 1);\n'
    '        const char *path = candidates[0][0] ? candidates[0] : candidates[1][0] ? candidates[1] : "/var/mobile/tmp/m2v";\n'
    '        @try {\n'
    '            device = native_bridge_metal_open(turnip, path, registryID, nullptr);\n'
    '        } @catch (NSException *exception) {\n'
    '            device = nil;\n'
    '        }\n'
    '    }\n'
    '    return device;\n'
    '}\n'))
for original, replacement in PLUGIN_REPLACED[2:]:
    if replacement in plugin:
        continue
    if plugin.count(original) != 1:
        raise SystemExit(f'NativeAdrenoMetalPlugin.mm no longer has exactly one {original.strip()!r}')
    plugin = plugin.replace(original, replacement)
staged_plugin = OUT / 'NativeAdrenoMetalPlugin.mm'
staged_plugin.write_text(plugin)
staged_frontend = OUT / 'NativeBridgeMetalFrontend.mm'
staged_frontend.write_text(frontend)
subprocess.run([*(['xcrun', 'clang++'] + base[2:]), '-std=c++17', '-fobjc-arc', '-Wno-deprecated-declarations', '-bundle',
                '-I', ROOT / 'drivers/NativeAdreno', '-I', ROOT / 'kits/AdrenoPM4', '-I', VULKAN_INCLUDE, '-I', ROOT / 'build/turnip-smoke',
                '-I', ROOT / 'drivers/NativeMetal',
                '-x', 'objective-c++', staged_plugin,
                staged_frontend, '-x', 'none', pm4, BRIDGE,
                '-Wl,-dead_strip', '-lobjc', '-framework', 'Foundation', '-framework', 'Metal', '-framework', 'IOSurface', '-o', exe], check=True)
subprocess.run(['strip', '-x', exe], check=True)
# The linker writes the debug symbols of the Rust bridge next to the executable; keep them beside the bundle, not in the image.
symbols = OUT / 'NativeAdrenoMetal.dSYM'
shutil.rmtree(symbols, ignore_errors=True)
if (bundle / 'NativeAdrenoMetal.dSYM').exists():
    shutil.move(bundle / 'NativeAdrenoMetal.dSYM', symbols)
# Turnip (the real Mesa Vulkan driver with the native Darwin kernel backend) travels inside the plugin bundle, signed on its own with 4 KiB pages so its
# CDHash can be trusted like the plugin's. It is signed before the bundle so the bundle's resource seal covers the final bytes.
turnip_lib = bundle / 'libvulkan_freedreno.dylib'
shutil.copyfile(TURNIP, turnip_lib)
subprocess.run(['strip', '-x', turnip_lib], check=True)
subprocess.run(['codesign', '--force', '--sign', '-', '--pagesize', '4096', '--identifier', 'org.blu-sharky.turnip', turnip_lib], check=True)
turnip_detail = subprocess.run(['codesign', '--display', '--verbose=4', turnip_lib], capture_output=True, text=True, check=True).stderr
turnip_cdhash = re.search(r'^CDHash=([a-f0-9]{40})$', turnip_detail, re.M)[1]
assert re.search(r'^Page size=4096$', turnip_detail, re.M)
(bundle / 'Info.plist').write_bytes(plistlib.dumps(dict(
    CFBundleIdentifier='org.blu-sharky.NativeAdrenoMetal', CFBundleExecutable='NativeAdrenoMetal', CFBundlePackageType='BNDL',
    CFBundleVersion='1', CFBundleShortVersionString='1', NSPrincipalClass='NativeAdrenoMetalDevice',
    MinimumOSVersion='17.0'), fmt=plistlib.FMT_XML))
subprocess.run(['codesign', '--force', '--sign', '-', '--pagesize', '4096', '--identifier', 'org.blu-sharky.NativeAdrenoMetal',
                bundle], check=True)
subprocess.run(['codesign', '--verify', '--strict', bundle], check=True)
detail = subprocess.run(['codesign', '--display', '--verbose=4', bundle], capture_output=True, text=True, check=True).stderr
cdhash = re.search(r'^CDHash=([a-f0-9]{40})$', detail, re.M)[1]
assert re.search(r'^Page size=4096$', detail, re.M), 'the signature must use 4 KiB pages'
header = exe.read_bytes()[:16]
magic, cputype, subtype = int.from_bytes(header[0:4], 'little'), int.from_bytes(header[4:8], 'little'), int.from_bytes(header[8:12], 'little')
assert (magic, cputype, subtype & 0xffffff) == (0xfeedfacf, 0x100000c, 0), 'expected an ordinary ARM64 (subtype 0) Mach-O'
# The probe daemon (launchd starts it as an owner job). The original platform sandbox policy refuses IOServiceOpen for any user-client class
# a process has not been granted (the same refusal an unentitled consumer got in the offline owner tests), so it carries exactly the grant that
# worked there: com.apple.security.iokit-user-client-class naming the one class it opens, plus no-container.
entitlements = OUT / 'adrenoprobed-entitlements.plist'
entitlements.write_bytes(plistlib.dumps({'com.apple.private.security.no-container': True,
                                         'com.apple.security.iokit-user-client-class': ['AGXDeviceUserClient']}))
daemon = OUT / 'adrenoprobed'
subprocess.run(['xcrun', 'clang++', *base[2:], '-std=c++17', '-I', ROOT / 'drivers/NativeAdreno', '-I', ROOT / 'kits/AdrenoPM4', '-I', VULKAN_INCLUDE,
                '-I', ROOT / 'build/turnip-smoke',
                ROOT / 'drivers/NativeMetal/NativeAdrenoProbeDaemon.cpp', pm4, '-o', daemon], check=True)
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
