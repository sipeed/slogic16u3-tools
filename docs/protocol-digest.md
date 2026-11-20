# SLogic USB 协议要点（本工具用到的部分）

来源（2026-11-13 吸收，原文在 SLogic 仓库 / 下载区）：

- `slogic-protocol-1113.md` — libslogic 规范，2026-11-12 修订，新增 DDR3 测试寄存器
  （`R32_DDRTST` 0x0050）与 USB2 回退（U2）寄存器组；
- `20261112-ddr3-test-mode-usage.md` — DDR3 测试模式使用文档（v1，对应 DDR3-test-v1 port）。

本文只摘录**本产测工具直接依赖或需要判断**的事实；抓取数据流本身由 libsigrok 驱动实现，
本工具通过 sigrok-cli 使用，不自己实现。

## 设备与链路

| 产品 | VID | 运行 PID | DFU PID | 通道 | 链路 |
|---|---|---|---|---|---|
| SLogic8U2 | 0x359F | 0x3000（旧固件 0x0300） | 0x30F0 | 8 | USB2 HS |
| SLogic16U3 | 0x359F | 0x3031 | 0x30F1 | 16 | 5G |
| SLogic32U3 | 0x359F | 0x3032 | 0x30F2 | 32 | 10G |

通道模式与最高采样率（与 `product.toml` 的 `[[plan]]` 一致）：

| 模型 | 4ch | 8ch | 16ch | 32ch |
|---|---|---|---|---|
| 16U3 | 800M | 400M | 200M | — |
| 32U3 | 1400M | 800M | 400M | 200M |

- U3 在已知低于 USB3 的链路上限速 `320 MHz / 通道数`（U2 回退时生效）。
- 显示名 = 链路前缀 + 型号，如 `(10G) SLogic32 U3`；U2 回退时仍是同一 VID:PID 和串号，
  产品串为 `SLogic32 U2`。

## U3 寄存器（vendor 控制传输，每次 32-bit）

```
READ  bmRequestType=0xC0 bRequest=0x00 wValue=index<<2 wLength=4  timeout=500ms
WRITE bmRequestType=0x40 bRequest=0x01 wValue=index<<2 wLength=4
```

| 寄存器 | 地址 | 本工具用途 |
|---|---|---|
| R32_CTRL | 0x0004 | bit0 RUN（采集进行中）；bit1 RST。DDR 自测启用前检查 RUN=0，并写 0 释放 RST |
| R32_FLAG | 0x0008 | bit1 采集溢出（sticky，写 1 清除；RST 也清）。sigrok 2.3.x 会据此报 `end=error:overflow` |
| R32_AUX | 0x000C | 配置邮箱（通道/采样率/阈值/图案），由驱动使用 |
| R32_ADC_TEMP | 0x0048 | [16] valid，[15:0] 有符号 Q2 ℃；掉电/PLL 关闭时计数冻结 |
| R32_DBG_STATE | 0x004C | [5] 是否上报链路速度，[4:3] 链路（2=SS，3=SSP），[2] VBUS，[1] ddr_off，[0] pll_off |
| R32_DDRTST | 0x0050 | DDR3 自测：读状态、写 enable/clear（见下） |
| 0x0018 / 0x001C / 0x0020 | — | 固件诊断，**禁止读写**（0x001C 写会强制 DDR 复位/PLL 掉电） |

## DDR3 自测模式（R32_DDRTST = 0x0050）

- 适用：usb2_integration 固件，串号（固件标识）`202611120832` 起；2.3.x 发布固件没有（读 0）。
- 引擎自行扫描整个 DDR3：bank 0–7 → row 0–16383 → 128 beat 列扫描（字地址 0,8,…,1016），
  每 beat 写 128 bit 图案再读回比对（8 个 16-bit slot）；每完成一圈 16 位计数 +1，约 **391 次/秒**。
- 读 `0x0050`：[31:16] 循环计数（仅 DDR 硬复位清零，stop 后冻结）；[15:8] 逐 slot sticky
  （bit `8+i` = slot i 比对错）；[3] busy；[2] calib_ok；[1] enable；[0] sticky（lane 或
  stall watchdog ~7.9 ms 或校准超时 ~13.7 s）。
