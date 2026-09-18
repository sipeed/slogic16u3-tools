"""Product profile loading / validation / resource self-check.

All product differences (VID/PID, channels, bandwidth, expected signal,
firmware paths, programmer commands, timeouts) live in
resources/products/<id>/.  Each product is a directory:

    resources/products/<id>/
        product.toml      # profile (public)
        programmer.toml    # external programmer commands (public)
        firmware/          # app.bin, dfu.{fs,bin}, efuse.ekey (factory, gitignored)

Adding a new product requires only a new directory + resources -- no code
changes.  Every blank-flash / eFuse operation is driven by a single
`cli` argv prefix declared in programmer.toml (see slogicpt/programmer.py);
there are no wrapper shell scripts.
"""
from __future__ import annotations

import platform as platform_mod
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .i18n import t

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


# 烧空板默认 Gowin op：54 = "exFlash Erase,Program,Verify Arora V"。该操作经
# --fsFile 同时接受 ASCII 的 .fs 与二进制 .bin 位流（实测 GW5AT-60B 的 dfu.bin
# 二进制位流用 run 54 烧写并校验通过）。16U3/32U3 通用。
# 切勿按扩展名映射到 run 55/56："C Bin" 是给 RISC-V 软核固件(.bin)的，拿它烧
# FPGA 位流会静默写 0 字节、Verify 失败（起止地址相同）。确有 RISC-V 固件需求
# 时，在 [programmer.flash] 显式写 run 覆盖即可。
# 另注：Gowin CLI 的 --fsFile 需要绝对路径（相对路径报 "Not found any data File"），
# 本模块传入的 image 已 .resolve() 为绝对路径。
DEFAULT_FLASH_RUN = 54

# 烧空板探测线缆的默认候选（逐个试 --cable-index，首个读到本 device 的即用）：
# 覆盖各台架常见值——4=USB Debugger A，1=FT2CH，5=Gowin USB Cable(WINUSB)，0=GWU2X。
DEFAULT_CABLES = [4, 1, 5, 0]


def _resolve_image(product_dir: Path, spec: str) -> Path:
    """把 DFU 镜像声明解析为绝对路径。spec 含通配符（如 firmware/dfu.*）时在产品
    目录内匹配：优先 .fs 再 .bin，取唯一/首个匹配；无匹配则返回字面路径（交由
    check_resources 报缺失）。"""
    if "*" in spec or "?" in spec:
        hits = sorted(product_dir.glob(spec))
        for ext in (".fs", ".bin"):
            for h in hits:
                if h.suffix.lower() == ext:
                    return h.resolve()
        if hits:
            return hits[0].resolve()
    return (product_dir / spec).resolve()


@dataclass(frozen=True)
class FlashOp:
    """烧空板：把 DFU 位流写入外部 SPI Flash。
    默认组装 Gowin CLI：`<cli> --device D --cable-index c --run <run> --fsFile <image>
    --spiaddr <addr>`；声明了 argv 时改跑自定义完整命令（如 openFPGALoader），
    模板变量 {image}/{spiaddr} 运行时替换，不加 Gowin cli/cable 前缀。"""
    run: int                         # Gowin op，默认 54（Arora V 位流，.fs/.bin 通用）
    image: Path                      # DFU 位流绝对路径（.fs 或 .bin，均走 --fsFile）
    spiaddr: int                     # 外部 SPI Flash 起始地址
    # 烧录专用看门狗：Linux Gowin 实测 826KB 约 18s，Windows(Gowin exe) 慢 ~10 倍
    # （约 3 分钟，与驱动/频率无关），openFPGALoader 约 12s。不能沿用探测的
    # timeout_s=30——30s 只烧到 ~16% 就会被看门狗杀掉。
    timeout_s: float = 600
    # 自定义烧录命令（可按平台 argv_windows/argv_linux 覆盖）。实测 Gowin Windows
    # 版 exe 本身慢 10 倍且无法调优，Windows 用 openFPGALoader 提速 15 倍。
    argv: list[str] | None = None


