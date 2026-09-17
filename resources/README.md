# resources/ 资源目录约定

本目录由**管理员**按以下约定放置文件，产测 GUI 启动时自检并提示缺失项。
新增产品 = 新增一个目录 `products/<product_id>/`（含 `product.toml` + `programmer.toml`）
并放置对应固件资源，**无需修改任何代码**。

```
resources/
├── programmer.toml               # 共享烧录器默认（cli/cable/flash/efuse/timeout），公开入库
├── products/<product_id>/        # 一产品一目录，目录名 = product_id
│   ├── product.toml              # 产品档案（USB PID / 通道 / 带宽 / 期望信号 / 模式切换），公开入库
│   ├── programmer.toml           # 通常仅一行 device；其余继承共享默认，公开入库
│   └── firmware/                 # 固件与烧录资源（工厂本地，禁止入库/公开）
│       ├── app.bin               #   应用固件（DFU 模式下经 SPI Flash 写入）
│       ├── dfu.fs / dfu.bin      #   空板刷机位流（16U3=.fs / 32U3=.bin，均经 --fsFile）
│       └── efuse.ekey            #   eFuse AES 密钥（机密，绝不入库）
└── bin/                          # 跨平台工具二进制
    ├── sigrok-cli-linux-x86_64            # sigrok-cli：Linux AppImage 重命名/软链
    ├── sigrok-cli-windows-x86_64.exe      #   Windows
    ├── sigrok-cli-macos-arm64             #   macOS
    ├── Gowin-Programmer-x86_64.AppImage   # Gowin 烧录器（Linux，版本无关名；也兼容带版本原名）
    ├── Programmer/                        # Gowin 烧录器（Windows：整个 Programmer 文件夹拷进来）
    └── libusb-1.0.dll                     # Windows：pyusb 后端（见下文跨平台）
```

> **烧录器配置绝大多数是共享的**：16U3/32U3 用同一 Gowin Programmer，`cli`/`cli_windows`、
> `cable_candidates`、`[programmer.flash]`（含 `image` 的 glob `firmware/dfu.*`、`spiaddr`）、
> `[programmer.efuse]`（`key_file`）都集中在 `resources/programmer.toml` 给默认值。
> **各 `products/<id>/programmer.toml` 通常只需一行 `device`**（唯一各产品不同、必填、
> 无默认的字段——IDCODE 必须精确匹配）；需要时才按同名键覆盖（`cable_candidates` 整体
> 覆盖，`flash`/`efuse` 按键合并）。解析优先级：产品档案 → 共享 → 代码内默认
>（cli：Linux glob `Gowin-Programmer*.AppImage` / Windows `bin/Programmer/bin/programmer_cli.exe`；
> cable：`[4,1,5,0]`；flash run：54）。

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

共享 `resources/programmer.toml`（给所有产品的默认值）：

```toml
schema_version = 1

[programmer]
# cli 可省略：默认 Linux glob resources/bin/Gowin-Programmer*.AppImage + "--programmer-cli"，
# Windows 用 resources/bin/Programmer/bin/programmer_cli.exe。含 "/" 路径按 resources/ 解析。
cli         = ["bin/Gowin-Programmer-x86_64.AppImage", "--programmer-cli"]
cli_windows = ["bin/Programmer/bin/programmer_cli.exe"]
timeout_s = 30
cable_candidates = [4, 1, 5, 0]   # 逐个试，首个读到本 device 的即用（顺序只影响速度）

[programmer.flash]                # 空板烧写
image = "firmware/dfu.*"          # glob 各产品 firmware/ 下的 dfu 位流（dfu.fs 或 dfu.bin）
spiaddr = 0x800000                # 外部 SPI Flash 8M 偏移（golden image 槽）
# run 默认 54 = exFlash Erase,Program,Verify Arora V（--fsFile 通吃 .fs 与 .bin 位流）。
# 切勿用 55/56：那是 RISC-V 软核固件的 "C Bin"，烧 FPGA 位流会写 0 字节、Verify 失败。

[programmer.efuse]                # eFuse AES 密钥；缺省 → 烧空板无 eFuse 前置步骤
key_file = "firmware/efuse.ekey"
```

各 `products/<id>/programmer.toml` 通常只需：

