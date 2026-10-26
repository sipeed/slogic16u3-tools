# resources/ 资源目录约定

本目录由**管理员**按以下约定放置文件，产测 GUI 启动时自检并提示缺失项。
新增产品 = 新增一个目录 `products/<product_id>/`（含 `product.toml`）
并放置对应固件资源，**无需修改任何代码**。

```
resources/
├── products/<product_id>/        # 一产品一目录，目录名 = product_id
│   └── product.toml              # 产品档案（USB PID / 通道 / 带宽 / 测试计划），公开入库
├── fixture/
│   └── testgen.fs                # 信号源夹具（Tang Nano 9K）位流，各产品共用，入库
├── fixture.toml                  # 夹具 USB 身份（VID/PID + 序列号关键字），各产品共用，入库
└── bin/                          # 跨平台工具二进制
    ├── sigrok-cli-linux-x86_64            # sigrok-cli（须 SLogic 2.3.0 及以上）：Linux AppImage 重命名/软链
    ├── sigrok-cli-windows-x86_64.exe      #   Windows
    ├── openFPGALoader/openFPGALoader.exe  # Windows：夹具位流载入（Linux 用发行版的 openFPGALoader）
    └── libusb-1.0.dll                     # Windows：pyusb 后端（见下文跨平台）
```

空板的 JTAG 烧录（eFuse 密钥 + DFU 底包）不在本工具内，由另行提供的工具完成。

## 放置示例（Linux 工位）

```bash
# sigrok-cli（来自 SLogic 发布包，须 2.3.0 及以上：旧版 4ch@1400M 会回卷、8ch@800M 收不到数据）
ln -sf /path/to/sigrok-cli-SLogic-2.3.0-linux-x86_64.AppImage resources/bin/sigrok-cli-linux-x86_64
```

夹具位流 `fixture/testgen.fs` 随仓库分发，不用另外放置。更新夹具 RTL 后，把新构建的
`impl/pnr/testgen.fs` 覆盖到这里并提交即可，32U3 / 16U3 档案都引用这一份。

## fixture.toml：夹具 USB 身份

夹具（Tang Nano 9K）的 VID/PID 和序列号关键字集中在这里，32U3 / 16U3 共用：

```toml
schema_version = 1

[fixture]
vid = 0x0403
pid = 0x6010
serial_hints = ["BL702", "FACTORYAIOT"]
```

- 夹具和 Gowin USB 烧录器都是 `0403:6010`，所以 `serial_hints` 用来区分：
  序列号含任一关键字（不区分大小写）才算夹具。
- **新批次 VID/PID 变了**：改 `vid` / `pid`；**序列号变了**：往 `serial_hints`
  里加一条。不用改代码，也不用重新打包；改完重启工具生效。
- 改 VID/PID 后，Linux udev / Windows WinUSB 驱动要按新 ID 重新部署一次
  （「环境准备」页读的是同一个文件）。
- `product.toml` 不再写夹具的 `vid/pid`；没有 `fixture.toml` 时回退到代码内置默认值，
  旧资源目录仍能启动。

## product.toml：产品档案

主要段落（以 32U3 为例，完整注释见文件本身）：

```toml
[usb]
links = ["10G", "5G"]      # 产测逐轮链路：先 10G 全测，提示换插 5G 口后再全测；第一轮是门槛

[signal_source]            # 夹具位流 / 串口参数；VID/PID 与序列号关键字见 fixture.toml
firmware = "../../fixture/testgen.fs"   # 必填，相对本产品目录；本次启动首次用到时载入 SRAM，不应答时重载

[[capture.threshold_groups]]   # 1.8 V 的 bank 3 通道另用 0.9 V 阈值再采一次
channels = [20, 21, 22, 23, 24, 25]
voltage_threshold_v = 0.9

[[plan]]                   # 每个夹具图案模式一段；期望值由 slogicpt/patterns.py 按 RTL 推导
mode    = 1
label   = "4ch SPI @54 MHz"
checks  = ["protocol"]
configs = [                # 每轮链路一组配置（5G 下驱动限速：32U3 32ch 最高 100M）
  { channels = 4, samplerate = "1400M", window_ms = 2, link = "10G" },
  { channels = 4, samplerate = "800M",  window_ms = 2, link = "5G" },
]
```

