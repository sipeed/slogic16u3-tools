# resources/ 资源目录约定

本目录由**管理员**按以下约定放置文件，产测 GUI 启动时自检并提示缺失项。
新增产品 = 新增一个目录 `products/<product_id>/`（含 `product.toml` + `programmer.toml`）
并放置对应固件资源，**无需修改任何代码**。

```
resources/
├── products/<product_id>/        # 一产品一目录，目录名 = product_id
│   ├── product.toml              # 产品档案（USB PID / 通道 / 带宽 / 期望信号 / 模式切换），公开入库
│   ├── programmer.toml           # 外置烧录器命令（cli / device / flash / efuse），公开入库
│   └── firmware/                 # 固件与烧录资源（工厂本地，禁止入库/公开）
│       ├── app.bin               #   应用固件（DFU 模式下经 SPI Flash 写入）
│       ├── dfu.fs / dfu.bin      #   空板刷机镜像（16U3=.fs / 32U3=.bin）
│       └── efuse.ekey            #   eFuse AES 密钥（机密，绝不入库）
└── bin/                          # 跨平台工具二进制
    ├── sigrok-cli-linux-x86_64            # sigrok-cli：Linux AppImage 重命名/软链
    ├── sigrok-cli-windows-x86_64.exe      #   Windows
    ├── sigrok-cli-macos-arm64             #   macOS
    └── Gowin-Programmer-*.AppImage        # Gowin 烧录器（programmer.cli 默认自动 glob）
```

## 放置示例（Linux 工位）

```bash
# sigrok-cli（来自 SLogic 发布包）
ln -sf /path/to/sigrok-cli-SLogic-linux-x86_64-musl.AppImage resources/bin/sigrok-cli-linux-x86_64

# 应用固件与空板镜像
cp firmware_v1.2.3.bin  resources/products/slogic16u3/firmware/app.bin
cp dfu.fs               resources/products/slogic16u3/firmware/dfu.fs

# eFuse AES 密钥（机密，仅工位本地）
cp efuse.ekey           resources/products/slogic16u3/firmware/efuse.ekey
```

## programmer.toml：一条 CLI 驱动所有烧录器操作

空板烧写与 eFuse 读/写/锁**不再用包装脚本**，全部是同一条 `cli` argv 前缀 + 参数 +
资源文件（见 `slogicpt/programmer.py`）：

| 操作 | 命令形态 |
|---|---|
| 探测 / 读器件码 | `<cli> --device D --cable-index c --run 0` |
| eFuse 读锁定位 | `<cli> --device D --cable-index c --keyread` |
| 空板烧写 | `<cli> --device D --cable-index c --run <run> --fsFile <image> --spiaddr <addr>` |
| eFuse 写入并锁定（不可逆） | `<cli> --device D --cable-index c --keywritefile --keyFile <key> --keylock` |
| DFU↔APP 保底切换 | `<cli> --device D --cable-index c <switch.dfu2app / app2dfu 参数>` |

```toml
schema_version = 1

[programmer]
# cli 可省略：默认 glob resources/bin/Gowin-Programmer*.AppImage + "--programmer-cli"
# cli = ["../../bin/Gowin-Programmer-....AppImage", "--programmer-cli"]  # 覆盖默认
# cli_windows = ["C:/Gowin/Programmer/bin/programmer_cli.exe"]          # 按平台覆盖
device = "GW5AT-15A"           # 16U3；32U3 = "GW5AT-60B"
cable_candidates = [4, 1, 5]   # 按顺序试，首个读到器件的即用；32U3 = [5, 4, 1]
timeout_s = 30

[programmer.flash]             # 空板烧写；缺省 → 无烧空板能力
image = "firmware/dfu.fs"      # 相对产品目录；32U3 = "firmware/dfu.bin"（.fs/.bin 位流皆可）
# run 默认 54 = exFlash Erase,Program,Verify Arora V（--fsFile 通吃 .fs 与 .bin 位流）。
# 切勿用 55/56：那是 RISC-V 软核固件的 "C Bin"，烧 FPGA 位流会写 0 字节、Verify 失败。
spiaddr = 0x800000             # 外部 SPI Flash 8M 偏移（golden image 槽，16U3/32U3 相同）

[programmer.efuse]             # eFuse AES 密钥；缺省 → 烧空板无 eFuse 前置步骤
key_file = "firmware/efuse.ekey"

# [programmer.switch]          # DFU↔APP 保底切换（产品不支持 USB RECONFIG 时）
# dfu2app = ["--run", "52", "--fsFile", "firmware/app_sram.fs"]
# app2dfu = ["--run", "52", "--fsFile", "firmware/dfu.fs"]
# 每方向一组参数，追加在公共前缀之后；含 "/" 的 token 按产品目录解析为绝对路径。
```

