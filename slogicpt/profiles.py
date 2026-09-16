"""Product profile loading / validation / resource self-check.

All product differences (VID/PID, channels, bandwidth, expected signal,
firmware paths, blank-flash commands, timeouts) live in
resources/products/*.toml.  Adding a new product requires only a new TOML
file plus firmware/blank-flash resources -- no code changes.
"""
from __future__ import annotations

import platform as platform_mod
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

if getattr(sys, "frozen", False):
    # PyInstaller single binary: resources/ sits NEXT TO the executable
    # (admin-editable), never inside the bundle
    REPO_ROOT = Path(sys.executable).resolve().parent
else:
    REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "out"
RESOURCES_DIR = REPO_ROOT / "resources"
PRODUCTS_DIR = RESOURCES_DIR / "products"

PLATFORM_KEY = {"Linux": "linux", "Windows": "windows", "Darwin": "darwin"}.get(
    platform_mod.system(), "linux")

SCHEMA_VERSION = 1


def parse_rate(value) -> int:
    """'200M' / '10k' / '500000' / 200000000 -> Hz (int)."""
    if isinstance(value, int):
        return value
    m = re.match(r"^(\d+)([kKmMgG]?)$", str(value).strip())
    if not m:
        raise ValueError(f"Invalid rate: {value!r} (expected e.g. 200M, 10K, 500000)")
    n = int(m.group(1))
    unit = m.group(2).upper()
    return n * {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}[unit]


def format_rate(hz: int) -> str:
    """200000000 -> '200M' (sigrok --config accepts this form)."""
    if hz % 1_000_000 == 0:
        return f"{hz // 1_000_000}M"
    if hz % 1_000 == 0:
        return f"{hz // 1_000}k"
    return str(hz)


def parse_count(value) -> int:
    """'1M' / '100k' / 1000 -> sample count (int)."""
    return parse_rate(value)


@dataclass(frozen=True)
class CaptureTest:
    channels: int
    samplerate_hz: int
    samples: str


@dataclass(frozen=True)
class BlankFlashStep:
    name: str
    label: str
    argv: list[str]
    timeout_s: float
    in_pipeline: bool


@dataclass(frozen=True)
class ModeSwitch:
    """DFU<->APP 模式切换方式。
    - "manual":      工具不自动切换；切换步骤仅提示，由工人手动操作，随后等待目标
                     模式设备出现。
    - "script":      工具运行脚本切换（如 16U3 外置 JTAG + gowin_cli），工具会向
                     argv 追加方向参数 "dfu2app" / "app2dfu"。脚本未放置时自动回退
                     为人工提示（即"外置 JTAG 或工人手动"）。
    - "usb_reconfig": 工具用 USB 控制传输 RECONFIG（见"USB LA 协议规范"0x30）触发
                     FPGA 重配置，双向自动切换（如 32U3）。
    """
    method: str                      # "manual" | "script" | "usb_reconfig"
    argv: list[str] | None = None    # script 方式命令（工具追加 dfu2app/app2dfu）
    timeout_s: float = 60


MODE_SWITCH_METHODS = ("manual", "script", "usb_reconfig")


@dataclass(frozen=True)
class ExpectedSignal:
    freq_hz: float
    duty_pct: float
    freq_tol_pct: float
    duty_tol_pp: float


@dataclass(frozen=True)
class Timeouts:
    wait_dfu_device_s: float = 30
    wait_app_device_s: float = 30
    dfu_flash_s: float = 600
    capture_s: float = 120
    blank_flash_step_s: float = 120


@dataclass
class Problem:
    severity: Literal["error", "warning"]
    product_id: str | None
    message: str

    def __str__(self) -> str:
        prefix = f"[{self.product_id}] " if self.product_id else ""
        return f"{self.severity.upper()}: {prefix}{self.message}"


