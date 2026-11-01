#!/usr/bin/env python3
"""Build a symbol-bearing RELEASE comparison kernel in an isolated output tree."""
import json
import os
import shutil
import hashlib
import argparse
from pathlib import Path
import subprocess

repo = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--lto', action='store_true', help='enable the RELEASE default link-time optimization')
options = parser.parse_args()
mode = 'lto' if options.lto else 'snapshot'
root = repo / 'out/sheng/ipados-26.5.2-23F84/sheng_ipados26/tree'
output = root / 'build/xnu/fileset-reference'
output.mkdir(parents=True, exist_ok=True)
source = output / 'source'
if not source.exists():
    shutil.copytree(root/'.cache/xnu-work', source, symlinks=True,
                    ignore=shutil.ignore_patterns('.git'))
    # The shared tree currently has this observation hook at file scope. The
    # reference build disables observation hooks; repair only its private copy.
    sleh = source / 'osfmk/arm64/sleh.c'
    text = sleh.read_text()
    misplaced = '\n\tboot_progress_user_breakpoint(state, esr);\n#if __has_feature(ptrauth_calls)'
    if misplaced in text:
        sleh.write_text(text.replace(misplaced, '\n#if __has_feature(ptrauth_calls)', 1))
    manifest = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in source.rglob('*') if p.is_file()}
    (output/'source-sha256.json').write_text(json.dumps(manifest,indent=2)+'\n')
sdk = root / '.cache/fakeroot'
archives = [root/'build/corecrypto-2023/libcorecrypto_kernel_noasm-namespaced.a']
forced = [root/f'build/{folder}/{name}' for folder,name in (
    ('libpthread-kernel','libpthread_kernel.a'),('armdt-platform','libArmDTPlatform.a'),('armcpu','libArmCpu.a'))]
assert all(p.is_file() for p in archives+forced)
env = dict(os.environ, KERNEL_SDKROOT=str(sdk),
           XNU_AVAILABILITY_PL=str(sdk/'usr/local/libexec/availability.pl'))
flags = '-Wno-reserved-identifier -Wno-pre-c11-compat -Wno-missing-designated-field-initializers -Wno-suggest-override -Wno-unnecessary-virtual-specifier -Wno-suggest-destructor-override -Wno-ms-bitfield-padding -DXNU_NATIVE_PAN=1 -DXNU_SOURCE_CORECRYPTO14=1 -DXNU_BOOT_PROGRESS=0'
args = ['make','-C',str(source),'build','SDKROOT=iphoneos',
    'ARCH_CONFIGS=ARM64','MACHINE_CONFIGS=BCM2837','KERNEL_CONFIGS=RELEASE',
    'ARCH_STRING_FOR_CURRENT_MACHINE_CONFIG=arm64e','LARGE_MEMORY_DEFINE=-UARM_LARGE_MEMORY',
    'ARM64_LINK_ADDR=0xfffffff007004000','ARM64_PLKSEG_ADDR=0xfffffff004004000',
    'DEPLOYMENT_TARGET_FLAGS=-miphoneos-version-min=26.0 -DXNU_TARGET_OS_IOS',
    'DEPLOYMENT_LINKER_FLAGS=-Wl,-ios_version_min,26.0',
    'RC_DARWIN_KERNEL_VERSION=25.5.0','RC_ProjectSourceVersion=12377.121.6',
    f'BUILD_LTO={int(options.lto)}','BOUND_CHECKS=0','BUILD_WERROR=0','MAKEJOBS=-j8',
    'LDFLAGS_KERNEL_LTO=-miphoneos-version-min=26.0 -Wl,-mllvm,-inline-threshold=100',
    f'CFLAGS_EXTRA={flags}',
    'copyio.o_CFLAGS_ADD=-march=armv8.1-a','pcb.o_CFLAGS_ADD=-march=armv8.1-a',
    'LD_KERNEL_ARCHIVES='+' '.join([f'-L{sdk}/usr/local/lib/kernel','-lfirehose_kernel',
                                    *map(str,archives),*(f'-Wl,-force_load,{p}' for p in forced)]),
    'XNU_EXTERNAL_KERNEL_ARCHIVES='+' '.join(map(str,archives+forced)),
    f'OBJROOT={output}/{mode}-obj',f'SYMROOT={output}/{mode}-sym',f'DSTROOT={output}/{mode}-dst']
(output/f'{mode}-command.json').write_text(json.dumps(args,indent=2)+'\n')
with (output/f'{mode}-build.log').open('w') as log:
    result = subprocess.run(args,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
print('RELEASE reference exit',result.returncode,'log',output/f'{mode}-build.log',flush=True)
raise SystemExit(result.returncode)
