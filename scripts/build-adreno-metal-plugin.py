#!/usr/bin/env python3
"""Build the owner-signed Adreno userspace pieces for ARM64/iOS 17: the Metal plugin bundle (drivers/AdrenoMetal/NativeAdrenoMetalPlugin.mm)
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
subprocess.run(['python3', ROOT / 'scripts/build-adreno-metal-bridge.py', '--target', 'ios'], check=True)
BRIDGE = ROOT / 'build/adreno-metal-bridge/ios/libadreno_metal_bridge.a'
pm4 = OUT / 'AdrenoPM4.o'
subprocess.run([*base, '-std=c11', '-c', ROOT / 'kits/AdrenoPM4/AdrenoPM4.c', '-o', pm4], check=True)
exe = bundle / 'NativeAdrenoMetal'
# What the OS frameworks need from the Metal objects and NativeMetal does not do yet; the frontend and the plugin are compiled from
# staged copies with each change placed at its anchor, for as long as NativeMetal lacks it.
# - respondsToSelector: (device) without targetDeviceInfo: the device inherits it from the target's _MTLDevice, which answers it from
#   driver state only the target's IOGPU drivers set (NULL here). Metal's pipeline library loader (MTLPipelineLibraryBuilder::
#   newLibraryWithFile, RenderBox's prebuilt pipelines) asks whether the device has it and then reads through it (a fault at 0x10 in
#   WidgetRenderer_Default); refused, the loader describes the device by its name instead.
# - setDepthClipMode: (render encoder): VectorKit sets it on every encoder (Maps aborted on the unrecognized selector). The runtime has
#   no depth clamp, so Clip, Metal's default, is what it draws; Clamp is drawn as Clip and said once.
ADDED_METHODS = [
    ('- (void)setLineWidth:(float)width {[self check:nmb_render_set_line_width(self.handle,width) what:"setLineWidth"];}\n',
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
frontend = (ROOT / 'drivers/NativeMetal/NativeBridgeMetalFrontend.mm').read_text()
for anchor, marker, methods in ADDED_METHODS:
    if marker in frontend:
        continue
    if frontend.count(anchor) != 1:
        raise SystemExit(f'NativeBridgeMetalFrontend.mm no longer has exactly one {anchor.strip()!r}')
    frontend = frontend.replace(anchor, anchor + methods)
# - texture layouts (newTextureLayoutWithDescriptor:isHeapOrBufferBacked:) and textures over memory in one (newTextureWithBytesNoCopy:
#   length:descriptor:deallocator:), which NativeMetal refuses because Apple's layout is not known: DroidVM answers a layout of its own
#   for the block-compressed formats (ASTC, ETC2/EAC, BC), each level's blocks row after row, each slice's levels one after another.
#   ImageIO keeps an ASTC image (an ATX file: PosterBoard's wallpaper snapshots) in the device's layout and reads one back only under
#   the same layout's watermark, and CoreAnimation lays a compressed image's texture memory out in it for the render server, which
#   makes the texture over that memory; with neither, every wallpaper preview in the gallery was blank. The texture over memory is a
#   texture of its own, the memory copied in level by level; the memory's deallocator runs when the texture goes. Other formats keep
#   no layout (and no texture over memory), as before.
FRONTEND_CLASSES = """// DroidVM's texture layout (newTextureLayoutWithDescriptor:isHeapOrBufferBacked:): a block-compressed format's blocks row after row,
// each level after the one before, each slice's chain of levels after the slice before. Its watermark names it, so data ImageIO laid
// out for another GPU's layout is refused rather than read as this one.
typedef struct {uint64_t words[2];} DVMLayoutWatermark;
static bool dvmBlockFormat(MTLPixelFormat format, NSUInteger &bytes, NSUInteger &width, NSUInteger &height) {
 switch(format){
#define DVM_ASTC(W,H) case MTLPixelFormatASTC_##W##x##H##_sRGB: case MTLPixelFormatASTC_##W##x##H##_LDR: case MTLPixelFormatASTC_##W##x##H##_HDR: \\
  bytes=16;width=W;height=H;return true;
 DVM_ASTC(4,4) DVM_ASTC(5,4) DVM_ASTC(5,5) DVM_ASTC(6,5) DVM_ASTC(6,6) DVM_ASTC(8,5) DVM_ASTC(8,6) DVM_ASTC(8,8) DVM_ASTC(10,5) DVM_ASTC(10,6)
 DVM_ASTC(10,8) DVM_ASTC(10,10) DVM_ASTC(12,10) DVM_ASTC(12,12)
#undef DVM_ASTC
 case MTLPixelFormatETC2_RGB8: case MTLPixelFormatETC2_RGB8_sRGB: case MTLPixelFormatETC2_RGB8A1: case MTLPixelFormatETC2_RGB8A1_sRGB:
 case MTLPixelFormatEAC_R11Unorm: case MTLPixelFormatEAC_R11Snorm:
 case MTLPixelFormatBC1_RGBA: case MTLPixelFormatBC1_RGBA_sRGB: case MTLPixelFormatBC4_RUnorm: case MTLPixelFormatBC4_RSnorm:
  bytes=8;width=height=4;return true;
 case MTLPixelFormatEAC_RG11Unorm: case MTLPixelFormatEAC_RG11Snorm: case MTLPixelFormatEAC_RGBA8: case MTLPixelFormatEAC_RGBA8_sRGB:
 case MTLPixelFormatBC2_RGBA: case MTLPixelFormatBC2_RGBA_sRGB: case MTLPixelFormatBC3_RGBA: case MTLPixelFormatBC3_RGBA_sRGB:
 case MTLPixelFormatBC5_RGUnorm: case MTLPixelFormatBC5_RGSnorm: case MTLPixelFormatBC6H_RGBFloat: case MTLPixelFormatBC6H_RGBUfloat:
 case MTLPixelFormatBC7_RGBAUnorm: case MTLPixelFormatBC7_RGBAUnorm_sRGB:
  bytes=16;width=height=4;return true;
 default:return false;
 }
}
@interface DVMLinearTextureLayout : NSObject
+ (instancetype)layoutWithDescriptor:(MTLTextureDescriptor *)d;
- (NSUInteger)size;
- (void)upload:(const uint8_t *)memory into:(id<MTLTexture>)texture;
@end
@implementation DVMLinearTextureLayout {
 NSUInteger _block,_bw,_bh,_width,_height,_depth,_levels,_slices,_chain;
}
+ (instancetype)layoutWithDescriptor:(MTLTextureDescriptor *)d {
 NSUInteger bytes=0,width=0,height=0;
 if(!d||!dvmBlockFormat(d.pixelFormat,bytes,width,height))return nil;
 DVMLinearTextureLayout *layout=[self new];
 layout->_block=bytes;layout->_bw=width;layout->_bh=height;
 layout->_width=std::max<NSUInteger>(d.width,1);layout->_height=std::max<NSUInteger>(d.height,1);
 layout->_depth=d.textureType==MTLTextureType3D?std::max<NSUInteger>(d.depth,1):1;
 layout->_levels=std::max<NSUInteger>(d.mipmapLevelCount,1);
 const bool cube=d.textureType==MTLTextureTypeCube||d.textureType==MTLTextureTypeCubeArray;
 layout->_slices=std::max<NSUInteger>(d.arrayLength,1)*(cube?6:1);
 for(NSUInteger level=0;level<layout->_levels;level++)layout->_chain+=[layout imageBytes:level]*[layout levelDepth:level];
 static std::atomic<bool> told{false};
 if(!told.exchange(true))NBLOG(@"native-metal: texture layouts are DroidVM's linear ones (block-compressed formats; format %lu first)",(unsigned long)d.pixelFormat);
 return layout;
}
- (NSUInteger)levelDepth:(NSUInteger)level {return std::max<NSUInteger>(_depth>>level,1);}
- (NSUInteger)blocksAcross:(NSUInteger)level {return (std::max<NSUInteger>(_width>>level,1)+_bw-1)/_bw;}
- (NSUInteger)blocksDown:(NSUInteger)level {return (std::max<NSUInteger>(_height>>level,1)+_bh-1)/_bh;}
- (NSUInteger)rowBytes:(NSUInteger)level {return [self blocksAcross:level]*_block;}
- (NSUInteger)imageBytes:(NSUInteger)level {return [self rowBytes:level]*[self blocksDown:level];}
- (NSUInteger)offsetOfSlice:(NSUInteger)slice level:(NSUInteger)level {
 NSUInteger at=slice*_chain;
 for(NSUInteger l=0;l<level;l++)at+=[self imageBytes:l]*[self levelDepth:l];
 return at;
}
- (NSUInteger)size {return _chain*_slices;}
- (NSUInteger)alignment {return 16384;}
- (MTLSize)copyGranularity {return MTLSizeMake(_bw,_bh,1);}
- (DVMLayoutWatermark)watermark {
 static const char mark[16]={'D','r','o','i','d','V','M',' ','l','i','n','e','a','r',' ','1'};
 DVMLayoutWatermark watermark;
 memcpy(&watermark,mark,sizeof watermark);
 return watermark;
}
- (void)initializeTextureMemory:(void *)memory {(void)memory;}
- (void)finalizeTextureMemory:(void *)memory {(void)memory;}
// The region's blocks between the texture memory and linear bytes (rows of blockBytes, images of rows), clipped to the level.
- (void)move:(MTLRegion)region slice:(NSUInteger)slice level:(NSUInteger)level memory:(uint8_t *)memory linear:(uint8_t *)linear
    rowBytes:(NSUInteger)rowBytes imageBytes:(NSUInteger)imageBytes toMemory:(bool)toMemory {
 if(!memory||!linear||slice>=_slices||level>=_levels)return;
 const NSUInteger across=[self blocksAcross:level],down=[self blocksDown:level],depth=[self levelDepth:level];
 const NSUInteger bx=region.origin.x/_bw,by=region.origin.y/_bh;
 if(bx>=across||by>=down||region.origin.z>=depth)return;
 const NSUInteger bw=std::min((region.origin.x+region.size.width+_bw-1)/_bw,across)-bx;
 const NSUInteger bh=std::min((region.origin.y+region.size.height+_bh-1)/_bh,down)-by;
 const NSUInteger bd=std::min<NSUInteger>(region.origin.z+std::max<NSUInteger>(region.size.depth,1),depth)-region.origin.z;
 const NSUInteger row=[self rowBytes:level],image=[self imageBytes:level];
 uint8_t *base=memory+[self offsetOfSlice:slice level:level];
 for(NSUInteger z=0;z<bd;z++)
  for(NSUInteger y=0;y<bh;y++){
   uint8_t *texel=base+(region.origin.z+z)*image+(by+y)*row+bx*_block;
   uint8_t *line=linear+z*imageBytes+y*rowBytes;
   if(toMemory)memcpy(texel,line,bw*_block);else memcpy(line,texel,bw*_block);
  }
}
- (void)copyFromLinearBytes:(const void *)bytes linearOffset:(NSUInteger)offset linearBytesPerRow:(NSUInteger)rowBytes
       linearBytesPerImage:(NSUInteger)imageBytes toTextureMemory:(void *)memory textureSlice:(NSUInteger)slice
              textureLevel:(NSUInteger)level textureRegion:(MTLRegion)region {
 if(!bytes)return;
 [self move:region slice:slice level:level memory:(uint8_t *)memory linear:(uint8_t *)bytes+offset rowBytes:rowBytes imageBytes:imageBytes toMemory:true];
}
- (void)copyFromTextureMemory:(const void *)memory textureSlice:(NSUInteger)slice textureLevel:(NSUInteger)level textureRegion:(MTLRegion)region
                toLinearBytes:(void *)bytes linearOffset:(NSUInteger)offset linearBytesPerRow:(NSUInteger)rowBytes
          linearBytesPerImage:(NSUInteger)imageBytes {
 if(!bytes)return;
 [self move:region slice:slice level:level memory:(uint8_t *)memory linear:(uint8_t *)bytes+offset rowBytes:rowBytes imageBytes:imageBytes toMemory:false];
}
// Texture memory in this layout copied into a texture, every slice's every level.
- (void)upload:(const uint8_t *)memory into:(id<MTLTexture>)texture {
 for(NSUInteger slice=0;slice<_slices;slice++)
  for(NSUInteger level=0;level<_levels;level++){
   const MTLRegion region=MTLRegionMake3D(0,0,0,std::max<NSUInteger>(_width>>level,1),std::max<NSUInteger>(_height>>level,1),[self levelDepth:level]);
   [texture replaceRegion:region mipmapLevel:level slice:slice withBytes:memory+[self offsetOfSlice:slice level:level]
              bytesPerRow:[self rowBytes:level] bytesPerImage:[self imageBytes:level]];
  }
}
@end
// The deallocator of the memory a texture was made over, run when the texture goes.
@interface DVMMemoryRelease : NSObject
- (instancetype)initWithBytes:(void *)bytes length:(NSUInteger)length deallocator:(void (^)(void *,NSUInteger))deallocator;
@end
@implementation DVMMemoryRelease {
 void *_bytes;NSUInteger _length;void (^_deallocator)(void *,NSUInteger);
}
- (instancetype)initWithBytes:(void *)bytes length:(NSUInteger)length deallocator:(void (^)(void *,NSUInteger))deallocator {
 if((self=[super init])){_bytes=bytes;_length=length;_deallocator=[deallocator copy];}
 return self;
}
- (void)dealloc {if(_deallocator)_deallocator(_bytes,_length);}
@end
static char dvmMemoryReleaseKey;

