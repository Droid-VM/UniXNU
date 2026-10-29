# DarwinBoot

Start with the [adaptation procedure](/porting/platform) for board inputs, source changes, build integration and verification order.

UEFI bootloader. Installs the loader protocol and hands the kernel, ADT, trust cache and ramdisk to XNU.

| Repository | Maintainer | Scope |
| --- | --- | --- |
| [DarwinBoot](https://github.com/UniXNU/DarwinBoot) | [UniXNU](https://github.com/UniXNU) | UEFI bootloader. Installs the loader protocol and hands the kernel, ADT, trust cache and ramdisk to XNU. |

## State

Not a driver. Used by every XNU boot path.

The loader accepts arm64 and arm64e `MH_EXECUTE` kernels. CPU subtype
capability bits are retained. XNU initializes pointer authentication and
applies its chained fixups. `MH_FILESET` input is not supported yet.

## Build

Include the DarwinBoot INF in the target firmware DSC/FDF, then run `./unixnu build firmware` from UniXNU. Follow [firmware integration](/porting/uefi-firmware#required-integration) and verify the loader protocol before selecting an XNU entry.

The source storage runtime also accepts `kernel.macho.vroot`, a separate signed
policy bundle passed through `/chosen/memory-map/RootPolicies`. Firmware checks
the envelope; the kernel verifies each signature and hash table against its
compiled owner anchor. The new sidecar path remains untested.
