# -*- coding: utf-8 -*-
"""英文 key -> 中文 对照表（由 slogicpt.i18n.t 使用）。

只放用户可见的界面/日志/报告/异常文案。英文是源码里的 key，此处给中文。
新增/修改界面英文时，务必在此同步中文，否则运行到 zh 会回退成英文（可见）。
分节仅为便于维护，运行时是一个扁平 dict。占位符 {name} 在中英值中保持一致，
以便 str.format() 在两种语言下都能填值。
"""

ZH: dict[str, str] = {
    # ---- ui ---------------------------------------------------------------
    # status glyph labels (STATUS_STYLE)
    "Pending": "待执行",
    "Running…": "执行中…",
    "Waiting": "等待操作",
    "Pass": "通过",
    "Fail": "失败",
    # step row / logs
    "Single step: {label}": "单步执行：{label}",
    # startup
    "No product profiles": "无产品档案",
    "No usable product profiles under resources/products/; "
    "see resources/README.md to add them.":
        "resources/products/ 下没有可用的产品档案，请参照 resources/README.md 放置。",
    # header
    "Stop": "停止",
    # sequence panel
    # detail panes (report on top, live log on bottom) + ⚙ capture-params popup
    "Run Log": "运行日志",
    "Clear": "清空",
    "Session report (paste directly into feedback):": "本次会话报告（可直接粘贴反馈）:",
    "Copy Report": "复制报告",
    # resource-path overrides + one-time environment setup (under Auxiliary)
    "Environment Setup (one-time) · {name}": "环境准备（一次性）· {name}",
    "Deploy": "部署",
    "Remove": "移除",
    # banner
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
    "Profile defines no steps": "档案未定义任何步骤",
    # aux buttons
    # eFuse badge
    # device status
    "libusb backend not found (libusb-1.0.dll): cannot enumerate USB devices, "
    "DFU/APP detection disabled — see resources/README.md":
        "未找到 libusb 后端（libusb-1.0.dll）：无法枚举 USB 设备，"
        "DFU/APP 检测失效——见 resources/README.md",
    "Device: {dev}": "设备: {dev}",
    # enablement tooltips
    "Missing: {items}": "缺少: {items}",
    # fixture plug/unplug guidance (scan button & full-test setup)
    "Requires Tang Nano 9K plugged in": "需插着 Tang Nano 9K",
    # switch button
    # actions / logs
    # env buttons
    "Redeploy": "重新部署",
    "Rule exists; rewrite to update": "规则已存在，可重新写入以更新",
    "Remove the deployed rule": "移除已部署的规则",
    "Not deployed; nothing to remove": "尚未部署，无需移除",
    # language switch / sampling
    "Cannot switch": "无法切换",
    "Stop requested…": "已请求停止…",
    # resource rows / file dialogs
    "Copied": "已复制",
    # language switch guard
    "Cannot switch language while a test is running.": "测试运行中无法切换语言。",
    # slots
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
    "Unknown step: {step_id}": "未知步骤: {step_id}",
    "Cancelled": "已取消",
    "Step \"{label}\" failed": "步骤「{label}」失败",
    "Flow aborted: {e}": "流程中止: {e}",
    "Flow exception:": "流程异常:",
    "Flow exception, see log for details": "流程异常，详见日志",
    "Capture failed: {e}": "采样失败: {e}",

    # ---- fixture/protocol steps (pipeline._run_mode_config) --------------
    "Applied the profile's signal_source.channel_order permutation to this capture":
        "已按档案的 signal_source.channel_order 重排本次采样",
    "Signal generator · all {n} patterns": "信号发生器 · 全部 {n} 个模式",
    "Unpack failed: {e}": "解包失败: {e}",
    "Capture failed: {e}": "采样失败: {e}",
    "Signal generator unavailable: {e}": "信号发生器不可用: {e}",
    "no signal generator": "无信号发生器",
    "Mode {m} not confirmed: {e}": "模式 {m} 未确认: {e}",
    "mode not confirmed": "模式未确认",
    "M{mode} is not ready: {note}": "M{mode} 未就绪: {note}",
    "not ready": "未就绪",
    "Signal generator on {p}, alive, mode 0": "信号发生器在 {p}，存活，模式 0",
    "== signal generator: {p} patterns pass, {f} fail ==":
        "== 信号发生器: {p} 个模式通过，{f} 个失败 ==",
    "Capture attempt {a}/{n} failed ({e}); retrying":
        "采样第 {a}/{n} 次失败（{e}），重试中",
    "Capture failed: {e}": "采样失败: {e}",

    # The end-of-flow summary that names the wires to go and look at.
    "---- failing channels ({n}) ----": "---- 不通过的通道（{n} 处）----",
    "got": "实测",
    "want": "应为",
    "ok in group": "组内正常",
    # ---- 夹具插入提示 ----------------------------------------------------
    "Please PLUG IN the signal-generator fixture (Tang Nano 9K) and wait -- "
    "the flow continues by itself":
        "请【插上】信号发生器夹具（Tang Nano 9K）的 USB，然后等待 —— 流程会自动继续",
    "Fixture detected ({p}), continuing": "已检测到夹具（{p}），继续",
    "still waiting ({s}s): {m}": "仍在等待（{s} 秒）：{m}",

    # ---- fixture.py ------------------------------------------------------
    "Launch failed: {e}": "启动失败: {e}",

    # ---- profiles.py (self-check Problem messages) -----------------------
    "profile parse failed: {e}": "档案解析失败: {e}",
    "schema_version should be {expected}, actual {actual!r}":
        "schema_version 应为 {expected}, 实际 {actual!r}",
    "product.id ({pid}) does not match directory name ({dirname})":
        "product.id ({pid}) 与目录名 ({dirname}) 不一致",
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

    # ---- sigrok.py --------------------------------------------------------
    "Capture cancelled": "采样已取消",
    "Capture timed out ({s:.0f}s)": "采样超时 ({s:.0f}s)",
    "Device does not support samplerate {rate} (driver wrapped to {wrapped}). "
    "Please fix the product profile capture.samplerates":
        "设备不支持采样率 {rate}（驱动回卷到 {wrapped}）。请修正产品档案 capture.samplerates",
    "sigrok-cli exit code {code}": "sigrok-cli 退出码 {code}",
    "No capture data file produced: {path}": "未产生采样数据文件: {path}",
    "Capture data is less than one full sample": "采样数据不足一个样本",

    # ---- env_setup.py (title/hint only; deploy log stays English) --------
    "Linux device permissions (udev)": "Linux 设备权限 (udev)",
    "Windows driver (WinUSB)": "Windows 驱动 (WinUSB)",
    "Install a udev rule for SLogic (359F) and the fixture's FTDI ({pair}) for "
    "root-free access; deploying pops up an authorization dialog, then replug "
    "the device to take effect.":
        "为 SLogic(359F) 与夹具 FTDI({pair}) 安装 udev 规则，免 root 访问；"
        "部署会弹出授权框，之后重新插拔设备生效。",
    "Bind the fixture's FTDI A-channel ({pair} interface 0) to WinUSB, which "
    "openFPGALoader needs to load the fixture; deploying pops up a UAC "
    "authorization dialog.":
        "将夹具 FTDI A 通道({pair} 接口0)绑定到 WinUSB，openFPGALoader 载入夹具所需；"
        "部署会弹出 UAC 授权框。",

    # ---- fixture / signal source / plan (SLogic32U3 handover) ------------
    'fixture.toml parse failed: {e}': 'fixture.toml 解析失败：{e}',
    'fixture.toml schema_version should be {expected}, actual {actual!r}':
        'fixture.toml 的 schema_version 应为 {expected}，实际为 {actual!r}',
    'fixture.toml is invalid: {e}': 'fixture.toml 配置无效：{e}',
    'signal_source.serial_hints must be a non-empty list':
        'signal_source.serial_hints 必须是非空列表',
    'Cannot find the fixture: no {vid:#06x}:{pid:#06x} device reports a fixture serial':
        '找不到夹具：没有任何 {vid:#06x}:{pid:#06x} 设备的序列号匹配夹具关键字',
    'openFPGALoader --scan-usb did not run: {p}': 'openFPGALoader --scan-usb 运行失败：{p}',
    'no matching USB device': '没有匹配的 USB 设备',
    "On Windows the fixture's FTDI channel A must use the WinUSB driver: deploy it on the Environment Setup page, then replug the fixture":
        'Windows 上夹具 FTDI 的 A 通道必须用 WinUSB 驱动：到「环境准备」页点「部署」，然后重新插拔夹具',
    'Loading fixture with openFPGALoader: {name} (FTDI serial {s})':
        '用 openFPGALoader 烧录夹具：{name}（FTDI 序列号 {s}）',
    'openFPGALoader reported no success marker (rc={rc})':
        'openFPGALoader 未报告成功标记（rc={rc}）',
    'Fixture bitstream not found: {p}':
        '找不到夹具位流：{p}',
    'openFPGALoader timed out after {s}s':
        'openFPGALoader 超时（{s} 秒）',
    'none of them reports a serial containing "{h}"':
        '没有一个的序列号含 "{h}"',
    '{n} of them report a serial containing "{h}"':
        '其中 {n} 个的序列号含 "{h}"',
    "Multiple {vid:#06x}:{pid:#06x} ports found ({names}); {hint}. The Gowin USB programmer is an FT2232H with the same IDs and two ports of its own, so unplug it if you do not need it, or set signal_source.serial explicitly to the fixture's port.":
        '发现多个 {vid:#06x}:{pid:#06x} 串口（{names}）；{hint}。Gowin USB 编程器是同 ID 的 FT2232H，自带两个串口——不需要时请拔掉它，或在 signal_source.serial 中显式指定夹具的串口。',
    'No Tang Nano signal generator found (looking for {vid:#06x}:{pid:#06x})':
        '未找到 Tang Nano 信号发生器（查找 {vid:#06x}:{pid:#06x}）',
    'Configured port {p} is not present':
        '配置的串口 {p} 不存在',
    '{p} opened but the FPGA did not answer; is the bitstream loaded?':
        '{p} 已打开但 FPGA 无应答；位流是否已加载？',
    'Cannot open {p}: {e}':
        '无法打开 {p}：{e}',
    'No confirmation for mode {m} on {p}':
        '{p} 未确认切换到模式 {m}',
    '== M{mode} {label} ==':
        '== M{mode} {label} ==',
    '-- {label} ({ms} ms window, {n} samples) --':
        '-- {label}（窗口 {ms} ms，{n} 个采样点）--',
    '   {label}: {verdict}  ({p} ok / {f} fail)':
        '   {label}: {verdict}  （{p} 通过 / {f} 失败）',
    'could not load the signal generator':
        '信号发生器烧录失败',
    'capture failed':
        '采集失败',
    'PASS':
        'PASS',
    'FAIL':
        'FAIL',
    'signal_source.channel_order must be a permutation of 0..{n}':
        'signal_source.channel_order 必须是 0..{n} 的一个排列',
    'plan[{i}] has no usable configs':
        'plan[{i}] 没有可用的 configs',
    'capture.threshold_groups[{i}] has an out-of-range channel':
        'capture.threshold_groups[{i}] 含越界通道',
    'plan[{i}].configs[{j}].channels={ch} exceeds num_channels':
        'plan[{i}].configs[{j}].channels={ch} 超过 num_channels',
    'plan[{i}].configs[{j}].samplerate={sr} not in the samplerates list':
        'plan[{i}].configs[{j}].samplerate={sr} 不在 samplerates 列表中',

    # ---- station arming (v0.2.0) -------------------------------------------
    'Recognised automatically from the board':
        '由被测板自动识别',
    'Arm the station: each newly connected board is recognised and run automatically':
        '布防：每接入一块被测板即自动识别并执行',
    'Waiting for the board under test…':
        '等待被测板接入…',
    'Board removed; waiting for the next board…':
        '被测板已取下，等待下一块…',
    'Waiting for the next board…':
        '等待下一块被测板…',
    'Start':
        '开始',
    'No analyzer detected':
        '未检测到分析仪',
    'Disarmed':
        '已撤防',
    'Remove the board under test':
        '请取下被测板',
    'Connect a board first (click Start to recognise it)':
        '请先接入被测板（点"开始"自动识别）',
    '{n} analyzers online; keep only the board under test connected':
        '在线 {n} 台分析仪；请只保留被测板一台',
    'USB link':
        'USB 链路',
    'USB link {got} (needs {need}): OK':
        'USB 链路 {got}（要求 {need}）：合格',
    'No {name} running its app firmware was found':
        '未找到运行应用固件的 {name}',
    'link too slow: {got}, needs {need} (check the cable and the host port)':
        '链路不合格：{got}，要求 {need}（检查线缆与主机接口）',
    '{name} is in DFU mode: the app firmware has not been flashed':
        '{name} 处于 DFU 模式：应用固件尚未烧录',
    'unknown link {name!r} (expected one of {opts})':
        '未知链路 {name!r}（应为 {opts} 之一）',
    'Check USB link':
        '检查 USB 链路',

    # ---- test station: 10G then 5G link passes ---------------------------
    'Check USB link ({link})':
        '检查 USB 链路（{link}）',
    'Re-plug into a {link} port':
        '换插到 {link} 接口',
    "Move the board's USB cable to a {link} port; the test continues when it is detected":
        '请把被测板的 USB 线换插到 {link} 接口；检测到后自动继续测试',
    'Waiting for {link}: seen {devs}':
        '等待 {link} 链路：当前 {devs}',
    'no config for the {link} pass':
        '没有 {link} 轮的采集配置',
    'plan M{mode} has no config for the {link} pass':
        'plan M{mode} 没有 {link} 轮的采集配置',
    'Connect the board under test':
        '请连接被测板',
    'Click Start, then connect the board under test; it is recognised automatically':
        '点击「开始」后接入被测板，自动识别产品',
    # ---- Material UI (v0.3.0) ---------------------------------------------
    'SLogic Production Test':
        'SLogic 产测',
    'Failed steps: {items}':
        '失败步骤：{items}',
    'Product: {name}':
        '产品：{name}',
    'Report':
        '报告',
    'Environment Setup':
        '环境准备',
    'Language':
        '语言',
    'Theme':
        '主题',
    'Follow system':
        '跟随系统',
    'Light':
        '浅色',
    'Dark':
        '深色',
    'Armed':
        '已布防',
    'not plugged in':
        '未插入',
    'capture retried {n}x: {why}':
        '采集重试 {n} 次后成功（{why}）',
    'Retry':
        '重试',
    'Run this step again (and the steps after it that have not run)':
        '重新执行这一步（以及它之后尚未执行的步骤）',
    'Retry: {label}':
        '重试：{label}',
    'Passed after retry: {items}':
        '重试后通过：{items}',
    ' (retried {n}x)':
        '（重试 {n} 次）',
    # ---- result panel (v0.4.3) --------------------------------------------
    'Analyzer':
        '分析仪',
    'Armed: every board connected is tested automatically':
        '已布防：接入的每块板都会自动开测',
    'Bench readiness':
        '台架就绪检查',
    'Boards tested since the program started (not saved)':
        '本次启动以来测过的板子（不存档）',
    'Device permissions':
        '设备权限',
    'Fixture':
        '夹具',
    'Link':
        '链路',
    'Now detected: {devs}':
        '当前检测到：{devs}',
    'Step {k}/{n}: {what}':
        '第 {k}/{n} 步：{what}',
    'This session: {n} tested · {p} pass · {f} fail':
        '本次：已测 {n} · 通过 {p} · 失败 {f}',
    'USB link check':
        'USB 链路检查',
    'libusb backend not found':
        '未找到 libusb 后端',
    'no board':
        '无被测板',
    'not connected':
        '未连接',
    'not connected (Tang Nano 9K)':
        '未连接（Tang Nano 9K）',
    'not found; see resources/README.md':
        '未找到，见 resources/README.md',
    'udev rule deployed':
        'udev 规则已部署',
    'udev rule missing: deploy it on the Environment Setup page':
        '未部署 udev 规则：到「环境准备」页一键部署',
    'Fixture driver': '夹具驱动',
    'Switch to DFU and back to APP': '切换到 DFU 再切回 APP',
    'DFU/APP switch': 'DFU/APP 切换',
    'Switching {a} -> {b}': '正在切换 {a} -> {b}',
    'RECONFIG to the {mode} device failed: {e}': '向 {mode} 设备发送 RECONFIG 失败：{e}',
    'RECONFIG sent ({a} -> {b}), waiting for the {b} device…': '已发送 RECONFIG（{a} -> {b}），等待 {b} 设备…',
    'not back in {mode} mode within {s}s after RECONFIG (seen: {seen})':
        '发送 RECONFIG 后 {s} 秒内没有以 {mode} 模式重新出现（当前：{seen}）',
    'APP came back with another serial (firmware id) {a}, was {b}': '切回 APP 后序列号（固件标识）变了：{a}，原为 {b}',
    'nothing': '无',
    '{mode} device back: {dev}': '{mode} 设备已就绪：{dev}',
    '[usb] reconfig needs dfu_pid': '[usb] reconfig 需要配置 dfu_pid',
    '[[plan]] is empty; there is nothing to test': '[[plan]] 为空，没有可测的内容',
    'openFPGALoader not found; cannot load the fixture': '找不到 openFPGALoader，无法载入夹具',
    'not WinUSB: deploy it on the Environment Setup page':
        '不是 WinUSB：到「环境准备」页一键部署',
    '{n} connected; keep only the board under test':
        '连了 {n} 台，只保留被测板',
    'Reset':
        '复位',
    'Clear the result and reset the bench: the fixture back to M0, devices rescanned':
        '清空结果并复位台架：夹具切回 M0，重新扫描设备',
    'Fixture on {p} answered, back in M0':
        '夹具（{p}）应答正常，已切回 M0',
    'Fixture reset failed: {e}':
        '夹具复位失败：{e}',
    'Fixture does not answer ({e}); loading its bitstream into SRAM again':
        '夹具无应答（{e}），重新把位流载入 SRAM',
    'Loading the fixture bitstream into SRAM (first use in this session)':
        '本次启动首次使用夹具，把位流载入 SRAM',
}
