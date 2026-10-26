# SLogic-tools（产测工具）

Sipeed SLogic 系列逻辑分析仪产线产测工具，**数据驱动多产品**：SLogic16U3、SLogic32U3 及未来产品。
新增产品 = 在 `resources/products/` 加一份 TOML 档案 + 按约定放置夹具位流（有夹具的产品），**无需改代码**（详见 `resources/README.md`）。

## 产线工位

| 工位 | 工具 | 接什么 | 做什么 |
|---|---|---|---|
| ① JTAG 烧录 | 另行提供（不在本工具内） | Gowin 烧录器 + 空板 | eFuse 写入 AES 密钥 → 烧 DFU 底包 |
| ② OTA 烧应用固件 | 批量 OTA 网页工具 https://slogic.sipeed.com/ota/ | 多块 DFU 板（USB2 即可） | 并行烧 app 固件；前提是①已完成（DFU 底包 + 已加密） |
| ③ 产测 | **本工具** | 被测板（运行 app 固件）+ 信号源夹具 + 一个 5G 口 | 32U3：先经 USB RECONFIG 切到 DFU 再切回 APP → 10G 链路全测 → 提示换插 5G 口 → 5G 链路再全测，全部通过才 PASS；16U3：同一夹具（转接线接 CH0..CH15），5G 链路测一轮 |

本工具只负责③产测。点 **开始** 即"布防"：每接入一块被测板，
工具自动识别产品并跑完全部步骤；结论以大卡片显示在窗口中央，直到下一块板接入。点 **停止** 撤防（运行中则取消）。
撤防时把鼠标移到步骤条上的某一步、点击即可对最近识别的板单步执行。

界面：顶栏（语言 / 主题 / 复位 / 开始·停止）→ 产品与设备状态（含本次启动以来的通过/失败计数）→ 横向步骤条
→ 中央结论区 → 底部可展开的「报告 / 运行日志 / 环境准备」。中央结论区按状态显示：

| 状态 | 显示 |
|---|---|
| 待机 / 等待接入 | 就绪清单：分析仪（台数、链路速率）、夹具串口、sigrok-cli、设备权限（Linux udev），缺什么一眼可见 |
| 执行中 | 当前在做什么（哪个模式、通道数 × 采样率、阈值）+ 进度条；**模式矩阵**（每轮链路 × 各模式）逐格变成通过/失败 |
| 需要操作 | 操作提示（如"换插 5G 口"），卡片描边呼吸提示 |
| PASS / FAIL | 结论 + 产品·SN·链路·耗时，模式矩阵保留（悬停失败格子看原因） |

- **只有通过 / 失败两种结论**：判不了（采不到、解码不出、测不了）一律算失败，没有"跳过""覆盖"之类的中间状态。
- **失败原因就近显示**：失败步骤下方直接写简要原因（哪个模式、哪几路），结论卡片同步显示；完整明细在「报告」里。
- **重试**：失败步骤下方有「重试」按钮，由操作员决定是否重跑（只在撤防、或布防后板子还没拔时可用），
  会连同它后面依赖它的待执行步骤一起重跑（如 10G 某步重试通过后继续换插 5G）。工具**不会自己重试失败的步骤**；
  唯一的自动重试是采集时主机侧的瞬时错误（USB 传输/超时类），最多 3 次，并在报告里注明。分析仪卡死（stall）不自动重试。
- **复位**：清空结果回到待机，并复位台架：结束残留的 sigrok-cli、检查夹具（不应答则重新载入位流）并切回 M0。
  分析仪**不做** USB 复位（实测 32U3 在 10G 口上被复位后会掉线，需重新拔插）；分析仪异常请人工拔插。
底栏展开后拖动它上方的小把手可调高度（会记住）。
主题默认跟随系统明暗，也可在顶栏手动切换（会记住）；切换语言或主题不会丢失当前步骤状态、结论与日志。

- **DFU/APP 来回切换**（`[usb] reconfig = true`，目前只有 32U3）：第一步向板子发 USB RECONFIG 切到 DFU、再发一次切回 APP，
  每次都要在 30 s 内以目标模式重新枚举、且是主机上唯一的 SLogic，切回 APP 后 USB 序列号（固件标识）不变；
  失败即 FAIL，不再测 Mx。实测一次来回 Linux 约 1.5 s、Windows 约 2.3 s。16U3 不支持 RECONFIG，没有这一步。
- **逐台测**：sigrok-cli 无法在多台分析仪中选定一台（驱动不支持 `conn=`），所以主机上同时只能有一台被测分析仪；多于一台时工具会提示而不开测。
- **逐轮链路测试**（`product.toml` 的 `[usb] links`，如 32U3 = `["10G", "5G"]`）：第一轮是门槛，达不到直接 FAIL、不进入图案测试；之后每轮在结论卡片上提示把线换到对应速率的口，检测到该速率重新枚举后自动继续（固件暂无降速命令，换口是人工的）。各轮用各自的采集配置（`[[plan]]` 的 `configs` 带 `link` 标签：5G 下驱动限速，32U3 为 4ch@800 / 8ch@400 / 16ch@200 / 32ch@100）。
- 5G 轮的接法：**换一根只支持 5G 的线直插主机口**（或主机原生 USB 3.0 口），**不要经过 Hub**。实测经 Hub 接 5G 时，M5（旧夹具位流下 27 路 1 kHz 同时翻转；2026-10-20 起相邻反相）会因地弹在所有通道同一时刻出约 30 ns 毛刺，恒低的 CH1 被读成高、1 kHz 读成 2 kHz；同一块板直连则 10G/5G 都干净。
- 夹具 GND 与分析仪 GND 之间要有**短而粗的地线**。M5 时过时不过先查接地：M5 下用 16ch@200M、1.5 V 采 4 ms，恒低的 CH1 应 0 个边沿、1 kHz 的 CH5 应 8 个。
- **夹具位流载入 SRAM**：本次启动后第一次用到夹具时，把 `resources/fixture/testgen.fs` 载入 Tang Nano 9K 的
  SRAM（约 1.5 s），之后换板测试不再载入；夹具串口不应答（被拔插、掉过电）时自动重新载入。
  不写夹具 Flash：openFPGALoader 写 GW1NR-9C 片上 Flash 报 `CRC check : FAIL`，写后不启动。