@dataclass(frozen=True)
class Programmer:
    """外置 JTAG 烧录器命令声明（来自 programmer.toml）。一个 `cli` argv 前缀
    驱动全部操作：探测（--run 0）、eFuse 读（--keyread）、烧空板（FlashOp）、
    eFuse 写+锁（--keywritefile --keyFile <key> --keylock）、DFU<->APP 保底
    切换（switch，见下）。缺省则无烧录能力。"""
    cli: list[str]                   # argv 前缀，如 [<appimage>, "--programmer-cli"] 或 ["programmer_cli.exe"]
    device: str                      # Gowin --device 型号，如 GW5AT-15A / GW5AT-60B
    cable_candidates: list[int]      # 逐个尝试的 --cable-index，首个读到器件的即用
    timeout_s: float
    flash: FlashOp | None            # None -> 未声明 [programmer.flash]，无烧空板
    efuse_key_file: Path | None      # None -> 未声明 [programmer.efuse]，无 eFuse 写锁
    # [programmer.switch]：模式切换保底方案（USB 切换不可用时经外置烧录器执行）。
    # 键 "dfu2app"/"app2dfu" -> 追加在公共前缀之后的参数；含 "/" 的 token 视为
    # 产品目录相对路径并解析为绝对路径。空 dict -> 未声明，回退弹窗人工。
    switch: dict[str, list[str]] = field(default_factory=dict)


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
    # DFU<->APP 切换：True -> 产品自身支持 USB 控制传输 RECONFIG（无需外置硬件，
    # 首选）；False -> 依次回退 programmer.switch（外置烧录器保底）/ 弹窗人工。
    switch_usb_reconfig: bool
    product_dir: Path                   # resources/products/<id>/（固件等资源按此解析）
    programmer: Programmer | None       # None -> programmer.toml 缺失/无效，无烧录能力
    timeouts: Timeouts

    @property
    def unitsize(self) -> int:
        return (self.num_channels + 7) // 8

    def legal_rates(self, channels: int) -> list[int]:
        """Samplerates satisfying channels*rate/8 <= max_bandwidth."""
        limit = self.max_bandwidth_mbps * 1_000_000
        return [r for r in self.samplerates_hz if channels * r // 8 <= limit]


def _default_cli() -> list[str]:
    """平台感知的烧录器 CLI 缺省值（cli 未在任何 TOML 声明时用）。
    版本无关的约定路径优先，便于升级替换；Linux 兼容已有带版本的 AppImage。"""
    bin_dir = RESOURCES_DIR / "bin"
    if PLATFORM_KEY == "windows":
        # 把整个 Gowin Programmer 文件夹拷进 resources/bin/Programmer/（≈ Linux 软链）
        exe = bin_dir / "Programmer" / "bin" / "programmer_cli.exe"
        return [str(exe)] if exe.is_file() else []
    # linux / darwin：版本无关名优先，否则 glob 兼容现有 Gowin-Programmer-<ver>-*.AppImage
    exe = bin_dir / "Gowin-Programmer-x86_64.AppImage"
    if not exe.is_file():
        hits = sorted(bin_dir.glob("Gowin-Programmer*.AppImage"))
        exe = hits[0] if hits else None
    return [str(exe), "--programmer-cli"] if exe else []


def _cli_from(raw_cli, base: Path) -> list[str]:
    """解析一份声明的 cli：首元素含 "/" 或绝对路径 -> 按 base 解析为绝对路径，
    **路径不存在则返回 []**（让调用方回退到更低优先级的来源）；无 "/" 的裸命令名
    （如 'openFPGALoader'）信任 PATH 原样保留。空/未声明 -> []。"""
    if not raw_cli:
        return []
    out = [str(x) for x in raw_cli]
    first = Path(out[0])
    if "/" in out[0] or first.is_absolute():
        cand = first if first.is_absolute() else (base / first)
        cand = cand.resolve()
        if not cand.is_file():
            return []          # 声明的路径不存在 -> 回退
        out[0] = str(cand)
    return out


def load_shared_programmer() -> dict:
    """读共享 resources/programmer.toml 的 [programmer]（cli/cli_<plat>/timeout_s）。
    16U3/32U3 共用同一烧录器，cli 集中于此，产品档案不再各写一遍。文件缺失/无效
    则返回 {}（回退到 _default_cli()）。"""
    f = RESOURCES_DIR / "programmer.toml"
    if not f.is_file():
        return {}
    try:
        return tomllib.loads(f.read_text(encoding="utf-8")).get("programmer", {}) or {}
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def load_programmer(product_dir: Path,
                    shared: dict | None = None) -> tuple[Programmer | None, list[str]]:
    """Load programmer.toml.  Returns (programmer, error strings).
    `shared` 是 load_shared_programmer() 的结果（cli/timeout 的共享缺省）。"""
    shared = shared or {}
    f = product_dir / "programmer.toml"
    if not f.is_file():
        return None, [t("programmer.toml missing: {f}").format(f=f)]
    try:
        doc = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        return None, [t("programmer.toml parse failed: {f}: {e}").format(f=f, e=e)]
    praw = doc.get("programmer")
    if praw is None:
        return None, [t("programmer.toml missing [programmer] section: {f}").format(f=f)]
    if "device" not in praw:
        # device 是唯一各产品必填、无默认的字段（IDCODE 校验必须精确匹配）
        return None, [t("programmer.toml [programmer] missing device: {f}").format(f=f)]
    try:
        # 以下各项：产品档案覆盖 -> 共享 resources/programmer.toml -> 代码默认。
        # cli 优先级
        cli = (_cli_from(praw.get(f"cli_{PLATFORM_KEY}", praw.get("cli")), product_dir)
               or _cli_from(shared.get(f"cli_{PLATFORM_KEY}", shared.get("cli")),
                            RESOURCES_DIR)
               or _default_cli())
        # cable_candidates：产品 -> 共享 -> 代码默认（覆盖各台架常见 index，逐个试）
        cables = [int(c) for c in (praw.get("cable_candidates")
                                   or shared.get("cable_candidates") or DEFAULT_CABLES)]
        if not cables:
            raise ValueError(t("cable_candidates is empty"))

        # flash / efuse：共享默认 + 产品覆盖（按键合并）
        fraw = {**(shared.get("flash") or {}), **(praw.get("flash") or {})}
        flash = None
        if fraw.get("image"):
            # 自定义烧录命令：按平台取 argv_<plat>/argv，首元素按 resources/ 解析；
            # 可执行文件缺失时 _cli_from 返回 [] -> 自动回退 Gowin 默认命令形态
            fargv = _cli_from(fraw.get(f"argv_{PLATFORM_KEY}", fraw.get("argv")),
                              RESOURCES_DIR) or None
            flash = FlashOp(
                run=int(fraw.get("run", DEFAULT_FLASH_RUN)),
                image=_resolve_image(product_dir, str(fraw["image"])),
                spiaddr=int(fraw.get("spiaddr", 0)),
                timeout_s=float(fraw.get("timeout_s", 600)),
                argv=fargv,
            )
        eraw = {**(shared.get("efuse") or {}), **(praw.get("efuse") or {})}
        key_file = (product_dir / str(eraw["key_file"])).resolve() \
            if eraw.get("key_file") else None

        switch: dict[str, list[str]] = {}
        sraw = praw.get("switch")
        if sraw is not None:
            for direction in ("dfu2app", "app2dfu"):
                args = sraw.get(direction)
                if args is None:
                    continue
                # 含 "/" 的 token 视为产品目录相对路径（如 firmware/xx.fs），
                # 解析为绝对路径，与 cli 首元素的解析规则一致
                switch[direction] = [
                    str((product_dir / a).resolve())
                    if "/" in str(a) and not Path(str(a)).is_absolute() else str(a)
                    for a in args]
            if not switch:
                raise ValueError(t("[programmer.switch] must declare at least dfu2app or app2dfu"))

        prog = Programmer(
            cli=cli,
            device=str(praw["device"]),
            cable_candidates=cables,
            timeout_s=float(praw.get("timeout_s", shared.get("timeout_s", 30))),
            flash=flash,
            efuse_key_file=key_file,
            switch=switch,
        )
        return prog, []
    except (KeyError, ValueError, TypeError) as e:
        return None, [t("programmer.toml [programmer] invalid: {e}").format(e=e)]


def _parse_profile(product_dir: Path,
                   shared: dict | None = None) -> tuple[ProductProfile | None, list[Problem]]:
    problems: list[Problem] = []
    pid_for_log = product_dir.name
    toml_file = product_dir / "product.toml"
    try:
        doc = tomllib.loads(toml_file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        return None, [Problem("error", pid_for_log, t("profile parse failed: {e}").format(e=e))]

    def err(msg: str) -> tuple[None, list[Problem]]:
        problems.append(Problem("error", pid_for_log, msg))
        return None, problems

    if doc.get("schema_version") != SCHEMA_VERSION:
        return err(t("schema_version should be {expected}, actual {actual!r}").format(
            expected=SCHEMA_VERSION, actual=doc.get('schema_version')))

    try:
        product = doc["product"]
        usb = doc["usb"]
        capture = doc["capture"]
        expected = doc["expected"]
        firmware = doc.get("firmware", {})

        prod_id = str(product["id"])
        if prod_id != product_dir.name:
            return err(t("product.id ({pid}) does not match directory name ({dirname})").format(
                pid=prod_id, dirname=product_dir.name))

        dfu_pid = usb.get("dfu_pid")
        if dfu_pid is None:
            problems.append(Problem(
                "warning", prod_id,
                t("usb.dfu_pid not configured; DFU and one-click flow will be disabled")))

        num_channels = int(capture["num_channels"])
        channel_options = [int(c) for c in capture["channel_options"]]
        if any(c <= 0 or c > num_channels for c in channel_options):
            return err(t("channel_options {opts} exceed num_channels={n}").format(
                opts=channel_options, n=num_channels))

        samplerates_hz = sorted(parse_rate(r) for r in capture["samplerates"])

        default_channels = int(capture.get("default_channels", channel_options[-1]))
        if default_channels not in channel_options:
            return err(t("default_channels={dc} not in channel_options").format(dc=default_channels))
        limit = int(capture["max_bandwidth_mbps"]) * 1_000_000
        legal_defaults = [r for r in samplerates_hz
                          if default_channels * r // 8 <= limit]
        default_samplerate_hz = parse_rate(
            capture.get("default_samplerate", legal_defaults[-1] if legal_defaults else samplerates_hz[0]))
        if default_samplerate_hz not in legal_defaults:
            return err(t("default_samplerate={sr} is invalid for {dc}ch (not in the list or exceeds bandwidth)").format(
                sr=capture.get('default_samplerate'), dc=default_channels))

        tests = []
        for i, ct in enumerate(doc["capture"].get("tests", [])):
            ch = int(ct["channels"])
            rate = parse_rate(ct["samplerate"])
            if ch > num_channels:
                return err(t("capture.tests[{i}].channels={ch} exceeds num_channels").format(i=i, ch=ch))
            if rate not in samplerates_hz:
                return err(t("capture.tests[{i}].samplerate={sr} not in samplerates list").format(
                    i=i, sr=ct['samplerate']))
            tests.append(CaptureTest(ch, rate, str(ct.get("samples", capture.get("default_samples", "1M")))))
        if not tests:
            problems.append(Problem("warning", prod_id,
                                    t("capture.tests is empty; one-click flow has no test points")))

        max_bw = int(capture["max_bandwidth_mbps"])
        for ct in tests:
            if ct.channels * ct.samplerate_hz // 8 > max_bw * 1_000_000:
                return err(t("test point {ch}ch@{rate} exceeds bandwidth {bw}MB/s").format(
                    ch=ct.channels, rate=format_rate(ct.samplerate_hz), bw=max_bw))

        app_rel = firmware.get("app")
        app_firmware = (product_dir / app_rel).resolve() if app_rel else None

        # DFU<->APP 切换能力：产品自身是否支持 USB 控制传输 RECONFIG（"USB LA
        # 协议规范"0x30）。保底方案在 programmer.toml [programmer.switch] 声明。
        ms_raw = doc.get("mode_switch", {})
        switch_usb_reconfig = bool(ms_raw.get("usb_reconfig", False))

        programmer, prog_errors = load_programmer(product_dir, shared)
        for m in prog_errors:
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
            switch_usb_reconfig=switch_usb_reconfig,
            product_dir=product_dir,
            programmer=programmer,
            timeouts=timeouts,
        )
        return profile, problems
    except (KeyError, ValueError, TypeError) as e:
        kind = t("missing required field") if isinstance(e, KeyError) else t("invalid field")
        return err(f"{kind}: {e!r}")


def load_profiles(products_dir: Path = PRODUCTS_DIR) -> tuple[list[ProductProfile], list[Problem]]:
    """Load every resources/products/<id>/product.toml.  A broken product yields
    a Problem and is skipped without affecting other products."""
    profiles: list[ProductProfile] = []
    problems: list[Problem] = []
    if not products_dir.is_dir():
        return [], [Problem("error", None,
                            t("product profile directory does not exist: {d}").format(d=products_dir))]
    dirs = sorted(d for d in products_dir.iterdir()
                  if d.is_dir() and (d / "product.toml").is_file())
    if not dirs:
        return [], [Problem("error", None,
                            t("product profile directory is empty: {d}; please place <product_id>/product.toml").format(d=products_dir))]
    shared = load_shared_programmer()   # 共享 cli/timeout，各产品继承
    for d in dirs:
        profile, probs = _parse_profile(d, shared)
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
                t("app VID/PID {vidpid} conflicts with product {other}").format(
                    vidpid=f"{key[0]:#06x}:{key[1]:#06x}", other=app_seen[key])))
        app_seen[key] = p.id
        if key in dfu_pids:
            problems.append(Problem(
                "error", p.id,
                t("app VID/PID {vidpid} conflicts with dfu_pid of product {other}").format(
                    vidpid=f"{key[0]:#06x}:{key[1]:#06x}", other=dfu_pids[key])))
    return profiles, problems


