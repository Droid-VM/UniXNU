# Adreno 740 驱动

sheng 通过原生 XNU 服务驱动 Adreno 740。图形栈使用原生 4 KiB SMMU 翻译、GMU 固件、CP ring、真实 fence 和 GPU 到 scanout 的合成。

## 运行时链路

```mermaid
flowchart LR
  A[iPadOS 应用] --> B[QuartzCore 和 RenderBox]
  B --> C[NativeMetal 插件]
  C --> D[metal2vulkan]
  D --> E[Turnip]
  E --> F[NIR 和 IR3]
  F --> G[Darwin KMD]
  G --> H[NativeAdreno]
  H --> I[GMU、CP、SMMU 和 ring]
  I --> J[Adreno 740]
  J --> K[GPU 合成到 scanout]
  K --> L[NativeMobileDisplay]
```

1. NativeMetal 通过系统 `MTLAddDevice` 发布 Metal 设备。发布模式把设备交回 backboardd，白名单进程获得各自的设备。它管理 Metal 对象、编码器、IOSurface、event、fence 和 pipeline archive。
2. metal2vulkan 把 AIR 翻译成 SPIR-V，并给出 pipeline 描述、binding 计划和 function constant。
3. Turnip 把 SPIR-V 经过 NIR 编译成 A740 IR3。Darwin KMD 通过 NativeAdreno user client 访问内核。
4. NativeAdreno 管理 BO、GPU 地址、提交 ring、fence 和 SMMU context。GMU 让 GPU 退出 reset 并提交功耗票，CP 执行 IB。
5. 插件把完成帧合成到 scanout 页，NativeMobileDisplay 提供并回收 swap。

运行时编译链路是 Metal、AIR、SPIR-V、NIR、IR3。AirNir 是离线的 AIR 到 NIR 到 IR3 编译器。

## 发布到 render server

`scripts/add-gpu-plugin-data.py` 把插件写入打包 Data seed 的副本。

| 选项 | 作用 |
| --- | --- |
| `--publish` | 写入 `NativeAdrenoMetal.publish`。backboardd 把原生设备交回原有发现流程，CARenderServer 通过它合成系统界面。 |
| `--clients NAME,...` | 写入 `NativeAdrenoMetal.clients`。每个列入的进程获得自己的设备和驱动连接；其中最多两个还获得 256 MiB GPU 窗口和自己的 shader cache。`*` 表示所有进程。 |

发布模式关闭 backboardd 的 deep framebuffer 和压缩 framebuffer。补丁 `zzw-native-adreno-sandbox-grant` 让加速器服务在 `NativeSandboxGrantProcess` 中指定 render server，使其 user client 打开通过沙箱检查。该属性也可以是数组：render server 加上拥有自己原生设备的进程（DroidVM 的 `droidvm-gpu-clients`）。

NativeAdreno 为每个连接提供一个 user-client core。render server 获得 `0x20000000` 处 1 GiB 窗口，最多两个列入的客户端各获得 256 MiB 及自己的队列范围和回收路径。

## 组件

| 组件 | 作用 |
| --- | --- |
| `drivers/NativeMetal` | Metal 前端、metal2vulkan、命令编码、IOSurface 和 scanout client |
| `drivers/NativeAdreno` | GPU 服务、GMU/CP 启动、user client、BO、VM、ring、提交和 fence |
| `drivers/NativeMobileDisplay` | swap 提供、校验、回收和 scanout hand-off |
| `kits/AdrenoFirmware` | GMU HFI、启动、功耗票和 SQE 固件数据 |
| `kits/AdrenoPM4` | A740 PM4 状态、常量、VPC 和 rasterizer 包 |
| `kits/ArmSmmu4K` | 原生 4 KiB 表、context、stream、live mapping 和 TLB 维护 |
| `kits/AirNir` | 离线 AIR 到 NIR 到 IR3 编译器 |

## 最新提交，从新到旧

