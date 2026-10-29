# GPU 与 Metal 适配

先完成[平台 DMA](/zh/porting/platform)和基础[显示呈现](/zh/porting/display)。
阅读 [Adreno740 运行路径](/zh/drivers/adreno740)与 [AdrenoMetal 接口](/zh/drivers/adreno-metal)。

实现后按[驱动接入步骤](/zh/porting/drivers#build-integration)连接初始化调用、归档配方和 OS ABI，
再执行本页对应的硬件验证。

## 源码与接口 {#source-map}

以下链接固定到已核对的源码版本。使用所选 SoC、组件和 OS 版本核对接口；
`UniXNU-sheng` 中的文件提供板级集成示例。

| 实现部分 | 源码 | 阅读与实现内容 |
| --- | --- | --- |
| 电源与固件 | [NativeAdrenoGmuPlatform.h](https://github.com/UniXNU/Adreno740/blob/4e0314df8ad29d52f6fa3f9383a540adcd618b87/NativeAdrenoGmuPlatform.h#L15), [build-adreno740-kits.py](https://github.com/UniXNU/UniXNU-sheng/blob/516a780473a2ac59c8b438e132bb60204dda9e22/scripts/build-adreno740-kits.py) | `NativeAdrenoGmuPlatform::bind/services`、映射窗口、计时器和固件输入 |
| 提交与内存 | [NativeAdrenoClient.h](https://github.com/UniXNU/Adreno740/blob/4e0314df8ad29d52f6fa3f9383a540adcd618b87/NativeAdrenoClient.h#L14), [NativeAdrenoVm.h](https://github.com/UniXNU/Adreno740/blob/4e0314df8ad29d52f6fa3f9383a540adcd618b87/NativeAdrenoVm.h) | `NativeAdrenoTransport`、BO 生命周期、GPU 映射和完成 |
| OS/前端接口 | [NativeOfflineAccelerator.cpp](https://github.com/UniXNU/AdrenoMetal/blob/454ff2ce83766ce7b3e0f355e83f1d72907708b3/NativeOfflineAccelerator.cpp), [OfflineMetalWire.h](https://github.com/UniXNU/AdrenoMetal/blob/454ff2ce83766ce7b3e0f355e83f1d72907708b3/OfflineMetalWire.h) | 加速器发布及配套前端使用的请求结构 |

## 1. 匹配 GPU 代际与 OS ABI

Adreno740 实现 A740 内核接口。AdrenoMetal 使用该接口、Turnip、metal2vulkan
和选定的 OS GPU/IOSurface ABI。确认目标 GPU 版本，对照寄存器、固件、命令包和内存接口。
其他 GPU 代际需要先实现对应内核后端和编译器支持，再选择前端。

另一块 A740 设备需要收集 GPU/GMU 寄存器、中断、SMMU context 与 stream ID、固件、
电源域、时钟/电压档位、互连投票、RAM 保留区和扫描输出布局，并与选定固件核对。

## 2. 适配电源和固件输入

阅读 [SM8550 平台源码](https://github.com/UniXNU/UniXNU-sm8550-common/tree/main/kits)
和 [sheng package](https://github.com/UniXNU/UniXNU-sheng/tree/main/package)。
`Sm8550Platform` 提供共享 GMU 操作和 command database 处理，
`ShengGmuVotes.c` 与 `ShengGmuVotesSource.h` 提供板级 OPP 和带宽策略。
根据目标电源和 command database 适配这些输入。

sheng 的 `scripts/build-adreno740-kits.py` 嵌入以下本地输入：

| 固件 | 用途 |
| --- | --- |
| `qcom/gmu_gen70200.bin` | GMU 镜像 |
| `qcom/a740_sqe.fw` | 命令处理器固件 |
| `qcom/sm8550/sheng/a740_zap.mbn` | 板级签名 ZAP 镜像 |

配方读取 `.cache/firmware/sheng-gpu/`，核对 `scripts/data/adreno-inputs.json` 中的固定值。
按目标设备已确认的固件修改提取路径与固定值，在 `proprietary-files.txt` 中记录来源。

**预期结果：** 板级电源输入和固件匹配目标 GPU。提交命令前确认 GMU 启动和 HFI 响应。

## 3. 建立内存和命令提交路径

1. 核对 ArmSmmu4K context 所有权、stream 选择、地址宽度和缓存维护。
   使用目标硬件描述中的 GPU 和固件 stream ID。
2. 适配保留内存和 GPU 虚拟地址窗口，确保扫描输出、固件、ring 和客户端分配位于各自拥有的范围。
3. 使用有界任务确认 CP 初始化、ring 推进和 fence 完成。
4. 渲染已知图像并检查结果，再运行 UI 工作负载。
5. 验证客户端退出、映射生命周期和 fence 失败处理。

使用 AdrenoFirmware 的固件/HFI 操作和 AdrenoPM4 支持的命令包编码。
sheng 配方把这些 kit、ArmSmmu4K 和板级投票链接到 `libAdreno740.a`，再通过
`build-adreno740-accelerator.py` 构建 accelerator。创建目标配方时保留该顺序。

**预期结果：** 命令完成、fence 推进、已知图像正确。GPU 结果与桌面呈现结果分别记录。

### 降低映射与提交开销 {#memory-path}

桌面每帧都会分配、映射和释放大量缓冲区，单次调用的开销决定流畅度。Adreno740
和 ArmSmmu4K 遵循以下规则，移植时保持不变：

| 操作 | 规则 |
| --- | --- |
| 对象查找 | 客户端的对象数只受内存限制，通过 handle 索引查找。 |
| 分配 | BO 来自系统内存：不带 task、缓存模式为 write-combined 的可分页 `IOBufferMemoryDescriptor`，由 `prepare()` 锁定。VM 一次批量设置页属性，释放时恢复；客户端视图映射该描述符。只有要求连续内存的调用方才走连续的内核分配。 |
| 映射自有内存 | `ArmSmmu4KLiveMapContextBatchExclusive`：不与已有区间做别名检查。 |
| 映射导入内存 | 已映射的导入都属同一缓存类别，且其页都不在本驱动的 BO 中（覆盖 DRAM 的页位图）时，使用同一调用；否则使用完整检查的批量映射。 |
| 解除映射 | 不超过 512 页时每页一次 TLBIVAL，更多时按 ASID 失效。 |
| 提交 | 不做 TLB 失效。映射只填写无效叶子，解除映射会失效自己的页，上层页表不变。fence 卡住时仍会失效。 |

启动日志的 `[vm map]`、`[vm unmap]`、`[vm kinds]` 和 `[bo memory]` 行给出每种操作的
次数和耗时。修改前后对比这些行。

使用 `NativeAdrenoClient` 接口时，按以下顺序实现一次有界提交：
查询 ABI/能力，分配 BO，将其映射到允许的 IOVA，填写命令，再提交并等待返回的 fence。
通过 `NativeAdrenoTransport.call` 连接实际内核分发端，保留各请求的输入/输出大小。
CPU 读取结果前完成缓存同步；fence 超时时先停止队列并确认 DMA 已停止，再释放 BO 和映射。
在错误返回中保留失败的请求类型、句柄和 fence，便于定位分配、映射或执行阶段。

## 4. 构建并加入 Metal

准备匹配的 SDK、编译器依赖和 ABI 输入后，在目标构建树执行：

```sh
python3 scripts/build-turnip-darwin.py --target ios
python3 scripts/build-adreno-metal-plugin.py
python3 scripts/add-gpu-plugin-data.py --publish /path/to/base.dbr /path/to/gpu.dbr
```

加入插件的命令使用 macOS 镜像工具，生成 Data seed 副本。
构建 payload 时将输出传给 `--data-ramdisk`，再重建启动镜像。
如需指定应用进程，还可传入 `--clients NAME,...`。运行时插件名为 `NativeAdrenoMetal`。

核对所选 OS profile 的 sandbox、user-client 和 trust-cache 集成。
Metal 桥接层从不返回 nil encoder：无法编码的 pass 会得到一个命令缓冲区被拒绝的
encoder，因为 RenderBox 会缓存 encoder 的方法。进程的第一个问题写在驱动的进程
文本行上（启动日志中的 `[gpu client text]`）。
AirNir 是离线编译器 kit；运行时前端使用组件页说明的 AIR → SPIR-V → NIR → IR3 路径。

**预期结果：** Metal 设备发现、客户端连接、pipeline 创建、渲染和呈现分别成功。
随画面结果记录固件、编译器和插件版本，分别测量首次 pipeline 编译和缓存复用时间。

## 下一步

继续 [Wi-Fi](/zh/porting/network)，或返回[驱动清单](/zh/porting/drivers)。