```toml
schema_version = 1
[programmer]
device = "GW5AT-15A"           # 唯一各产品不同、必填、无默认；32U3 = "GW5AT-60B"
# 需要时才覆盖默认（cable_candidates 整体覆盖；[programmer.flash]/[programmer.efuse] 按键合并）：
# cable_candidates = [5, 4, 1]
# [programmer.switch]          # DFU↔APP 保底切换（产品不支持 USB RECONFIG 时）
# dfu2app = ["--run", "52", "--fsFile", "firmware/app_sram.fs"]  # 含 "/" 按本产品目录解析
# app2dfu = ["--run", "52", "--fsFile", "firmware/dfu.fs"]
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
| `未找到烧录器 CLI` | Linux 把 Gowin Programmer AppImage 放进 `bin/`；Windows 把 Programmer 文件夹拷进 `bin/Programmer/`；或在共享 `resources/programmer.toml` 改 `cli`/`cli_windows` |
| `DFU 镜像缺失` | 把空板位流放到 `products/<id>/firmware/`（16U3=dfu.fs / 32U3=dfu.bin） |
| `eFuse 密钥文件缺失` | 把 `efuse.ekey` 放到 `products/<id>/firmware/`（缺失时烧空板的 eFuse 前置步骤会失败） |
| `未找到 sigrok-cli 二进制` | 按平台命名放入 `bin/`（见上文） |
| `未找到 libusb 后端（libusb-1.0.dll）` | Windows：把 `libusb-1.0.dll` 放进 `bin/`（见跨平台一节），并给设备装 WinUSB 驱动 |

> **GUI 资源路径可临时覆盖**：`测试参数`页的"资源路径覆盖"三行分别显示当前档案解析出的
> app 固件 / DFU 镜像 / eFuse 密钥路径（灰字 placeholder），可点 `…` 浏览换用别的文件，
> **仅本次会话生效、不写回 TOML**——方便临时验证不同固件/位流/密钥。

## 跨平台（Linux / Windows）

- 烧录器 cli 集中在共享 `resources/programmer.toml`，支持 `cli` / `cli_windows` /
  `cli_darwin`（各产品档案也可各自覆盖）。默认路径版本无关，便于升级替换；
- **Windows 烧录器**：把整个 Gowin `Programmer` 文件夹拷进 `bin/Programmer/`（相当于
  Linux 的软链，只是 Windows 用拷贝），默认即用 `bin/Programmer/bin/programmer_cli.exe`，
  通常无需改配置；放在别处则在共享 `programmer.toml` 改 `cli_windows`。
- **libusb 后端**：本工具用 pyusb 枚举 USB 设备（DFU/APP 检测、RECONFIG 切换）。
  - 推荐**把 `libusb-1.0.dll` 内置到 `resources/bin/`**——LGPL 允许随项目分发，工位免装依赖；
    工具会**优先加载这个内置 DLL**（见 `slogicpt/device_watch.libusb_backend`），`build.py`
    也会自动把它打包进 exe。`.gitignore` 已放行该文件，可 `git add` 入库随项目走。
  - 或装到系统 / 放 exe 同级。**无论哪种，都需给设备装 WinUSB 驱动**（本设备带 MS OS 2.0
    描述符，Win10+ 通常自动装 WinUSB；否则用 Zadig 手动指定 WinUSB）。
  - 缺后端时 GUI 顶栏会**显式红字告警**"未找到 libusb 后端"，不再静默显示"无设备"。

- **Linux 权限**：libusb 通常能枚举设备，但读序列号 / 发 DFU↔APP 的 RECONFIG 控制传输
  需要访问权限。缺 udev 规则、且用户不在 `uucp`（Arch）或 `dialout`（Debian/Ubuntu）组时会
  报 `LIBUSB_ERROR_ACCESS`。给 VID 0x359F 加一条 udev 规则：
  ```
  # /etc/udev/rules.d/60-sipeed-slogic.rules
  SUBSYSTEM=="usb", ATTR{idVendor}=="359f", MODE="0660", GROUP="uucp", TAG+="uaccess"
  # Debian/Ubuntu 改 GROUP="dialout"
  sudo udevadm control --reload-rules && sudo udevadm trigger   # 之后重新插拔设备
  ```
  （症状：设备能被检测到，但切换模式或读序列号报权限错误。）

### Windows 工位放置清单

| 项 | 放置 |
|---|---|
| Python（**仅源码运行时**需要，跑打包好的 exe 不需要） | **3.11 或更高**（依赖标准库 tomllib；低版本启动即报错退出） |
| sigrok-cli | `bin/sigrok-cli-windows-x86_64.exe` |
| Gowin 烧录器 | 把 Gowin `Programmer` 文件夹拷进 `bin/Programmer/`（默认即用其 `bin/programmer_cli.exe`） |
| libusb 后端 | `bin/libusb-1.0.dll`（推荐内置入库；`build.py` 自动打包进 exe）+ 设备 WinUSB 驱动（Zadig） |
| 固件资源 | `products/<id>/firmware/` 下的 app.bin / dfu.* / efuse.ekey |

## 打包分发

`python build.py`（PyInstaller，需 **Python 3.11+** 解释器）产出单二进制，与本
`resources/` 目录平级摆放。Windows 上若 `bin/libusb-1.0.dll` 存在则自动打包进 exe：

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