| 日期 | 提交 | 变化 |
| --- | --- | --- |
| 2026-10-20 | UniXNU `16d0b1e` | 为 render server 授予 GPU client，并关闭每个 mapping 都触发的 managed page 报告，该报告曾淹没内核消息缓冲区。 |
| 2026-10-20 | UniXNU `6440a94` | 应用翻译器补丁 0004 和 0005，加入前端、archive、资源和注册检查，并用 `--publish` 和 `--clients` 发布设备。 |
| 2026-10-20 | NativeMetal `6dcd4cb` | 为每次 render pipeline 创建记录 CPU 时间、系统时间、page fault、page-in 和上下文切换。 |
| 2026-10-20 | NativeMetal `5df5195` | 按原设备回答 Apple1-3 和 Common1-2 GPU family，并记录被豁免的 feature 行和 function specialization。 |
| 2026-10-20 | NativeMetal `5ca27a6` | 每个列入的进程获得自己的设备、驱动连接和 shader cache，并记录 QuartzCore 调用方和 context 标志。 |
| 2026-10-20 | NativeMetal `8eea9ca` | 在 inlining 前裁剪 uber-shader 条目，并保留最近的 module decode 和 profile fingerprint。 |
| 2026-10-20 | NativeAdreno `805c7e4` | 池化 BO 不再重复设置 write-combine；每次提交遍历池曾耗时 0.65 ms。 |
| 2026-10-20 | NativeAdreno `6fed926` | BO 池默认 1536 MiB；大 BO 留在池中，峰值使用 782 MiB。 |
| 2026-10-20 | NativeAdreno `2668969` | 每 64 次表操作才完整观察 owned SMMU route，并跳过提交 TLB 失效中的 image 观察。 |
| 2026-10-20 | NativeAdreno `a1032b2` | 只池化用户 BO，为每个进程提供一个 user-client core：render server 1 GiB，两个客户端各 256 MiB。 |
| 2026-10-20 | NativeAdreno `b6ca2ff` | 一次性设置池的 write-combine，map 和 unmap 只观察全局策略、context 和 owned route。 |
| 2026-10-20 | ArmSmmu4K `4d0d659` | 只在完整校验更新时读取 context，并对 invalid leaf 跳过 TLB 失效。 |
| 2026-10-20 | ArmSmmu4K `960de2c` | 观察 owned stream 时不扫描全部 slot。 |

## 状态

| 步骤 | 结果 |
| --- | --- |
| GMU 冷启动和 HFI | 工作 |
| GPU context 和 CP 命令 | 工作 |
| 原生 4 KiB SMMU context | active |
| Metal 设备发现 | 工作 |
| GPU 帧复制到 scanout | 工作，像素一致 |
| render server 合成 | 工作 |
| 每进程 Metal 设备 | 工作 |
| GPU 合成桌面 | 工作 |

GPU 路径覆盖发现、命令编码、pipeline archive、IOSurface 导入、fence、合成和 scanout。render server 和列入的客户端使用原生设备。

## 着色器编译

驱动在设备上编译原始 UI shader：

1. `default.metallib` 带有 AIR 和 Apple GPU 机器码，AIR 进入翻译路径。
2. metal2vulkan 把 AIR 翻译成 SPIR-V，Turnip 在 pipeline 创建时编译 IR3。
3. 冷启动会编译它遇到的每个 UI 变体。int-148：77 个 pipeline，约 43 s，cache hit 0。int-150：103 个 Metal pipeline，共 40.1 s，最大 1.40 s；94 个 Vulkan pipeline，共 23.3 s。
4. 补丁 `0004` 在 inlining 前特化 QuartzCore uber shader 并折叠常量加载。补丁 `0005` 保留最近的 module decode 和 device-profile fingerprint。pipeline journal 记录每次 render pipeline 创建。

已实现的缓解：

| 缓解 | 效果 |
| --- | --- |
| NDEBUG release 构建 | 去掉每 pass 后的 Mesa assertion 和 NIR 校验。 |
| metal2vulkan 补丁 `0004` | 在 Vulkan 编译前特化和裁剪 QuartzCore uber shader。 |
| metal2vulkan 补丁 `0005` | 保留最近的 module decode 和 device-profile fingerprint。 |
| priming cache | 缓存运行时启动 entry point 工作。 |
| `m2vpack` 导出和导入 | 复用之前编译的 pipeline cache；导出是增量的。 |
| Turnip builtin pack | 内嵌 clear、blit 和 empty shader 变体。 |
| pipeline journal | 记录每个 pipeline 的 CPU 时间、系统时间、page fault 和上下文切换。 |

cache 链路覆盖运行时启动和之前启动导出过的 UI 变体。新的 UI 变体编译一次后进入导出 cache。

## 构建

```sh
python3 scripts/build-turnip-darwin.py --target ios
python3 scripts/build-adreno-metal-plugin.py
python3 scripts/add-gpu-plugin-data.py --publish --clients <name,...> <seed>.dbr <output>.dbr
```

staging 脚本的输出是 `unixnu build payload --data-ramdisk` 使用的 Data seed。

主机检查：

```sh
python3 scripts/check-native-metal-frontend-host.py
python3 scripts/check-turnip-darwin-host.py
python3 scripts/check-smmu4k-live.py
python3 scripts/check-turnip-pipeline-cache-host.py
python3 scripts/check-priming-cache-host.py --output build/priming-cache/run
python3 scripts/probe-turnip-internal-compiler-host.py --qualification <old> --output <new>
```

设备仓库增加原始输入检查：

```sh
python3 scripts/build-adreno-compiler-host.py --probe
python3 scripts/build-turnip-ios-compiler-peer.py
python3 scripts/check-turnip-ios-compiler-cache.py
python3 scripts/check-quartzcore-turnip-cache-host.py
```

## 相关

- [小米平板 6S Pro（sheng）](/zh/devices/sheng)
- [XNU 驱动](/zh/drivers/xnu-drivers)
- [4 KiB 页表](/zh/xnu/four-k-page-tables)
