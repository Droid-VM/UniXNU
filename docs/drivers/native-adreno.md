# Adreno 740 driver

sheng drives the Adreno 740 through native XNU services. The stack uses native 4 KiB SMMU translation, GMU firmware, a CP ring, real fences and GPU compositing into the scanout.

## Runtime path

```mermaid
flowchart LR
  A[iPadOS app] --> B[QuartzCore and RenderBox]
  B --> C[NativeMetal plugin]
  C --> D[metal2vulkan]
  D --> E[Turnip]
  E --> F[NIR and IR3]
  F --> G[Darwin KMD]
  G --> H[NativeAdreno]
  H --> I[GMU, CP, SMMU and ring]
  I --> J[Adreno 740]
  J --> K[GPU composite to scanout]
  K --> L[NativeMobileDisplay]
```

1. NativeMetal publishes a Metal device through the system `MTLAddDevice` path. The published mode returns the device to backboardd, and listed processes receive their own device. It owns the Metal objects, encoders, IOSurfaces, events, fences and pipeline archives.
2. metal2vulkan translates AIR to SPIR-V and supplies pipeline descriptors, binding plans and function constants.
3. Turnip compiles SPIR-V through NIR to A740 IR3. Its Darwin KMD talks to the NativeAdreno user client.
4. NativeAdreno owns BOs, GPU addresses, the submission ring, fences and SMMU contexts. The GMU brings the GPU out of reset and applies power votes. The CP executes the IBs.
5. The plugin composites the finished frame into scanout pages. NativeMobileDisplay offers and retires the swap.

The runtime compiler chain is Metal, AIR, SPIR-V, NIR, IR3. AirNir is an offline direct AIR to NIR to IR3 compiler.

## Publish to the render server

`scripts/add-gpu-plugin-data.py` stages the plugin into a copy of the packed Data seed.

| Option | Effect |
| --- | --- |
| `--publish` | Writes `NativeAdrenoMetal.publish`. backboardd returns the native device to the original discovery, and CARenderServer composites the system UI through it. |
| `--clients NAME,...` | Writes `NativeAdrenoMetal.clients`. Each listed process gets its own device and driver connection. Up to two also get a 256 MiB GPU window and their own shader cache. `*` lists every process. |

The published device turns off backboardd's deep and compressed framebuffers. Patch `zzw-native-adreno-sandbox-grant` lets the accelerator service name the render server in `NativeSandboxGrantProcess`, so its user-client open passes the sandbox check. The property can also be an array, the render server and the processes given a native device of their own (DroidVM's `droidvm-gpu-clients`).

NativeAdreno serves one user-client core per connection. The render server gets a 1 GiB window at `0x20000000`, and up to two listed clients get 256 MiB each with their own queue range and teardown.

## Components

| Component | Role |
| --- | --- |
| `drivers/NativeMetal` | Metal frontend, metal2vulkan, command encoding, IOSurface and scanout client |
| `drivers/NativeAdreno` | GPU service, GMU/CP startup, user client, BO, VM, ring, submit and fence |
| `drivers/NativeMobileDisplay` | Swap offer, validation, retirement and scanout hand-off |
| `kits/AdrenoFirmware` | GMU HFI, startup, power votes and SQE firmware data |
| `kits/AdrenoPM4` | A740 PM4 state, constants, VPC and rasterizer packets |
| `kits/ArmSmmu4K` | Native 4 KiB tables, context, streams, live mappings and TLB maintenance |
| `kits/AirNir` | Offline AIR to NIR to IR3 compiler |

## Recent changes, newest first

