# SLogic-tools（产测工具）

Sipeed SLogic 系列逻辑分析仪产线产测工具，**数据驱动多产品**：SLogic16U3、SLogic32U3 及未来产品。
新增产品 = 在 `resources/products/` 加一份 TOML 档案 + 按约定放置固件与刷机资源，**无需改代码**（详见 `resources/README.md`）。

## 组件

- `pt/` — 产测 GUI（PyQt5）：设备识别（APP/OTA 模式）、采样验证、OTA 烧写、空板刷机、一键全流程
- `ota/` — OTA 模式下经 USB-SPI 烧写 SPI Flash 的库与命令行（`spi_flash.py`，支持 `--vid/--pid/--addr/--verify`）
- `resources/` — 管理员维护的产品档案与资源（产品 TOML、固件、blank_flash manifest、sigrok-cli 二进制）
- `cli/` — **已废弃**（旧 slogic_cli，仅作历史参考）；采样改用 sigrok-cli

## 环境准备

1. 创建虚拟环境并安装依赖：
   ```bash
   python -m venv .venv
   source .venv/bin/activate        # Windows: .\.venv\Scripts\Activate.ps1
   pip install -r ota/requirements.txt
   pip install -r pt/requirements.txt
   ```
   需要 Python 3.11+（使用标准库 tomllib）。

2. 放置资源（详见 `resources/README.md`）：
   ```bash
   # sigrok-cli（来自 SLogic 发布包，按平台命名）
   ln -sf /path/to/sigrok-cli-SLogic-linux-x86_64-musl.AppImage resources/bin/sigrok-cli-linux-x86_64

   # 应用固件
   cp app_firmware.bin resources/firmware/slogic16u3/app.bin

   # 空板刷机脚本（gowin 工具链脚本放入 manifest 所在目录）
   cp efuse_lock.sh gowin_flash.sh usb_rst.sh resources/blank_flash/slogic16u3/
   ```
   GUI 启动时自检，缺什么会在横幅中逐条列出并禁用相应功能。

## 运行产测 GUI

```bash
.venv/bin/python pt/src/gui.py    # 任意工作目录均可，路径不依赖 cwd
```

产测流程（一键 FULL TEST，也可单步执行）：

1. **空板刷机**：执行 manifest 声明的命令（如 Gowin 烧录 OTA 固件），完成后设备自动以 OTA 模式枚举
2. **OTA 烧写**：经 USB-SPI 将应用固件写入产品档案指定地址并回读校验
3. **等待应用设备**：默认自动等待；超时则横幅提示操作员插拔，检测到后自动继续
4. **采样验证**：按档案测试点（如 16ch@200M）经 sigrok-cli 采样，与外置固定信号源
   （默认 10MHz 50% TTL 接入全部通道）比对频率/占空比，显示 PASS / FAIL

## 命令行自测（无需 GUI）

```bash
.venv/bin/python pt/src/profiles.py       # 档案加载与资源自检
.venv/bin/python pt/src/waveform.py       # 波形拆包/验证自测（含 32ch）
.venv/bin/python pt/src/sigrok.py         # 真机采样 + 验证
.venv/bin/python pt/src/device_watch.py   # 设备插拔监视
.venv/bin/python ota/src/spi_flash.py firmware.bin --pid 0x30F1 --addr 0x0   # OTA 烧写
```

## 新增产品清单

1. `resources/products/<id>.toml`（复制现有档案修改：VID/PID、通道数、带宽、采样率档位、测试点、固件地址）
2. `resources/firmware/<id>/app.bin`
3. `resources/blank_flash/<id>/manifest.toml` + 刷机脚本
4. OTA PID 未定时可先不填 `usb.ota_pid`，GUI 会提示"OTA 未配置"，其余功能可用
