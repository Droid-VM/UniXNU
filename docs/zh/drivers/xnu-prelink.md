# XnuPrelink

代码匹配会识别编译器插入的完整 PAC 失败检查序列，输出内核仍保留这些检查。
两个生成入口都会核对匹配代码的签名，再复用已有映射。
`--fileset-reference FILE` 可以提供带符号的对照内核。唯一的代码匹配可提供符号名，
最终地址仍从实际编译的原生内核获取。映射记录中会保存对照文件的哈希。

对于 arm64e fileset 内核集合，`emit_prelink.py` 会选择 fileset 路径并生成
函数和数据映射。`--fileset-map FILE` 可以指定已有映射，输入哈希和匹配器版本
必须一致。输出保留所选根组件及其全部依赖。

输出工具把原生内核各段放进集合的代码、常量和数据范围，按原生链接器的分段
重定位记录改写地址，重建链式修正表和函数起点，并重新计算 corecrypto 的代码
HMAC。集合根头使用原生内核的链接地址，普通预链接的 `--slide` 不用于 fileset。
原生内核输入文件保持不变。

默认遇到缺失映射就停止。`--allow-missing` 可以生成实验映像，JSON 报告按
kext 列出所有未解析目标。结构验证通过后，仍需要补全映射并进行硬件验证，
才能确认启动兼容性。

`unixnu kernel --skip-prelink` 将原生可执行文件保存为 `kernel.macho`。
`unixnu prelink --skip-prelink` 可以使用已有构建。此模式不会加入原版 kext，
输出旁的记录标明 `native-only`。

先按[适配流程](/zh/porting/platform)准备板级输入、修改源码、接入构建并逐步验证。

内核预链接生成器与 ABI 工具。

[源码仓库](https://github.com/UniXNU/XnuPrelink).

XnuPrelink 将构建的 XNU 内核绑定到所选原始 kext，并写出预链接方案。OS 配置提供 kernelcache 固定版本、根组件、slide 和 ABI 设置。

## 集成

组件目录记录实现范围、硬件与 OS 要求、板级依赖、源码版本、构建阶段、输出和运行时标识符。所选配置按依赖顺序运行构建命令。

```sh
./unixnu build prelink -D ../UniXNU-sheng
```

[组件目录](/zh/reference/components)