- 结果只在界面显示（报告区可一键复制），不存档。

## 目录结构

```
├── slogicpt/            # 产测应用（唯一代码目录）
│   ├── ui/              #   工站界面（Material 3 风格，仅依赖 PyQt5）
│   │   ├── app.py       #     入口：主题、语言、全局异常兜底（槽函数异常只记日志，不退出）
│   │   ├── main_window.py #   组装：顶栏 / 步骤条 / 结论区 / 报告·日志·环境准备；重试、复位
│   │   ├── station.py   #     布防状态机（与界面分离）、手动重试、台架复位
│   │   ├── widgets.py   #     步骤条（含重试按钮）、结论卡片、就绪清单、模式矩阵、Chip、底部抽屉、采集参数对话框
│   │   ├── report.py    #     报告文本
│   │   ├── theme.py     #     明/暗色 token、调色板与样式表、系统明暗检测
│   │   └── icons.py     #     内嵌 SVG 图标
│   ├── procutil.py      #   跨平台子进程：隐藏窗口、杀进程树、超时/取消运行器
│   ├── env_setup.py     #   「环境准备」一键部署：Linux udev 规则 / Windows 夹具 FTDI 的 WinUSB 驱动
│   ├── i18n.py, _translations.py  # 中英界面文案
│   ├── pipeline.py      #   流程引擎：步骤表、链路门槛、采集瞬时错误重试、超时/人工提示
│   ├── profiles.py      #   产品档案加载/校验/资源自检
│   ├── device_watch.py  #   USB 设备检测（产品 + APP/DFU 模式 + 链路速率）
│   ├── patterns.py      #   夹具图案的期望值（唯一权威，由 RTL 推导）
│   ├── checks.py        #   判定层：逐通道 + 结构化判定（不滤波；判不了即 FAIL）
│   ├── deep_checks.py   #   内容级检查：SPI/I2C/UART 解码、突发包络
│   ├── fixture.py       #   夹具（Tang Nano 9K）位流载入 SRAM（本次启动首次用到时 / 不应答时）
│   ├── fpga.py          #   夹具串口：切模式、存活检查
│   ├── sigrok.py        #   sigrok-cli 采样封装（logic_channels 分组、回卷检测）
│   └── waveform.py      #   波形拆包（任意通道数）与接线置换
├── resources/           # 管理员资源：产品档案 / 夹具位流 / sigrok-cli（见其 README）
├── out/                 # 运行时采样输出（自动创建，不入库）
├── build.py             # PyInstaller 单二进制打包
├── requirements.txt
└── TODO.md              # 待验证 / 固件侧 / 台架侧的未完成事项
```

## 快速开始

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt                     # 需 Python 3.11+
# 放置 sigrok-cli（夹具位流已随仓库），见 resources/README.md
python -m slogicpt                                  # 启动 GUI，点「开始」
```

台架要点：

- **sigrok-cli 须用 SLogic 2.3.0 及以上**：旧版在 4ch@1400M 会回卷到 1600M、8ch@800M 收不到数据。
- 产测台架不要接 Gowin 烧录器：它和夹具（BL702）同为 FTDI `0403:6010`，其 JTAG 操作还会干扰夹具 FPGA 的 SRAM 配置。
- 产品识别：按 USB PID。
- 夹具识别：VID/PID 与序列号关键字在 `resources/fixture.toml`（各产品共用）；换批次/换固件只改这个文件，不用改代码。
- 夹具位流载入用 openFPGALoader：Linux 用发行版包；Windows 放 `resources/bin/openFPGALoader/openFPGALoader.exe`，
  并在「环境准备」里一键给夹具 FTDI 装 WinUSB 驱动（见 resources/README.md）。

## 命令行

```bash
python -m slogicpt.profiles      # 档案加载与资源自检
python -m slogicpt.device_watch  # 设备插拔监视（含链路速率）
python -m slogicpt.pipeline      # 流程引擎自测（步骤表、链路判据）
python -m slogicpt.patterns      # 夹具图案期望值自测
python -m slogicpt.checks        # 判定层自测
python -m slogicpt.deep_checks   # SPI/I2C/UART 解码、突发包络自测
python -m slogicpt.waveform      # 波形拆包自测
python -m slogicpt.fpga          # 找到夹具串口并切换模式（需接夹具）
python -m slogicpt.fixture       # 夹具载入自测
python -m slogicpt.sigrok        # 查找 sigrok-cli、扫描并试采一次（需真机）
```

## 打包分发

```bash
pip install pyinstaller
python build.py     # 产出 dist/slogic-pt-<platform>，与 resources/ 平级摆放分发
```
