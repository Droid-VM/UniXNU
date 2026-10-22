# Integrity and boot policy

UniXNU keeps the original Apple security components and supplies the inputs they check. The source kernel adds the platform code those components need, and the boot payload supplies the trust caches and device tree properties that describe the device.

## Inputs

| Input | Supplier |
| --- | --- |
| Trust cache bundle | `--trust-cache FILE` |
| Trust cache pins | `[payload] trust_cache_sha256` |
| Platform seed | `--unique-chip-id HEX` |
| Device tree identity | `[payload.adt] ipad_identity` and the board script |
| PMGR integration | `XNU_SHENG_PMGR` on boards that need it |
| Root policy | `XNU_VERIFIED_ROOT_PROBE`, `XNU_OWNER_ROOT_AUDIT`, `XNU_OWNER_ROOT_TRUST` |

## Trust cache

DarwinBoot validates the structure of the trust cache bundle and copies it into the kernel window. The ADT `/chosen/memory-map/TrustCache` placeholder receives the address and size.

The kernel patch `native-trust-cache-runtime` initializes the runtime trust cache path on `BCM2837`. The original AppleImage4 and AMFI components validate code against the cache.

The payload step accepts a trust cache bundle only when its SHA-256 is listed in `[payload] trust_cache_sha256`.

## Code signing at 4 KiB

The original CodeDirectory uses a 16 KiB hash page. A 4 KiB kernel validates 4 KiB pages. The patch `native-4k-signature-hash-cache`:

1. Reads every original 16 KiB block through `vn_rdwr`.
2. Checks the block against the original CodeDirectory hash.
3. Derives one hash per 4 KiB page from the verified block.
4. Stores the derived hashes in read-only kernel memory.
5. Serves `cs_validate_page` from the derived table.

A block is published only after the original 16 KiB hash matches. A failed block leaves the blob without a derived table, and the page validator returns `CS_VALIDATE_TAINTED`.

## AMFI policy

The ADT property `/chosen/amfi-only-platform-code` keeps AMFI in its platform code policy. The `early_security_init` entry in `[kernel.prelink]` runs the original `AppleMobileFileIntegrity` initializer at the selected address.

The kernel patches `offline-owner-root-*` add the owner root checks used by the first boot path:

| Patch | Function |
| --- | --- |
| `offline-owner-root-binding-audit` | Checks the root binding against the boot manifest |
| `offline-owner-root-crypto-probe` | Checks the owner crypto provider |
| `offline-owner-boot-manifest` | Records the boot manifest hash |
| `offline-owner-cache-carrier` | Selects the code signing cache carrier |
| `offline-owner-systemos-mount` | Mounts the SystemOS cryptex |
| `offline-owner-z-air-init` | Initializes the Z air path for the owner root |

## GPU client grant

NativeAdreno publishes `NativeSandboxGrantProcess` on its accelerator service. The patch `zzw-native-adreno-sandbox-grant` compares the opening process name with that property. The named process opens the user client past `mac_iokit_check_open_service` and `mac_iokit_check_open`. The property is one name (the render server) or an array of names: a board that gives other processes a native Metal device of their own lists them beside the render server, because their own profiles refuse the driver's user client too. A name matches the first 16 characters of the process name.

## Verified root

`kits/VerifiedRoot` supplies a signed policy bundle. The verified block driver checks every read against the policy. The policy can be selected per capacity with `--verified-root-bundle`.

The kit provides:

| Tool | Function |
| --- | --- |
| `verified_root.c` | Policy parsing and read authentication |
| `xnu_crypto.c` | XNU crypto adapter |
| `manifest.py` | Policy bundle builder |
| `verify_image.py` | Image verification |

## Storage integrity

`NativeVirtioBlock` and `NativeRamBlock` share the verified block policy. The build script checks that both transports import the same original storage bindings and that the compiled object matches the recorded hash.

## Reporting

The payload report records:

| Field | Meaning |
| --- | --- |
| `kernel_sha256` | Input kernel |
| `payload_kernel_sha256` | Kernel in the payload, after the optional panic screen patch |
| `adt_sha256` | ADT sidecar |
| `trust_cache_sha256` | Trust cache bundle |
| `data_ramdisk_sha256` | Data seed |
| `payload_sha256` | Payload image |

The boot manager log records the staged kexts, the installed ACPI tables and the injected drivers.

## Next steps

- [Payload](/concepts/payload) describes the trust cache and Data inputs.
- [Patch reference](/xnu/patch-reference) lists the security patches.
- [Boot arguments](/xnu/boot-arguments) lists the security related arguments.
