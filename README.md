# SLogic-tools（产测工具）

Sipeed SLogic 系列逻辑分析仪产线产测工具，**数据驱动多产品**：SLogic16U3、SLogic32U3 及未来产品。
新增产品 = 在 `resources/products/` 加一份 TOML 档案 + 按约定放置固件与刷机资源，**无需改代码**（详见 `resources/README.md`）。

## 目录结构

```
├── slogicpt/            # 产测应用（唯一代码目录）
│   ├── gui.py           #   工站界面：线性测试序列（一键/单步）+ 日志/参数/报告 + 状态横幅
│   ├── pipeline.py      #   流程引擎（步骤/超时/人工介入提示）
│   ├── profiles.py      #   产品档案加载/校验/资源自检
│   ├── sigrok.py        #   sigrok-cli 采样封装（logic_channels 分组、回卷检测）
│   ├── waveform.py      #   波形拆包（任意通道数）与 PWM 验证
│   ├── device_watch.py  #   USB 设备检测（产品 + APP/DFU 模式）
│   ├── blank_flash.py   #   manifest 驱动的空板刷机
│   ├── flasher.py       #   OTA 烧写封装
│   └── dfu/             #   DFU（OTA 模式）USB-SPI Flash 烧写库 + CLI
├── resources/           # 管理员资源：产品档案 / 固件 / 刷机脚本 / sigrok-cli（见其 README）
├── out/                 # 运行时采样输出（自动创建，不入库）
├── build.py             # PyInstaller 单二进制打包
├── requirements.txt
└── TODO.md              # 待交付事项（资源放置 / 固件侧 / 工位侧）
```

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt                     # 需 Python 3.11+
# 放置资源（sigrok-cli / 固件 / 刷机脚本），见 resources/README.md
python -m slogicpt                                  # 启动产测 GUI
```

产测流程（一键从头到尾自动执行，或在序列列表逐步点 ▶）：

1. **烧空板**：执行 manifest 声明的命令（Gowin/openFPGALoader 烧入 OTA 固件），完成后设备以 DFU 模式（0x359F:0x30F1 "SLogic DFU"，各产品共用）枚举
2. **OTA 烧写**：经 USB-SPI 将应用固件写入档案指定地址并回读校验
3. **等待应用设备**：自动等待；超时则横幅提示操作员插拔，检测到后自动继续
4. **采样验证**：按档案测试点经 sigrok-cli 采样（自动切 logic_channels 分组），与外置固定
   信号源（默认 10MHz 50% TTL）比对每通道频率/占空比 → PASS / FAIL；
   结果报告可一键复制用于问题反馈

## 命令行

```bash
python -m slogicpt.dfu.spi_flash firmware.bin --pid 0x30F1 --addr 0x0   # DFU 烧写 CLI
python -m slogicpt.profiles      # 档案加载与资源自检
python -m slogicpt.waveform      # 波形拆包/验证自测（含 32ch）
python -m slogicpt.sigrok        # 真机采样 + 验证
python -m slogicpt.device_watch  # 设备插拔监视
python -m slogicpt.pipeline      # 流程引擎自测
```

## 打包分发

```bash
pip install pyinstaller
python build.py     # 产出 dist/slogic-pt-<platform>，与 resources/ 平级摆放分发
```
