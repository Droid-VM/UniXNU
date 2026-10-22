# 完整性与启动策略

UniXNU 保留原始 Apple 安全组件，并提供它们检查的输入。源码内核加入这些组件所需的平台代码，启动负载提供描述设备的 trust cache 和设备树属性。

## 输入

| 输入 | 提供方 |
| --- | --- |
| trust cache 包 | `--trust-cache FILE` |
| trust cache 哈希 | `[payload] trust_cache_sha256` |
| 平台种子 | `--unique-chip-id HEX` |
| 设备树身份 | 格式 2 的带类型 `[adt]` 表；格式 1 的板级脚本 |
| PMGR 集成 | 需要时使用 `XNU_SHENG_PMGR` |
| 根策略 | `XNU_VERIFIED_ROOT_PROBE`、`XNU_OWNER_ROOT_AUDIT`、`XNU_OWNER_ROOT_TRUST` |

## trust cache

DarwinBoot 校验 trust cache 包结构，并把它复制到内核窗口。ADT 的 `/chosen/memory-map/TrustCache` 占位符接收地址和大小。

补丁 `native-trust-cache-runtime` 在 `BCM2837` 上初始化运行时 trust cache 路径。原始 AppleImage4 和 AMFI 组件按缓存校验代码。

负载步骤只接受 SHA-256 位于 `[payload] trust_cache_sha256` 的 trust cache 包。

## 4 KiB 下的代码签名

原始 CodeDirectory 使用 16 KiB 哈希页。4 KiB 内核校验 4 KiB 页。补丁 `native-4k-signature-hash-cache`：

1. 通过 `vn_rdwr` 读取每个原始 16 KiB 块。
2. 用原始 CodeDirectory 哈希校验该块。
3. 从已验证块派生每个 4 KiB 页的哈希。
4. 把派生哈希存入只读内核内存。
5. 让 `cs_validate_page` 使用派生表。

只有原始 16 KiB 哈希匹配后，块才会发布。校验失败的块不产生派生表，页校验返回 `CS_VALIDATE_TAINTED`。

## AMFI 策略

ADT 属性 `/chosen/amfi-only-platform-code` 让 AMFI 保持在平台代码策略。`[kernel.prelink] early_security_init` 在指定地址运行原始 `AppleMobileFileIntegrity` 初始化器。

`offline-owner-root-*` 补丁为首次启动加入 owner root 检查：

| 补丁 | 功能 |
| --- | --- |
| `offline-owner-root-binding-audit` | 用启动 manifest 检查根绑定 |
| `offline-owner-root-crypto-probe` | 检查 owner crypto provider |
| `offline-owner-boot-manifest` | 记录启动 manifest 哈希 |
| `offline-owner-cache-carrier` | 选择代码签名缓存载体 |
| `offline-owner-systemos-mount` | 挂载 SystemOS cryptex |
| `offline-owner-z-air-init` | 初始化 owner root 的 Z air 路径 |

## GPU client 授权

Adreno740 在加速器服务上发布 `NativeSandboxGrantProcess`。补丁 `zzw-native-adreno-sandbox-grant` 把打开进程的名称与该属性比较，指定进程可以绕过 `mac_iokit_check_open_service` 和 `mac_iokit_check_open` 打开 user client。该属性是一个名称（render server），或一个名称数组：板子若给其他进程各自的原生 Metal 设备，就把它们与 render server 一起列出，因为这些进程自己的沙箱配置同样拒绝该驱动的 user client。名称与进程名的前 16 个字符比较。

## verified root

`kits/VerifiedRoot` 提供签名的策略包。verified block 驱动按策略校验每次读取。策略可以按容量用 `--verified-root-bundle` 选择。

| 工具 | 功能 |
| --- | --- |
| `verified_root.c` | 策略解析和读取校验 |
| `xnu_crypto.c` | XNU crypto 适配 |
| `manifest.py` | 策略包构建 |
| `verify_image.py` | 镜像校验 |

## 存储完整性

`VirtioMmioBlock` 和 `RamBlock` 共用 verified block 策略。构建脚本检查两个传输层导入相同的原始存储绑定，并检查编译对象与记录哈希一致。

## 报告

负载报告记录：

| 字段 | 含义 |
| --- | --- |
| `kernel_sha256` | 输入内核 |
| `payload_kernel_sha256` | 负载中的内核，经过可选 panic 屏幕补丁 |
| `adt_sha256` | ADT sidecar |
| `trust_cache_sha256` | trust cache 包 |
| `data_ramdisk_sha256` | Data 种子 |
| `payload_sha256` | 负载镜像 |

启动管理器日志记录暂存的 kext、安装的 ACPI 表和注入的驱动。

## 下一步

- [启动负载](/zh/concepts/payload)
- [补丁参考](/zh/xnu/patch-reference)
- [启动参数](/zh/xnu/boot-arguments)