@dataclass(frozen=True)
class ProductProfile:
    id: str
    display_name: str
    vid: int
    app_pid: int
    dfu_pid: int | None                 # None -> DFU not configured
    driver: str
    num_channels: int                   # device total channels -> unitsize = ceil(n/8)
    max_bandwidth_mbps: int
    channel_options: list[int]
    samplerates_hz: list[int]
    voltage_threshold_v: float
    default_samples: str
    default_channels: int
    default_samplerate_hz: int
    capture_tests: list[CaptureTest]
    expected: ExpectedSignal
    app_firmware: Path | None           # resolved absolute path (may not exist yet)
    app_flash_addr: int
    verify_after_flash: bool
    mode_switch: ModeSwitch             # DFU<->APP 切换方式
    blank_flash_dir: Path
    blank_flash_steps: list[BlankFlashStep] | None   # None -> manifest missing/bad
    timeouts: Timeouts

    @property
    def unitsize(self) -> int:
        return (self.num_channels + 7) // 8

    def legal_rates(self, channels: int) -> list[int]:
        """Samplerates satisfying channels*rate/8 <= max_bandwidth."""
        limit = self.max_bandwidth_mbps * 1_000_000
        return [r for r in self.samplerates_hz if channels * r // 8 <= limit]


def load_manifest(manifest_dir: Path) -> tuple[list[BlankFlashStep] | None, list[str]]:
    """Load blank-flash manifest.toml.  Returns (steps, error strings)."""
    manifest_file = manifest_dir / "manifest.toml"
    if not manifest_file.is_file():
        return None, [f"blank_flash manifest 缺失: {manifest_file}"]
    errors: list[str] = []
    try:
        doc = tomllib.loads(manifest_file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        return None, [f"blank_flash manifest 解析失败: {manifest_file}: {e}"]

    steps: list[BlankFlashStep] = []
    for i, raw in enumerate(doc.get("steps", [])):
        try:
            # per-platform command: argv_linux / argv_windows / argv_darwin
            # override the generic argv on that platform
            argv_raw = raw.get(f"argv_{PLATFORM_KEY}", raw.get("argv"))
            if argv_raw is None:
                raise ValueError(
                    f"缺少 argv（或本平台的 argv_{PLATFORM_KEY}）")
            argv = [str(a) for a in argv_raw]
            if not argv:
                raise ValueError("argv 为空")
            steps.append(BlankFlashStep(
                name=str(raw["name"]),
                label=str(raw.get("label", raw["name"])),
                argv=argv,
                timeout_s=float(raw.get("timeout_s", 120)),
                in_pipeline=bool(raw.get("in_pipeline", False)),
            ))
        except (KeyError, ValueError, TypeError) as e:
            errors.append(f"manifest steps[{i}] 无效: {e}")
    if not steps:
        errors.append(f"manifest 无有效步骤: {manifest_file}")
        return None, errors
    return steps, errors


def _parse_profile(path: Path) -> tuple[ProductProfile | None, list[Problem]]:
    problems: list[Problem] = []
    pid_for_log = path.stem
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        return None, [Problem("error", pid_for_log, f"档案解析失败: {e}")]

    def err(msg: str) -> tuple[None, list[Problem]]:
        problems.append(Problem("error", pid_for_log, msg))
        return None, problems

    if doc.get("schema_version") != SCHEMA_VERSION:
        return err(f"schema_version 应为 {SCHEMA_VERSION}, 实际 {doc.get('schema_version')!r}")

    try:
        product = doc["product"]
        usb = doc["usb"]
        capture = doc["capture"]
        expected = doc["expected"]
        firmware = doc.get("firmware", {})

        prod_id = str(product["id"])
        if prod_id != path.stem:
            return err(f"product.id ({prod_id}) 与文件名 ({path.stem}) 不一致")

        dfu_pid = usb.get("dfu_pid")
        if dfu_pid is None:
            problems.append(Problem(
                "warning", prod_id, "usb.dfu_pid 未配置，DFU 与一键流程将禁用"))

        num_channels = int(capture["num_channels"])
        channel_options = [int(c) for c in capture["channel_options"]]
        if any(c <= 0 or c > num_channels for c in channel_options):
            return err(f"channel_options {channel_options} 超出 num_channels={num_channels}")

        samplerates_hz = sorted(parse_rate(r) for r in capture["samplerates"])

        default_channels = int(capture.get("default_channels", channel_options[-1]))
        if default_channels not in channel_options:
            return err(f"default_channels={default_channels} 不在 channel_options 中")
        limit = int(capture["max_bandwidth_mbps"]) * 1_000_000
        legal_defaults = [r for r in samplerates_hz
                          if default_channels * r // 8 <= limit]
        default_samplerate_hz = parse_rate(
            capture.get("default_samplerate", legal_defaults[-1] if legal_defaults else samplerates_hz[0]))
        if default_samplerate_hz not in legal_defaults:
            return err(f"default_samplerate={capture.get('default_samplerate')} "
                       f"对 {default_channels}ch 不合法（不在档位或超带宽）")

        tests = []
        for i, t in enumerate(doc["capture"].get("tests", [])):
            ch = int(t["channels"])
            rate = parse_rate(t["samplerate"])
            if ch > num_channels:
                return err(f"capture.tests[{i}].channels={ch} 超出 num_channels")
            if rate not in samplerates_hz:
                return err(f"capture.tests[{i}].samplerate={t['samplerate']} 不在 samplerates 列表中")
            tests.append(CaptureTest(ch, rate, str(t.get("samples", capture.get("default_samples", "1M")))))
        if not tests:
            problems.append(Problem("warning", prod_id, "capture.tests 为空，一键流程无测试点"))

        max_bw = int(capture["max_bandwidth_mbps"])
        for t in tests:
            if t.channels * t.samplerate_hz // 8 > max_bw * 1_000_000:
                return err(f"测试点 {t.channels}ch@{format_rate(t.samplerate_hz)} 超出带宽 {max_bw}MB/s")

        app_rel = firmware.get("app")
        app_firmware = (RESOURCES_DIR / app_rel).resolve() if app_rel else None

        ms_raw = doc.get("mode_switch", {})
        ms_method = str(ms_raw.get("method", "manual"))
        if ms_method not in MODE_SWITCH_METHODS:
            return err(f"mode_switch.method={ms_method!r} 无效，应为 {MODE_SWITCH_METHODS}")
        ms_argv = None
        if ms_method == "script":
            # 平台专用命令 argv_linux / argv_windows / argv_darwin 优先于通用 argv
            argv_raw = ms_raw.get(f"argv_{PLATFORM_KEY}", ms_raw.get("argv"))
            if argv_raw is None:
                # 脚本未声明 → 回退为人工提示（"外置 JTAG 或工人手动"）
                ms_method = "manual"
            else:
                ms_argv = [str(a) for a in argv_raw]
        mode_switch = ModeSwitch(method=ms_method, argv=ms_argv,
                                 timeout_s=float(ms_raw.get("timeout_s", 60)))

        bf_rel = doc.get("blank_flash", {}).get("dir", f"blank_flash/{prod_id}")
        blank_flash_dir = (RESOURCES_DIR / bf_rel).resolve()
        steps, manifest_errors = load_manifest(blank_flash_dir)
        for m in manifest_errors:
            problems.append(Problem("warning", prod_id, m))

        tmo = doc.get("timeouts", {})
        timeouts = Timeouts(
            wait_dfu_device_s=float(tmo.get("wait_dfu_device_s", 30)),
            wait_app_device_s=float(tmo.get("wait_app_device_s", 30)),
            dfu_flash_s=float(tmo.get("dfu_flash_s", 600)),
            capture_s=float(tmo.get("capture_s", 120)),
            blank_flash_step_s=float(tmo.get("blank_flash_step_s", 120)),
        )

        profile = ProductProfile(
            id=prod_id,
            display_name=str(product["display_name"]),
            vid=int(usb["vid"]),
            app_pid=int(usb["app_pid"]),
            dfu_pid=int(dfu_pid) if dfu_pid is not None else None,
            driver=str(capture["driver"]),
            num_channels=num_channels,
            max_bandwidth_mbps=max_bw,
            channel_options=channel_options,
            samplerates_hz=samplerates_hz,
            voltage_threshold_v=float(capture.get("voltage_threshold_v", 1.6)),
            default_samples=str(capture.get("default_samples", "1M")),
            default_channels=default_channels,
            default_samplerate_hz=default_samplerate_hz,
            capture_tests=tests,
            expected=ExpectedSignal(
                freq_hz=float(expected["freq_hz"]),
                duty_pct=float(expected["duty_pct"]),
                freq_tol_pct=float(expected.get("freq_tol_pct", 5.0)),
                duty_tol_pp=float(expected.get("duty_tol_pp", 5.0)),
            ),
            app_firmware=app_firmware,
            app_flash_addr=int(firmware.get("app_flash_addr", 0)),
            verify_after_flash=bool(firmware.get("verify", True)),
            mode_switch=mode_switch,
            blank_flash_dir=blank_flash_dir,
            blank_flash_steps=steps,
            timeouts=timeouts,
        )
        return profile, problems
    except (KeyError, ValueError, TypeError) as e:
        kind = "缺少必填字段" if isinstance(e, KeyError) else "字段无效"
        return err(f"{kind}: {e!r}")


def load_profiles(products_dir: Path = PRODUCTS_DIR) -> tuple[list[ProductProfile], list[Problem]]:
    """Load every resources/products/*.toml.  A broken file yields a Problem
    and is skipped without affecting other products."""
    profiles: list[ProductProfile] = []
    problems: list[Problem] = []
    if not products_dir.is_dir():
        return [], [Problem("error", None, f"产品档案目录不存在: {products_dir}")]
    files = sorted(products_dir.glob("*.toml"))
    if not files:
        return [], [Problem("error", None, f"产品档案目录为空: {products_dir}，请放置 <product_id>.toml")]
    for f in files:
        profile, probs = _parse_profile(f)
        problems.extend(probs)
        if profile is not None:
            profiles.append(profile)
    # app_pid must be unique (it identifies the product); dfu_pid MAY be
    # shared across products -- DFU mode ("SLogic DFU", 0x30F1) is
    # product-agnostic by design
    app_seen: dict[tuple[int, int], str] = {}
    dfu_pids = {(p.vid, p.dfu_pid): p.id for p in profiles if p.dfu_pid is not None}
    for p in profiles:
        key = (p.vid, p.app_pid)
        if key in app_seen:
            problems.append(Problem(
                "error", p.id,
                f"app VID/PID {key[0]:#06x}:{key[1]:#06x} 与产品 {app_seen[key]} 冲突"))
        app_seen[key] = p.id
        if key in dfu_pids:
            problems.append(Problem(
                "error", p.id,
                f"app VID/PID {key[0]:#06x}:{key[1]:#06x} 与产品 {dfu_pids[key]} 的 dfu_pid 冲突"))
    return profiles, problems


def check_resources(profiles: list[ProductProfile],
                    sigrok_bin: Path | None) -> list[Problem]:
    """Startup self-check for admin-placed resources."""
    problems: list[Problem] = []
    if sigrok_bin is None:
        problems.append(Problem(
            "error", None,
            f"未找到 sigrok-cli 二进制，请按平台命名放入 {RESOURCES_DIR / 'bin'}（见 resources/README.md），采样功能禁用"))
    for p in profiles:
        if p.app_firmware is None:
            problems.append(Problem("warning", p.id, "firmware.app 未配置，DFU 与一键流程禁用"))
        elif not p.app_firmware.is_file():
            problems.append(Problem(
                "warning", p.id,
                f"应用固件缺失: {p.app_firmware}，DFU 与一键流程禁用"))
        if p.blank_flash_steps:
            for step in p.blank_flash_steps:
                exe = Path(step.argv[-1])
                candidate = p.blank_flash_dir / exe
                if not exe.is_absolute() and "/" not in str(exe.parent) and not candidate.exists():
                    problems.append(Problem(
                        "warning", p.id,
                        f"blank_flash 步骤 '{step.name}' 引用的文件缺失: {candidate}"))
    return problems


if __name__ == "__main__":
    profiles, problems = load_profiles()
    print(f"== 加载 {len(profiles)} 个产品档案 ==")
    for p in profiles:
        dfu = f"{p.dfu_pid:#06x}" if p.dfu_pid is not None else "未配置"
        print(f"\n{p.display_name} ({p.id})")
        print(f"  USB: {p.vid:#06x} app={p.app_pid:#06x} dfu={dfu}")
        print(f"  {p.num_channels}ch, {p.max_bandwidth_mbps}MB/s, unitsize={p.unitsize}")
        print(f"  采样率: {', '.join(format_rate(r) for r in p.samplerates_hz)}")
        for ch in p.channel_options:
            print(f"  {ch:>2}ch 合法档位: {', '.join(format_rate(r) for r in p.legal_rates(ch))}")
        print(f"  测试点: {', '.join(f'{t.channels}ch@{format_rate(t.samplerate_hz)}' for t in p.capture_tests)}")
        print(f"  期望: {p.expected.freq_hz/1e6:g}MHz ±{p.expected.freq_tol_pct}%, "
              f"{p.expected.duty_pct}% ±{p.expected.duty_tol_pp}pp")
        print(f"  固件: {p.app_firmware} @ {p.app_flash_addr:#x}")
        ms = p.mode_switch
        ms_extra = f"  argv={ms.argv}" if ms.method == "script" else ""
        print(f"  模式切换: {ms.method}{ms_extra}")
        print(f"  空板步骤: {[s.name for s in p.blank_flash_steps] if p.blank_flash_steps else '无 manifest'}")
    print(f"\n== {len(problems)} 个问题 ==")
    for prob in problems:
        print(f"  {prob}")
    sys.exit(1 if any(p.severity == "error" for p in problems) else 0)