| Date | Commit | Change |
| --- | --- | --- |
| 2026-10-20 | UniXNU `16d0b1e` | Grants the render server its GPU client and silences the per-mapping managed-page report that flooded the kernel message buffer. |
| 2026-10-20 | UniXNU `6440a94` | Applies translator patches 0004 and 0005, adds the frontend, archive, resource and registration checks, and stages the published device with `--publish` and `--clients`. |
| 2026-10-20 | NativeMetal `6dcd4cb` | Journals CPU and system time, page faults, page-ins and context switches for each render pipeline creation. |
| 2026-10-20 | NativeMetal `5df5195` | Answers Apple1-3 and Common1-2 GPU families as the original device does, and journals waived feature rows and function specializations. |
| 2026-10-20 | NativeMetal `5ca27a6` | Gives each listed process its own device, driver connection and shader cache, and journals QuartzCore callers and context flags. |
| 2026-10-20 | NativeMetal `8eea9ca` | Prunes uber-shader entries before inlining and keeps recent module decodes and profile fingerprints. |
| 2026-10-20 | NativeAdreno `805c7e4` | Skips write-combine re-assertion for pooled BOs; the per-submission pool walk cost 0.65 ms. |
| 2026-10-20 | NativeAdreno `6fed926` | Sets the BO pool to 1536 MiB; large BOs stay in the pool and peak use is 782 MiB. |
| 2026-10-20 | NativeAdreno `2668969` | Observes owned SMMU routes every 64th table operation and skips the image observation in submission TLB invalidation. |
| 2026-10-20 | NativeAdreno `a1032b2` | Pools only user BOs and serves one user-client core per process: 1 GiB for the render server, 256 MiB for two clients. |
| 2026-10-20 | NativeAdreno `b6ca2ff` | Write-combines the pool once and limits map and unmap observations to the global policy, the context and owned routes. |
| 2026-10-20 | ArmSmmu4K `4d0d659` | Reads the context only on whole-verification updates and skips TLB invalidation for invalid leaves. |
| 2026-10-20 | ArmSmmu4K `960de2c` | Observes owned streams without the all-slot scan. |

## State

| Step | Result |
| --- | --- |
| GMU cold start and HFI | works |
| GPU context and CP commands | works |
| Native 4 KiB SMMU context | active |
| Metal device discovery | works |
| GPU frame copy into scanout | works, pixel-identical |
| Render server compositing | works |
| Per-process Metal devices | works |
| GPU-composited desktop | works |

The GPU path covers discovery, command encoding, pipeline archives, IOSurface imports, fences, compositing and scanout. The render server and listed clients use the native device.

## Shader compilation

The driver compiles the original UI shaders on the device:

1. `default.metallib` carries AIR plus Apple GPU machine code. The AIR feeds the translation path.
2. metal2vulkan translates AIR to SPIR-V, then Turnip compiles IR3 at pipeline creation.
3. A cold boot compiles every UI variant it reaches. int-148: 77 pipelines, about 43 s, cache hit 0. int-150: 103 Metal pipelines, 40.1 s total, 1.40 s max; 94 Vulkan pipelines, 23.3 s.
4. Patch `0004` specializes the QuartzCore uber shaders and folds constant loads before inlining. Patch `0005` keeps recent module decodes and device-profile fingerprints. The pipeline journal records each render pipeline creation.

Implemented mitigations:

| Mitigation | Effect |
| --- | --- |
| NDEBUG release build | Removes per-pass Mesa assertions and NIR validation. |
| metal2vulkan patch `0004` | Specializes and prunes QuartzCore uber shaders before Vulkan compilation. |
| metal2vulkan patch `0005` | Keeps recent module decodes and device-profile fingerprints. |
| Priming cache | Caches runtime startup entry-point work. |
| `m2vpack` export and import | Reuses earlier compiled pipeline caches; the export is incremental. |
| Turnip builtin pack | Embeds clear, blit and empty shader variants. |
| Pipeline journal | Records per-pipeline CPU and system time, page faults and context switches. |

The cache chain covers runtime startup and the UI variants an earlier boot exported. A new UI variant compiles once, then enters the exported cache.

## Build

```sh
python3 scripts/build-turnip-darwin.py --target ios
python3 scripts/build-adreno-metal-plugin.py
python3 scripts/add-gpu-plugin-data.py --publish --clients <name,...> <seed>.dbr <output>.dbr
```

The staging script output is the Data seed for `unixnu build payload --data-ramdisk`.

Host checks:

```sh
python3 scripts/check-native-metal-frontend-host.py
python3 scripts/check-turnip-darwin-host.py
python3 scripts/check-smmu4k-live.py
python3 scripts/check-turnip-pipeline-cache-host.py
python3 scripts/check-priming-cache-host.py --output build/priming-cache/run
python3 scripts/probe-turnip-internal-compiler-host.py --qualification <old> --output <new>
```

The device repository adds the original-input checks:

```sh
python3 scripts/build-adreno-compiler-host.py --probe
python3 scripts/build-turnip-ios-compiler-peer.py
python3 scripts/check-turnip-ios-compiler-cache.py
python3 scripts/check-quartzcore-turnip-cache-host.py
```

## Related

- [Xiaomi Pad 6S Pro (sheng)](/devices/sheng)
- [XNU drivers](/drivers/xnu-drivers)
- [Four-kilobyte pages](/xnu/four-k-page-tables)