- 写 `0x0050`：[0] enable 电平；[1] clear toggle（每次写 bit1=1 清全部 sticky，同写 bit0 仍生效，
  所以 `0x3`=清并保持启用，`0x2`=清并停用）。RST 有效时写被忽略。
- **与采集互斥**，RTL 无互锁：采集中启用会停 ring、丢在途读、复位 ring 引擎。主机必须先确认
  `R32_CTRL[0]=0`。本工具在启用前检查，采集进行中直接拒绝。
- 推荐流程（本工具 `slogicpt/ddr.py` 照此实现）：停采集 → CTRL=0 → 写 0x0050=1 → 轮询
  calib_ok 与计数前进 → 监视 → 写 0x0050=0 → 写 0x0050=2 清除 → 读冻结计数。

## U2（USB2 回退）模式

- 仅 usb2_integration 固件：USB3 训练失败时自动回退，或由调试寄存器强制。VID:PID、串号不变，
  控制协议相同（EP0 网桥只转发 GET/SET_REGISTER 与 GET_RECONFIG_STATUS 0xC0/0x31；
  WCID、RECONFIG 请求被丢）。
- 额外调试寄存器：0x0024（模式状态 A）、0x0028（wrap/adapter B）、0x002C（mux/VBUS C）、
  0x0030（EP2 事件计数）、0x0018（写：强制模式，仅调试固件）。
- `R32_DBG_STATE` 在 U2 模式下描述的是 USB3 链路（回退时 [5]=0、[4:3]=2），U2 状态看 0x0024。

## 抓取数据流（由驱动实现，本工具只消费）

- 无帧头裸流，LSB=D0；每样本字节数：2ch→1/4、4ch→1/2、8ch→1、16ch→2、32ch→4。
- 首包丢弃：仅 16U3 丢 4 字节（32U3 从 SN 202610120954 起整字对齐，不丢）。
- 传输大小约 5 ms，至少 4 个在途；不足时驱动告警并降级。
- 每次采集结束打印一行 `slogic-report v1 ... end=ok|stopped|notrig|error:<reason> mbps=...`；
  本工具的压力测试与 DDR 之外的速率判据都读这一行。

## 与现有实现的核对结论（2026-11-13）

- `slogicpt/sigrok.py` 的假设（逗号通道表、FRAME 标记、unitsize=ceil(通道/8)、采样率回卷即失败）
  与规范一致（FRAME 标记只存在于 ≤2.2.0）。
- `device_watch.reconfig()`（0x40/0x30，wValue=1，wIndex=0x5253）在 U3 模式可用；本机
  32U3 固件 202611120832 实测 APP↔DFU 往返各约 1.0–1.5 s，且会重置 DDR 与测试计数。
- 32U3 的 `[usb] reconfig=true`、16U3 的 `reconfig_min_firmware=202610230648` 与文档不冲突。
- 5G/10G 判定仍用 libusb 协商速度，取不到时退回 sigrok 扫描行；规范里的 `R32_DBG_STATE[4:3]`
  可作为将来的第三条退路（本固件 202610301124+ 起才有）。

## 台架观察（2026-11-13，SN 202611120832，10G，32ch）

- DDR 自测：启用约 0.05 s 内 calib_ok，391 次/秒；stop 后计数冻结；`--clear` 语义与文档一致。
- 连续 10 次 × 10 s、以及 1 分钟单次均 sticky=0、lane 为空。
- 有一段时间（约 10 分钟）出现**全 lane 比对错**，`--clear` 后重新启用仍复现；做一次
  APP→DFU→APP 重配置后计数归零并恢复干净（此后 10/10 通过）。即：某些 DDR 状态会跨
  enable/disable 保留，只有 FPGA 重配置（≈ DDR 硬复位）能清。若现场再次出现连续 lane 错，
  先重配置/断电再复测，并把 lane 掩码与温度（0x0048）一起记录。
- 设备在测试期间重新枚举（拔插、主机侧重连）时，正在跑的自测在 FPGA 里不受影响：
  `ddr.run()` 会按端口路径找回设备继续观察，报告里带重连次数。
