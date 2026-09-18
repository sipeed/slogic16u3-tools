# -*- coding: utf-8 -*-
"""英文 key -> 中文 对照表（由 slogicpt.i18n.t 使用）。

只放用户可见的界面/日志/报告/异常文案。英文是源码里的 key，此处给中文。
新增/修改界面英文时，务必在此同步中文，否则运行到 zh 会回退成英文（可见）。
分节仅为便于维护，运行时是一个扁平 dict。占位符 {name} 在中英值中保持一致，
以便 str.format() 在两种语言下都能填值。
"""

ZH: dict[str, str] = {
    # ---- gui.py ----------------------------------------------------------
    # status glyph labels (STATUS_STYLE)
    "Pending": "待执行",
    "Running…": "执行中…",
    "Waiting": "等待操作",
    "Pass": "通过",
    "Fail": "失败",
    "Skip": "跳过",
    # step row / logs
    "Single step: {label}": "单步执行：{label}",
    # startup
    "No product profiles": "无产品档案",
    "No usable product profiles under resources/products/; "
    "see resources/README.md to add them.":
        "resources/products/ 下没有可用的产品档案，请参照 resources/README.md 放置。",
    # header
    "Scan the external programmer (JTAG) and read the eFuse lock state":
        "扫描外置烧录器(JTAG)并读 eFuse 锁定状态",
    "Product:": "产品:",
    "Run Full Test": "一键全流程",
    "Stop": "停止",
    # sequence panel
    "Test Sequence (auto in order, or ▶ per step)": "测试序列（自动按序执行，或点 ▶ 单步）",
    "Auxiliary (not counted in sequence result)": "辅助操作（不计入序列结果）",
    # detail panes (report on top, live log on bottom) + ⚙ capture-params popup
    "Run Log": "运行日志",
    "Clear": "清空",
    "Channels:": "通道数:",
    "Sample Rate:": "采样率:",
    "Samples:": "采样点数:",
    "Voltage Threshold (V):": "电压阈值(V):",
    "Custom Capture Verify (engineering)": "自定义采样验证（工程调试）",
    "Per-channel expected values (double-click to edit; "
    "sequence steps use profile values):":
        "每通道期望值（双击修改；序列步骤使用产品档案值）:",
    "Test Params": "测试参数",
    "Session report (paste directly into feedback):": "本次会话报告（可直接粘贴反馈）:",
    "Copy Report": "复制报告",
    # resource-path overrides + one-time environment setup (under Auxiliary)
    "Resource Path Overrides (blank = use profile; this session only)":
        "资源路径覆盖（留空＝用产品档案；仅本次会话生效）",
    "app firmware:": "app 固件:",
    "DFU image:": "DFU 镜像:",
    "eFuse key:": "eFuse 密钥:",
    "Environment Setup (one-time) · {name}": "环境准备（一次性）· {name}",
    "Deploy": "部署",
    "Remove": "移除",
    # banner
    "Standby": "待机",
    "Standby — select a product and connect the device": "待机 — 选择产品并连接设备",
    # problem chip / dialog
    "{n} errors": "{n} 错误",
    "{n} warnings": "{n} 警告",
    "Click for resource self-check details": "点击查看资源自检详情",
    "Resource Self-check": "资源自检",
    "Startup self-check found the following issues (missing items disable the "
    "related function; add resources and restart to take effect):":
        "启动自检发现以下问题（缺失项对应功能已禁用，补齐资源后重启生效）：",
    "No issues": "无问题",
    # product change placeholders
    "Profile has no firmware; please select manually": "档案未配置固件，请手动选择",
    "Profile has no DFU image": "档案未配置 DFU 镜像",
    "Profile has no eFuse key": "档案未配置 eFuse 密钥",
    "Profile defines no steps": "档案未定义任何步骤",
    # aux buttons
    "Switch": "切换",
    "Reflash (repair)": "复烧（返修）",
    "Wait for DFU mode (prompt manual action on timeout) → rewrite app firmware "
    "→ wait for app mode":
        "等待设备进入 DFU 模式（超时提示人工操作）→ 重写应用固件 → 等待应用模式",
    # eFuse badge
    "eFuse locked": "eFuse已锁",
    "eFuse unlocked": "eFuse未锁",
    "eFuse unknown": "eFuse未知",
    # device status
    "libusb backend not found (libusb-1.0.dll): cannot enumerate USB devices, "
    "DFU/APP detection disabled — see resources/README.md":
        "未找到 libusb 后端（libusb-1.0.dll）：无法枚举 USB 设备，"
        "DFU/APP 检测失效——见 resources/README.md",
    "Online {conflict} differs from selected {name}: switch to the correct "
    "product, or plug in the matching device":
        "在线 {conflict}，与所选 {name} 不符：请切换到正确产品，或改插对应设备",
    "also online: {items}": "另在线: {items}",
    "Device: {dev}": "设备: {dev}",
    "No {name} device detected": "未检测到 {name} 设备",
    # enablement tooltips
    "Online device is {online} (APP), differs from selected {sel} — switch to "
    "the correct product or replug":
        "在线设备为 {online}（APP），与所选 {sel} 不符——请切换到正确产品或改插设备",
    "app firmware": "app 固件",
    "external programmer (click 🔄 scan)": "外置烧录器（点 🔄 扫描）",
    "Missing: {items}": "缺少: {items}",
    "Connect the external programmer: click 🔄 scan in the top bar":
        "需要连接外置烧录器：点顶栏 🔄 扫描",
    "Device must be in DFU mode (complete the previous blank-flash step first)":
        "需设备处于 DFU 模式（先完成上一步烧空板）",
    "Device must be in DFU mode": "需设备处于 DFU 模式",
    "Device must be in APP mode (finish flashing APP and waiting for APP first)":
        "需设备处于 APP 模式（先完成烧 APP 并等待 APP）",
    "Production final-check operation; proceed with caution": "产线终检操作，谨慎执行",
    # switch button
    "This product has no DFU (dfu_pid); cannot switch": "该产品未配置 DFU（dfu_pid），无法切换",
    "Currently DFU (flashing) mode; switch to APP (application) mode":
        "当前为 DFU（烧录）模式，切换到 APP（应用）模式",
    "Currently APP (application) mode; switch to DFU (flashing) mode":
        "当前为 APP（应用）模式，切换到 DFU（烧录）模式",
    "No device detected; cannot switch": "未检测到设备，无法切换",
    # actions / logs
    "Run Full Test: {name}": "一键全流程: {name}",
    "This product has no programmer.toml; no external programmer capability.":
        "本产品未配置 programmer.toml，无外置烧录器能力。",
    "Scan external programmer: {name}": "扫描外置烧录器: {name}",
    "exception: {e}": "异常: {e}",
    # env buttons
    "Redeploy": "重新部署",
    "Rule exists; rewrite to update": "规则已存在，可重新写入以更新",
    "Remove the deployed rule": "移除已部署的规则",
    "Not deployed; nothing to remove": "尚未部署，无需移除",
    # mode switch / reflash / sampling
    "Cannot switch": "无法切换",
    "No device for this product detected.": "未检测到该产品设备。",
    "Auxiliary: mode switch {title}": "辅助操作: 模式切换 {title}",
    "Firmware missing": "固件缺失",
    "Reflash (repair): {name}": "复烧（返修）: {name}",
    "Parameter error": "参数错误",
    "Custom capture: {ch}ch@{rate}": "自定义采样: {ch}ch@{rate}",
    "Stop requested…": "已请求停止…",
    # resource rows / file dialogs
    "Defaults to product profile": "默认使用产品档案",
    "Select app firmware": "选择 app 固件",
    "Firmware/Bitstream (*.bin *.fs);;All files (*)": "固件/位流 (*.bin *.fs);;所有文件 (*)",
    "Select DFU image": "选择 DFU 镜像",
    "Bitstream (*.bin *.fs);;All files (*)": "位流 (*.bin *.fs);;所有文件 (*)",
    "Select eFuse key": "选择 eFuse 密钥",
    "eFuse key (*.ekey);;All files (*)": "eFuse 密钥 (*.ekey);;所有文件 (*)",
    "Copied": "已复制",
    # language switch guard
    "Cannot switch language while a test is running.": "测试运行中无法切换语言。",
    # slots
    "Current step: {label}": "当前步骤: {label}",
    "Running — {label}": "运行中 — {label}",
    "Manual mode switch required": "需要手动切换模式",
    "External programmer connected: {detail}": "外置烧录器已连接：{detail}",
    "External programmer not detected: {detail}": "未检测到外置烧录器：{detail}",
    "; failed steps: {items}": "；失败步骤: {items}",
    "eFuse locked; auto re-scanning the external programmer to refresh state…":
        "eFuse 已锁，自动复扫外置烧录器以刷新状态…",
    # report
    "SLogic Production Test Report": "SLogic 产测报告",
    "Time: {ts}": "时间: {ts}",
    "Product: {name} ({id})": "产品: {name} ({id})",
    "Product: -": "产品: -",
    "Device: {devs}": "设备: {devs}",
    "not detected": "未检测到",
    "Overall: {result}  (elapsed {s:.1f}s)": "总结果: {result}  (耗时 {s:.1f}s)",
    "Step Status": "步骤状态",
    "Details": "详情",
    "To report an issue, copy this report and attach the relevant run-log excerpts.":
        "如需反馈问题，请复制本报告并附上运行日志相关片段。",

    # ---- pipeline.py -----------------------------------------------------
    "Blank-flash · eFuse key write-lock": "烧空板 · eFuse 密钥写锁",
    "Blank-flash · write DFU image": "烧空板 · 烧写 DFU 镜像",
    "Wait for DFU device": "等待 DFU 设备",
    "DFU flash app firmware": "DFU 烧写应用固件",
    "Switch to APP mode": "切换到 APP 模式",
    "Wait for APP device": "等待 APP 设备",
    "Switch to DFU mode": "切换到 DFU 模式",
    "Capture {ch}ch@{rate}": "采样验证 {ch}ch@{rate}",
    "Custom capture {ch}ch@{rate}": "自定义采样 {ch}ch@{rate}",
    "Unknown step: {step_id}": "未知步骤: {step_id}",
    "Cancelled": "已取消",
    "Step \"{label}\" failed": "步骤「{label}」失败",
    "Flow aborted: {e}": "流程中止: {e}",
    "Flow exception:": "流程异常:",
    "Flow exception, see log for details": "流程异常，详见日志",
    "eFuse already locked (key written), skipping write-lock":
        "eFuse 已锁定（密钥已写入），跳过写锁",
    "eFuse write-lock": "eFuse 写锁",
    " (already locked)": "（已锁定）",
    "Cannot confirm eFuse state (external programmer not connected / cable fault?), aborting":
        "无法确认 eFuse 状态（外置烧录器未连接/线缆异常？），中止",
    " (state unknown)": "（状态未知）",
    "eFuse not locked: writing AES key and locking (prerequisite for encrypted "
    "DFU boot, irreversible)...":
        "eFuse 未锁：写入 AES 密钥并锁定（加密 DFU 启动前提，不可逆）…",
    " (write failed)": "（写入失败）",
    " (readback confirmed locked)": "（回读确认已锁定）",
    " (readback did not confirm locked)": "（回读未确认锁定）",
    "eFuse readback did not confirm locked, judged as failed": "回读 eFuse 未确认锁定，判定失败",
    "Blank-flash DFU image": "烧空板 DFU 镜像",
    "Waiting for {what} device ({vid}:{pid})...": "等待 {what} 设备 ({vid}:{pid})...",
    "{what} device ready": "{what} 设备已就绪",
    " (after manual intervention)": "（人工介入后）",
    "Waiting for {what} device timed out -- please replug":
        "等待 {what} 设备超时，请重新插拔/上电设备…",
    "APP SerialNumber: {sn} (for version identification/distinction)":
        "APP SerialNumber: {sn}（用于版本识别/区分）",
    "APP device provided no SerialNumber descriptor": "APP 设备未提供 SerialNumber 描述符",
    "dfu_pid not configured, cannot switch to APP": "dfu_pid 未配置，无法切换到 APP",
    "No APP device detected, skipping auto-switch, waiting for DFU directly":
        "未检测到 APP 设备，跳过自动切换，直接等待 DFU",
    "Sending RECONFIG to switch to {mode} mode ({vid}:{pid})...":
        "发送 RECONFIG 切换到 {mode} 模式 （{vid}:{pid}）...",
    "RECONFIG switch failed: {e}, trying fallback...": "RECONFIG 切换失败: {e}，尝试保底方案…",
    "External programmer switch failed, falling back to manual...":
        "外置烧录器切换失败，转人工…",
    "Please press the onboard MODE button to switch {name} to {mode} mode; it "
    "will continue automatically once the target-mode device is detected":
        "请按板载 MODE 按键，将 {name} 切换到 {mode} 模式；检测到目标模式设备后自动继续",
    "dfu_pid not configured": "dfu_pid 未配置",
    "Firmware or dfu_pid not configured, cannot flash": "固件或 dfu_pid 未配置，无法烧写",
    "DFU flash": "DFU 烧写",
    "== Capture {label} ({samples} samples) ==": "== 采样 {label} ({samples} samples) ==",
    "Multiple SLogic devices detected online at once; the current sigrok driver "
    "does not yet support selecting a device, please keep only the device under "
    "test and retry:":
        "检测到多台 SLogic 设备同时在线，当前 sigrok 驱动暂不支持指定设备，请只保留被测设备后重试:",
    "multiple-device ambiguity": "多设备歧义",
    "Capture failed: {e}": "采样失败: {e}",
    "Capture done: {n} samples, {sec}s -> {file}": "采样完成: {n} samples, {sec}s -> {file}",
    "{n}/{total} channels passed": "{n}/{total} 通道通过",

    # ---- programmer.py ---------------------------------------------------
    "Launch failed: {e}": "启动失败: {e}",
    "cancelled": "已取消",
    "timed out ({secs}s)": "超时 ({secs}s)",
    "Programmer CLI not found (programmer.cli undeclared and no default AppImage)":
        "未找到烧录器 CLI（programmer.cli 未声明且无默认 AppImage）",
    "Programmer CLI not found": "未找到烧录器 CLI",
    "Trying cable-index {cable} (device={dev}) to read the IDCODE...":
        "尝试 cable-index {cable}（device={dev}）读器件码…",
    "cable-index {cable} read a device, reading eFuse lock state...":
        "cable-index {cable} 读到器件，读 eFuse 锁定状态…",
    "Result: external programmer connected, device={dev}, cable-index={cable}, eFuse={efuse}":
        "结果：外置烧录器已连接，device={dev}，cable-index={cable}，eFuse={efuse}",
    "Result: no device read on any cable -- external programmer not connected or unpowered":
        "结果：未在任何 cable 上读到器件——外置烧录器未连接或未上电",
    "No device read on any cable (external programmer not connected?)":
        "未在任何 cable 上读到器件（外置烧录器未连接？）",
    "No cable-index provided, probing first...": "未提供 cable-index，先探测…",
    "programmer.toml declares no [programmer.flash]; cannot flash":
        "programmer.toml 未声明 [programmer.flash]，无法烧录",
    "DFU image missing: {img}": "DFU 镜像缺失: {img}",
    "Blank-flash (custom command, watchdog {secs}s): ": "烧空板（自定义命令，看门狗 {secs}s）: ",
    "flash complete": "烧录完成",
    "flash failed": "烧录失败",
    "Programmer CLI not found; cannot flash": "未找到烧录器 CLI，无法烧录",
    "No usable cable found; external programmer not connected?":
        "未找到可用 cable，外置烧录器未连接？",
    "Blank-flash: device={device} cable-index={cable} run={run} --fsFile={fsfile} "
    "spiaddr={spiaddr} (watchdog {secs}s)":
        "烧空板：device={device} cable-index={cable} run={run} --fsFile={fsfile} "
        "spiaddr={spiaddr} (看门狗 {secs}s)",
    "state: {state} (cable-index={cable})": "状态: {state}（cable-index={cable}）",
    "Switching via external programmer ({direction}): device={device} cable-index={cable}":
        "经外置烧录器切换（{direction}）：device={device} cable-index={cable}",
    "switch command complete": "切换命令完成",
    "switch command failed": "切换命令失败",
    "Programmer CLI not found; cannot write-lock": "未找到烧录器 CLI，无法写锁",
    "programmer.toml declares no [programmer.efuse]; cannot write-lock":
        "programmer.toml 未声明 [programmer.efuse]，无法写锁",
    "Key file missing: {key}": "密钥文件缺失: {key}",
    "Writing and locking AES key (irreversible): device={device} cable-index={cable} keyFile={keyfile}":
        "写入并锁定 AES 密钥（不可逆）：device={device} cable-index={cable} keyFile={keyfile}",
    "write-lock complete (key written and locked)": "写锁完成（密钥已写入并锁定）",
    "write-lock failed": "写锁失败",

    # ---- profiles.py (self-check Problem messages) -----------------------
    "programmer.toml missing: {f}": "programmer.toml 缺失: {f}",
    "programmer.toml parse failed: {f}: {e}": "programmer.toml 解析失败: {f}: {e}",
    "programmer.toml missing [programmer] section: {f}": "programmer.toml 缺 [programmer] 段: {f}",
    "programmer.toml [programmer] missing device: {f}": "programmer.toml [programmer] 缺 device: {f}",
    "cable_candidates is empty": "cable_candidates 为空",
    "[programmer.switch] must declare at least dfu2app or app2dfu":
        "[programmer.switch] 需至少声明 dfu2app 或 app2dfu",
    "programmer.toml [programmer] invalid: {e}": "programmer.toml [programmer] 无效: {e}",
    "profile parse failed: {e}": "档案解析失败: {e}",
    "schema_version should be {expected}, actual {actual!r}":
        "schema_version 应为 {expected}, 实际 {actual!r}",
    "product.id ({pid}) does not match directory name ({dirname})":
        "product.id ({pid}) 与目录名 ({dirname}) 不一致",
    "usb.dfu_pid not configured; DFU and one-click flow will be disabled":
        "usb.dfu_pid 未配置，DFU 与一键流程将禁用",
    "channel_options {opts} exceed num_channels={n}":
        "channel_options {opts} 超出 num_channels={n}",
    "default_channels={dc} not in channel_options": "default_channels={dc} 不在 channel_options 中",
    "default_samplerate={sr} is invalid for {dc}ch (not in the list or exceeds bandwidth)":
        "default_samplerate={sr} 对 {dc}ch 不合法（不在档位或超带宽）",
    "capture.tests[{i}].channels={ch} exceeds num_channels":
        "capture.tests[{i}].channels={ch} 超出 num_channels",
    "capture.tests[{i}].samplerate={sr} not in samplerates list":
        "capture.tests[{i}].samplerate={sr} 不在 samplerates 列表中",
    "capture.tests is empty; one-click flow has no test points":
        "capture.tests 为空，一键流程无测试点",
    "test point {ch}ch@{rate} exceeds bandwidth {bw}MB/s":
        "测试点 {ch}ch@{rate} 超出带宽 {bw}MB/s",
    "missing required field": "缺少必填字段",
    "invalid field": "字段无效",
    "product profile directory does not exist: {d}": "产品档案目录不存在: {d}",
    "product profile directory is empty: {d}; please place <product_id>/product.toml":
        "产品档案目录为空: {d}，请放置 <product_id>/product.toml",
    "app VID/PID {vidpid} conflicts with product {other}":
        "app VID/PID {vidpid} 与产品 {other} 冲突",
    "app VID/PID {vidpid} conflicts with dfu_pid of product {other}":
        "app VID/PID {vidpid} 与产品 {other} 的 dfu_pid 冲突",
    "sigrok-cli binary not found; place it in {d} with the platform-specific name "
    "(see resources/README.md); capture disabled":
        "未找到 sigrok-cli 二进制，请按平台命名放入 {d}（见 resources/README.md），采样功能禁用",
    "firmware.app not configured; DFU and one-click flow disabled":
        "firmware.app 未配置，DFU 与一键流程禁用",
    "application firmware missing: {path}; DFU and one-click flow disabled":
        "应用固件缺失: {path}，DFU 与一键流程禁用",
    "programmer CLI not found (programmer.cli not declared and no Gowin AppImage "
    "in resources/bin); blank-flash/eFuse disabled":
        "未找到烧录器 CLI（programmer.cli 未声明且 resources/bin 无 Gowin AppImage），烧空板/eFuse 功能禁用",
    "blank-flash DFU image missing: {path}; blank-flash disabled":
        "烧空板 DFU 镜像缺失: {path}，烧空板禁用",
    "eFuse key file missing: {path}; the eFuse write-lock prerequisite of "
    "blank-flash will fail":
        "eFuse 密钥文件缺失: {path}，烧空板的 eFuse 写锁前置步骤将失败",
    "switch {d} references a missing file: {path}; this direction falls back to a "
    "manual dialog":
        "切换 {d} 引用的文件缺失: {path}，该方向回退弹窗人工",

    # ---- sigrok.py / flasher.py / mode_switch.py -------------------------
    "Capture cancelled": "采样已取消",
    "Capture timed out ({s:.0f}s)": "采样超时 ({s:.0f}s)",
    "Device does not support samplerate {rate} (driver wrapped to {wrapped}). "
    "Please fix the product profile capture.samplerates":
        "设备不支持采样率 {rate}（驱动回卷到 {wrapped}）。请修正产品档案 capture.samplerates",
    "sigrok-cli exit code {code}": "sigrok-cli 退出码 {code}",
    "No capture data file produced: {path}": "未产生采样数据文件: {path}",
    "Capture data is less than one full sample": "采样数据不足一个样本",
    "Firmware file not found: {path}": "固件文件不存在: {path}",
    "Firmware: {path} ({size} bytes) -> {vid:#06x}:{pid:#06x} @ {addr:#x}":
        "固件: {path} ({size} bytes) -> {vid:#06x}:{pid:#06x} @ {addr:#x}",
    "USB transfer failed ({err}), resetting device and retrying once…":
        "USB 传输失败（{err}），复位设备后重试一次…",
    "DFU flash failed: {err}": "DFU 烧写失败: {err}",
    "Device not found after reset": "复位后未找到设备",
    "Device {vid:#06x}:{pid:#06x} not found, cannot send RECONFIG":
        "未找到设备 {vid:#06x}:{pid:#06x}，无法发送 RECONFIG",
    "RECONFIG sent to {vid:#06x}:{pid:#06x}": "RECONFIG 已发送至 {vid:#06x}:{pid:#06x}",
    "RECONFIG triggered (device offline, control transfer returned {err}, expected)":
        "RECONFIG 已触发（设备离线，控制传输返回 {err}，属预期）",

    # ---- env_setup.py (title/hint only; deploy log stays English) --------
    "Linux device permissions (udev)": "Linux 设备权限 (udev)",
    "Windows driver (WinUSB)": "Windows 驱动 (WinUSB)",
    "Install a udev rule for SLogic (359F) and the FTDI cable (0403:6010) for "
    "root-free access; deploying pops up an authorization dialog, then replug "
    "the device to take effect.":
        "为 SLogic(359F) 与 FTDI 线缆(0403:6010) 安装 udev 规则，免 root 访问；"
        "部署会弹出授权框，之后重新插拔设备生效。",
    "Bind the FTDI A-channel (0403:6010 interface 0) to WinUSB, required by "
    "openFPGALoader's high-speed flashing; deploying pops up a UAC authorization dialog.":
        "将 FTDI A 通道(0403:6010 接口0)绑定到 WinUSB，openFPGALoader 高速烧录所需；"
        "部署会弹出 UAC 授权框。",

    # ---- dfu/ (exceptions surfaced via flasher) --------------------------
    "Flash communication error: SR1 stuck reading 0xFF (bus hung)":
        "Flash 通信异常：SR1 恒读 0xFF（总线挂起）",
    "Flash write wait timed out: WIP did not clear in time":
        "Flash 写等待超时：WIP 未在预期内清零",
    "Start address 0x{addr:06X} is not 64KB-aligned": "起始地址 0x{addr:06X} 未按 64KB 对齐",
    "Firmware is empty": "固件为空",
    "SPI flash reset failed": "SPI flash reset 失败",
    "Flash not responding (ID={dev_id}); check the DFU connection and retry":
        "Flash 无响应（ID={dev_id}），请检查 DFU 连接后重试",
    "Post-erase verify failed: region is not all 0xFF": "擦除后校验失败：区域非全 0xFF",
    "Flash verify failed: first difference at 0x{pos:06X}": "烧写校验失败：首个差异在 0x{pos:06X}",
    "No libusb backend found (libusb-1.0.dll): put it in resources/bin/ or "
    "install system libusb-1.0":
        "未找到 libusb 后端（libusb-1.0.dll）：请放入 resources/bin/ 或安装系统 libusb-1.0",
    "Device not found; check the VID/PID or connection state":
        "设备未找到，请检查VID/PID或连接状态",
    "Device not found after reset; check the connection state":
        "复位后未找到设备，请检查连接状态",
    "Required input/output endpoints not found": "未找到所需的输入/输出端点",
    "USB communication error: {e}": "USB通信错误: {e}",
}