"""
FRONTEND_REPLACED = [
    ('@interface NBHeap : NBObject\n', FRONTEND_CLASSES + '@interface NBHeap : NBObject\n'),
    ("""// Not made here: a texture over raw memory in Apple's layout, and Apple's texture layout object. Both are refused, with the reason, rather than
// answered with a guessed shape.
- (id<MTLTexture>)newTextureWithBytesNoCopy:(void *)bytes length:(NSUInteger)length descriptor:(MTLTextureDescriptor *)d deallocator:(void (^)(void *,NSUInteger))deallocator {
 (void)bytes;(void)length;(void)d;(void)deallocator;
 NBLOG(@"native-metal: newTextureWithBytesNoCopy:length:descriptor:deallocator: is not implemented (Apple's texture layout of raw memory is not known)");
 return nil;
}
- (id)newTextureLayoutWithDescriptor:(MTLTextureDescriptor *)d isHeapOrBufferBacked:(BOOL)backed {
 (void)d;(void)backed;
 NBLOG(@"native-metal: newTextureLayoutWithDescriptor:isHeapOrBufferBacked: is not implemented (Apple's layout object is not known)");
 return nil;
}
""",
     """// Apple's texture layout is not known: a block-compressed format's layout and a texture over memory in it are DroidVM's own