档案自检会检查：每轮链路下每个启用的模式都有对应配置（否则启动即报错，避免某轮悄悄少测）。

## 保密边界（重要）

本仓库是公开仓库，以下内容**仅限工厂产测工位本地放置，禁止提交入库或以任何形式公开**
（.gitignore 已强制忽略）：

- `products/<id>/firmware/*.fs`、`*.bin` — 产品固件、DFU 位流等（夹具位流 `fixture/testgen.fs` 不在此列，入库）
- `products/<id>/firmware/*.ekey`、`efuse_key.txt` — eFuse AES 密钥（机密；本工具已不用，
  .gitignore 仍保留规则以防误提交）

`product.toml` 只声明接口契约（不含固件/位流实现），公开入库。

## 启动警告 → 解决办法对照

GUI 顶栏的警告角标里的每一条都对应一个待放置/待确认项，补齐后重启即消：

| 警告 | 解决办法 |
|---|---|
| `未找到 sigrok-cli 二进制` | 按平台命名放入 `bin/`（见上文） |
| `未找到 libusb 后端（libusb-1.0.dll）` | Windows：把 `libusb-1.0.dll` 放进 `bin/`（见跨平台一节），并给设备装 WinUSB 驱动 |

夹具位流 `testgen.fs` 不在启动自检里：缺失时图案测试一步报"找不到夹具位流"。

夹具载入时机：本次启动后第一次用到夹具时把 `testgen.fs` 载入夹具 SRAM，之后换板测试不再载入；
夹具串口不应答（被拔插、掉过电，SRAM 配置已丢）时自动重新载入。点「复位」也会检查夹具，
不应答同样会重新载入。不写夹具 Flash：openFPGALoader 写 GW1NR-9C 片上 Flash 报
`CRC check : FAIL`，写后不启动。

## 跨平台（Linux / Windows）