def check_resources(profiles: list[ProductProfile],
                    sigrok_bin: Path | None) -> list[Problem]:
    """Startup self-check for admin-placed resources."""
    problems: list[Problem] = []
    if sigrok_bin is None:
        problems.append(Problem(
            "error", None,
            t("sigrok-cli binary not found; place it in {d} with the platform-specific name (see resources/README.md); capture disabled").format(
                d=RESOURCES_DIR / 'bin')))
    for p in profiles:
        if p.app_firmware is None:
            problems.append(Problem("warning", p.id,
                                    t("firmware.app not configured; DFU and one-click flow disabled")))
        elif not p.app_firmware.is_file():
            problems.append(Problem(
                "warning", p.id,
                t("application firmware missing: {path}; DFU and one-click flow disabled").format(
                    path=p.app_firmware)))
        prog = p.programmer
        if prog is not None:
            if not prog.cli:
                problems.append(Problem(
                    "warning", p.id,
                    t("programmer CLI not found (programmer.cli not declared and no Gowin AppImage in resources/bin); blank-flash/eFuse disabled")))
            if prog.flash is not None and not prog.flash.image.is_file():
                problems.append(Problem(
                    "warning", p.id,
                    t("blank-flash DFU image missing: {path}; blank-flash disabled").format(
                        path=prog.flash.image)))
            if prog.efuse_key_file is not None and not prog.efuse_key_file.is_file():
                problems.append(Problem(
                    "warning", p.id,
                    t("eFuse key file missing: {path}; the eFuse write-lock prerequisite of blank-flash will fail").format(
                        path=prog.efuse_key_file)))
            for d, args in prog.switch.items():
                for tok in args:
                    tp = Path(tok)
                    # 只查被解析为产品目录内路径的 token（即声明里带 "/" 的资源引用）
                    if tp.is_absolute() and tp.is_relative_to(p.product_dir) \
                            and not tp.is_file():
                        problems.append(Problem(
                            "warning", p.id,
                            t("switch {d} references a missing file: {path}; this direction falls back to a manual dialog").format(
                                d=d, path=tp)))
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
        sw = ["USB RECONFIG"] if p.switch_usb_reconfig else []
        if p.programmer is not None and p.programmer.switch:
            sw.append(f"烧录器保底({'/'.join(p.programmer.switch)})")
        sw.append("弹窗人工")
        print(f"  模式切换: {' -> '.join(sw)}")
        if p.programmer is None:
            print("  烧录器: 无 programmer.toml")
        else:
            pr = p.programmer
            print(f"  烧录器: device={pr.device} cli={pr.cli} cables={pr.cable_candidates}")
            if pr.flash:
                print(f"    烧空板: run={pr.flash.run} image={pr.flash.image.name} spiaddr={pr.flash.spiaddr:#x}")
            if pr.efuse_key_file:
                print(f"    eFuse:  key_file={pr.efuse_key_file.name}")
            for d, args in pr.switch.items():
                print(f"    切换 {d}: {' '.join(args)}")
    print(f"\n== {len(problems)} 个问题 ==")
    for prob in problems:
        print(f"  {prob}")
    sys.exit(1 if any(p.severity == "error" for p in problems) else 0)