// (DVMLinearTextureLayout); any other format is refused, with the reason, rather than answered with a guessed shape.
- (id<MTLTexture>)newTextureWithBytesNoCopy:(void *)bytes length:(NSUInteger)length descriptor:(MTLTextureDescriptor *)d deallocator:(void (^)(void *,NSUInteger))deallocator {
 DVMLinearTextureLayout *layout=[DVMLinearTextureLayout layoutWithDescriptor:d];
 if(!layout||!bytes||length<layout.size){
  NBLOG(@"native-metal: newTextureWithBytesNoCopy:length:descriptor:deallocator: is made only over DroidVM's layout of a block-compressed format (format %lu, %lu bytes)",
        (unsigned long)d.pixelFormat,(unsigned long)length);
  return nil;
 }
 MTLTextureDescriptor *shared=[d copy];
 shared.storageMode=MTLStorageModeShared;
 id<MTLTexture> texture=[(id<MTLDevice>)self newTextureWithDescriptor:shared];
 if(!texture)return nil;
 [layout upload:(const uint8_t *)bytes into:texture];
 if(deallocator)objc_setAssociatedObject(texture,&dvmMemoryReleaseKey,[[DVMMemoryRelease alloc] initWithBytes:bytes length:length deallocator:deallocator],
                                         OBJC_ASSOCIATION_RETAIN_NONATOMIC);
 return texture;
}
- (id)newTextureLayoutWithDescriptor:(MTLTextureDescriptor *)d isHeapOrBufferBacked:(BOOL)backed {
 (void)backed;
 DVMLinearTextureLayout *layout=[DVMLinearTextureLayout layoutWithDescriptor:d];
 if(!layout)NBLOG(@"native-metal: newTextureLayoutWithDescriptor:isHeapOrBufferBacked: has no layout for format %lu (Apple's is not known)",(unsigned long)d.pixelFormat);
 return layout;
}
"""),
]
for original, replacement in FRONTEND_REPLACED:
    if replacement in frontend:
        continue
    if frontend.count(original) != 1:
        raise SystemExit(f'NativeBridgeMetalFrontend.mm no longer has exactly one {original.strip()[:80]!r}')
    frontend = frontend.replace(original, replacement)
# - the plugin's clients list (NativeAdrenoMetal.clients, written from droidvm-gpu-clients) also takes a name ending in '*' for every
#   process whose name begins with the rest: com.apple.WebKit.* gives WebKit's WebContent processes a device as well as its GPU process.
#   CoreGraphics draws a layer's wide-color backing store ('b3a8' RGB10A8, 'w30r' RGB10) only through the GPU; without a device
#   it draws nothing into one, and Safari's pages stayed black. The command line has no room to name each process. "*X*" names
#   every process whose name holds X: *Poster* gives PosterBoard and each wallpaper's poster extension (AegirPoster, the Kaleidoscope
#   and Extragalactic watch faces) a device; without one they crashed rendering the gallery's previews. The kernel's grant (patch
#   zzw-native-adreno-sandbox-grant) matches the same three forms.
# - a client that may write in neither shader-cache directory -- WebKit's GPU process, whose sandbox refuses both -- got no device
#   ("no-cachedir"), so CoreGraphics drew the page's wide-color layers nowhere: it now opens one whose cache is never on disk (the
#   runtime's METAL2VULKAN_SHADER_CACHE_OPTIONAL, patches/metal2vulkan/0006).
# - Turnip renders as it does by default, in GMEM with binning: NativeMetal makes every Metal process render in system memory without
#   concurrent binning (TU_DEBUG=sysmem,nocb) because Turnip's BV and BR threads stalled each other under sheng's own kernel driver,
#   which DroidVM does not have (the host's KGSL schedules the GPU); here system-memory rendering left stale runs and garbled labels
#   in composited layers once several apps had run, and GMEM did not. The first line of /private/var/ngpu/tu_debug, read by each
#   process that may read it, names other flags.
PLUGIN_REPLACED = [
    ('        setenv("TU_DEBUG", "sysmem,nocb", 0);\n',
     '        if (FILE *flags = fopen("/private/var/ngpu/tu_debug", "r")) {\n'
     '            char line[128] = {};\n'
     '            if (fgets(line, sizeof line, flags)) {line[strcspn(line, "\\n")] = 0; setenv("TU_DEBUG", line, 1);}\n'
     '            fclose(flags);\n'
     '        }\n'
     '        setenv("TU_DEBUG", "", 0);\n'),
    ('// Whether NativeAdrenoMetal.clients names this process (names separated by blanks, commas or new lines; "*" names every process).\n',
     '// Whether NativeAdrenoMetal.clients names this process (names separated by blanks, commas or new lines; "*" names every process,\n'
     '// a name ending in "*" every process whose name begins with the rest, "*X*" every process whose name holds X).\n'),
    ('        if (!strcmp(name, "*") || !strcmp(name, self)) return true;\n',
     '        if (!strcmp(name, "*") || !strcmp(name, self)) return true;\n'
     '        else if (const size_t length = strlen(name); length > 1 && name[length - 1] == \'*\') {\n'
     '            name[length - 1] = 0;\n'
     '            if (name[0] == \'*\' ? strstr(self, name + 1) != nullptr : !strncmp(name, self, length - 1)) return true;\n'
     '        }\n'),
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
plugin = (ROOT / 'drivers/NativeMetal/NativeAdrenoMetalPlugin.mm').read_text()
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
                '-Wl,-dead_strip', *prime_section, '-lobjc', '-framework', 'Foundation', '-framework', 'Metal', '-framework', 'IOSurface', '-o', exe],
               check=True)
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