> **eFuse 与烧空板的关系**：DFU 位流是 AES 加密的，FPGA 须先把密钥写入 eFuse
> 才能启动它（write），lock 只是防止密钥被读出。因此声明了 `[programmer.efuse]`
> 时，烧空板序列自动带前置步骤 `blank:efuse`：读锁定位 → 已锁则跳过 → 未锁则
> 写入并锁定（**不可逆**）→ 回读校验，随后才烧写 DFU 镜像；步骤结果计入测试
> 报告。GUI 无独立 eFuse 按钮，锁定状态（⚪未知/🔓未锁/🔒已锁，来自顶栏 🔄
> 扫描）显示在设备状态栏前缀。

> - `cli` 第一个参数若能在产品目录内解析为文件则按文件用，否则按 PATH 命令查找，
>   因此 Windows 只需把 `cli_windows` 指向本机 `programmer_cli.exe` 即可，其余不变；
> - 烧写（flash）一步也可换 **openFPGALoader**（开源、跨平台、支持 Gowin）：把对应
>   平台可执行文件放进 `bin/`、`cli` 指向它并调整参数即可；**eFuse Lock 无开源替代**，
>   仍需 Gowin 官方工具（Linux/Windows 均有 CLI）。

## product.toml：产品档案

> DFU↔APP 模式切换**无需任何脚本**，按可用方案自动分派（优先级从高到低）：
> 1. `[mode_switch] usb_reconfig = true`（product.toml）：产品自身经 USB 控制
>    传输 RECONFIG 双向切换（32U3），不依赖外置硬件；
> 2. `[programmer.switch]`（programmer.toml）：外置烧录器保底方案；
> 3. 都没有（或都失败）：GUI 弹窗提示工人按板载 MODE 按键，检测到目标模式
>    设备后自动继续。

## 保密边界（重要）

本仓库是公开仓库，以下内容**仅限工厂产测工位本地放置，禁止提交入库或以任何形式公开**
（.gitignore 已强制忽略）：

- `products/<id>/firmware/*.fs`、`*.bin` — DFU 位流、应用固件等烧录资源
- `products/<id>/firmware/*.ekey`、`efuse_key.txt` — eFuse AES 密钥（机密）

`product.toml` 与 `programmer.toml` 只声明接口契约（不含固件/密钥/位流实现），公开入库。

## 启动警告 → 解决办法对照

GUI 顶栏"⚠ 警告"角标里的每一条都对应一个待放置/待确认项，补齐后重启即消：

| 警告 | 解决办法 |
|---|---|
| `usb.dfu_pid 未配置` | 硬件确认该产品 DFU 模式 PID 后填入 `product.toml` 的 `[usb] dfu_pid` |
| `应用固件缺失: .../app.bin` | 把应用固件放到 `products/<id>/firmware/app.bin` |
| `未找到烧录器 CLI` | 把 Gowin Programmer AppImage 放进 `bin/`，或在 `programmer.toml` 声明 `cli` |
| `DFU 镜像缺失` | 把空板镜像放到 `products/<id>/firmware/`（16U3=dfu.fs / 32U3=dfu.bin） |
| `eFuse 密钥文件缺失` | 把 `efuse.ekey` 放到 `products/<id>/firmware/`（缺失时烧空板的 eFuse 前置步骤会失败） |
| `未找到 sigrok-cli 二进制` | 按平台命名放入 `bin/`（见上文） |

## 跨平台（Linux / Windows）

- `programmer.toml` 的 `cli` 支持 `cli_linux` / `cli_windows` / `cli_darwin` 覆盖，
  同一份资源包可同时服务两种产线工位（其余声明与资源全平台通用）；
- Windows 工位需 libusb 环境（WinUSB 驱动/Zadig 与 `libusb-1.0.dll`），与旧产测环境一致。

## 打包分发

`python build.py`（PyInstaller）产出单二进制，与本 `resources/` 目录平级摆放：

```
SLogicPT/
├── slogic-pt-linux-x86_64        # 或 slogic-pt-windows-x86_64.exe
├── resources/                    # 本目录整体拷贝，管理员随时可改，无需重新打包
└── out/                          # 运行时自动生成（采样数据）
```

## 产品档案要点

- `usb.dfu_pid` 未知时可先不写，GUI 会提示 "DFU 未配置" 并禁用 DFU 与一键流程，其余功能不受影响；
- `capture.samplerates` 必须填 sigrok 驱动实际支持的档位（`sigrok-cli -d <driver> --show` 查询），
  GUI 会再按 `channels × rate / 8 ≤ max_bandwidth_mbps` 过滤展示；
- `firmware.app_flash_addr` 因产品而异，填错会导致固件无法启动；
- `[[capture.tests]]` 是"一键全流程"逐组执行的测试点。