- **Windows 夹具载入（openFPGALoader）**：夹具（Tang Nano 9K）的位流由 openFPGALoader 按
  USB bus/device 载入。工位放置：
  1. 把 `openFPGALoader.exe` 放到 `bin/openFPGALoader/`。**这是一个静态单文件 exe，
     不依赖任何第三方 DLL**（libusb/libftdi/zlib 及 C/C++ 运行库全部静态链接，仅用系统自带
     的 KERNEL32 / UCRT `api-ms-win-crt-*`），解压即用；
  2. **一次性**给夹具 FTDI 的 A 通道装 WinUSB 驱动——**推荐在 GUI 底部
     「环境准备」页点「部署」一键完成**（弹 UAC 授权，内部即调用同目录的 `wdi-simple.exe`）；
     也可命令行免点击：
     `bin\openFPGALoader\wdi-simple.exe -v 0x0403 -p 0x6010 -i 0 -t 0 -n "USB Serial Converter A (WinUSB)"`
     （这里的 `0x0403/0x6010` 要与 `fixture.toml` 的 `vid/pid` 一致）
     需要还原时点同处的「移除」（或用设备管理器删掉该 WinUSB 驱动）；

  > openFPGALoader 官方 v1.1.1 GitHub release 只提供 MSYS2 动态包（exe + libftdi1 /
  > libusb-1.0 / zlib1 / libgcc / libstdc++ 共 5~6 个 DLL），**不是单文件**。此处的
  > 单文件 exe 是用同一份 v1.1.1 源码经 llvm-mingw(UCRT) 全静态交叉编译而来，行为一致。

  **`bin/openFPGALoader/` 下两个二进制的来源/许可**（工位本地放置，不入库）：
  - `openFPGALoader.exe`：本体 Apache-2.0（[trabucayre/openFPGALoader](https://github.com/trabucayre/openFPGALoader)
    v1.1.1，**源码未改**）；静态链入 libusb-1.0 / libftdi1（LGPL-2.1）、zlib（Zlib 许可）。
    构建：llvm-mingw(UCRT) 交叉工具链 + 项目自带 `Toolchain-x86_64-w64-mingw32-clang.cmake`
    （`BUILD_STATIC=ON`、`WINDOWS_CROSSCOMPILE=ON`、`CROSS_COMPILE_DEPS=ON`，libftdi 加
    `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`、zlib 静态版另建）。LGPL 组件源码均为上游公开、
    未改动，构建方式如上——满足"可重链/可复现"。
  - `wdi-simple.exe`：libwdi 的 CLI 示例（[pbatard/libwdi](https://github.com/pbatard/libwdi)，
    LGPL-3，**未改动**），从 Gowin USB Cable Driver 安装包（NSIS）提取。
- **libusb 后端**：本工具用 pyusb 枚举 USB 设备（识别产品、APP/DFU 模式、链路速率）。
  - 推荐**把 `libusb-1.0.dll` 内置到 `resources/bin/`**——LGPL 允许随项目分发，工位免装依赖；
    工具会**优先加载这个内置 DLL**（见 `slogicpt/device_watch.libusb_backend`），`build.py`
    也会自动把它打包进 exe。`.gitignore` 已放行该文件，可 `git add` 入库随项目走。
  - 或装到系统 / 放 exe 同级。**无论哪种，都需给设备装 WinUSB 驱动**（本设备带 MS OS 2.0
    描述符，Win10+ 通常自动装 WinUSB；否则用 Zadig 手动指定 WinUSB）。
  - 缺后端时 GUI 顶栏会**显式红字告警**"未找到 libusb 后端"，不再静默显示"无设备"。

- **Linux 权限**：libusb 通常能枚举设备，但读序列号、sigrok 采集与 openFPGALoader 载入夹具
  需要访问权限。缺 udev 规则时会报 `LIBUSB_ERROR_ACCESS`。**推荐在 GUI 底部的
  「环境准备」页点「部署」一键装好**（弹 polkit 授权，内部经 `pkexec` 写入下面的规则并
  `udevadm reload`）；点「移除」即删除。也可手动：
  ```
  # /etc/udev/rules.d/60-sipeed-slogic.rules
  SUBSYSTEM=="usb", ATTR{idVendor}=="359f", MODE="0660", TAG+="uaccess"
  # openFPGALoader 载入夹具用的 FTDI（Tang Nano 9K 的 BL702）
  SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="6010", MODE="0660", TAG+="uaccess"
  sudo udevadm control --reload-rules && sudo udevadm trigger   # 之后重新插拔设备
  ```
  （`TAG+="uaccess"` 让当前登录用户获得访问权，跨发行版通用；症状：设备能被检测到，
  但读序列号、采集或载入夹具报权限错误。之后重新插拔设备生效。）

### Windows 工位放置清单

| 项 | 放置 |
|---|---|
| Python（**仅源码运行时**需要，跑打包好的 exe 不需要） | **3.11 或更高**（依赖标准库 tomllib；低版本启动即报错退出） |
| sigrok-cli | `bin/sigrok-cli-windows-x86_64.exe`（SLogic 2.3.0 及以上） |
| openFPGALoader | `bin/openFPGALoader/openFPGALoader.exe` + 夹具 FTDI A 通道 WinUSB（「环境准备」一键部署） |
| libusb 后端 | `bin/libusb-1.0.dll`（推荐内置入库；`build.py` 自动打包进 exe）+ 设备 WinUSB 驱动（Zadig） |

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

- `usb.dfu_pid`（16U3=0x30F1、32U3=0x30F2）可选：只用于把"还在 DFU、没烧 app 固件"的板子认出来并提示；应用固件 OTA 由批量网页工具负责；
- `[signal_source]` 与 `[[plan]]` 必填：每个产品都用夹具图案测试；
- `capture.samplerates` 必须填 sigrok 驱动实际支持的档位（`sigrok-cli -d <driver> --show` 查询）；
  `[[plan]]` 里每组配置的采样率必须在这个列表里，且满足 `channels × rate / 8 ≤ max_bandwidth_mbps`，否则启动即报错；
- `[usb] links` 决定产测轮数与门槛；`[[plan]]` 每组 `configs` 的 `link` 标注它属于哪一轮。
