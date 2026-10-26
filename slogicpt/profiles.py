"""Product profile loading / validation / resource self-check.

All product differences (VID/PID, channels, bandwidth, test plan, timeouts)
live in resources/products/<id>/product.toml.  The fixture bitstream they test
with, resources/fixture/testgen.fs, is shared by every product.

Adding a new product requires only a new directory + resources -- no code
changes.
"""
from __future__ import annotations

import platform as platform_mod
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .fpga import (FPGA_PID, FPGA_SERIAL_HINTS, FPGA_VID, FixtureUsb)
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
FIXTURE_CONFIG = RESOURCES_DIR / "fixture.toml"

PLATFORM_KEY = {"Linux": "linux", "Windows": "windows"}.get(
    platform_mod.system(), "linux")

SCHEMA_VERSION = 1

# USB link names -> libusb_speed (LIBUSB_SPEED_*) codes, for [usb] links and
# the per-config `link` tag of [[plan]].
LINK_SPEEDS = {"USB2": 3, "5G": 4, "10G": 5, "20G": 6}


def link_name(speed: int | None) -> str:
    """libusb_speed code -> short link name (the inverse of LINK_SPEEDS; full-
    and low-speed both read as USB1)."""
    if speed in (1, 2):
        return "USB1"
    return next((k for k, v in LINK_SPEEDS.items() if v == speed), "?")


def parse_link(name: str) -> int:
    """'10G' / '5G' / 'USB2' -> libusb speed code (ValueError if unknown)."""
    key = str(name).strip().upper()
    if key not in LINK_SPEEDS:
        raise ValueError(t("unknown link {name!r} (expected one of {opts})").format(
            name=name, opts="/".join(LINK_SPEEDS)))
    return LINK_SPEEDS[key]


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


# =============================================================================
# Signal source and test plan
#
# Every product is tested against the Tang Nano 9K fixture's pattern modes:
#
#   [signal_source]   how to reach the generator (serial port, VID/PID, bitstream)
#   [[plan]]          which modes to run, at which channel counts and rates, and
#                     which kinds of check apply
#
# The expectations themselves deliberately do NOT live in the TOML: the full matrix
# is 10 modes x 4 rate groups x 32 channels = 1280 numbers, and a change to the RTL
# should need exactly one edit. They are computed in patterns.py instead.
# =============================================================================

@dataclass(frozen=True)
class SignalSource:
    """The fixture that generates the test patterns."""

    serial: str                          # "auto" or an explicit port name
    vid: int
    pid: int
    baud: int
    settle_ms: int                       # quiet time after a mode change
    reply_timeout_ms: int
    firmware: Path | None                # testgen.fs, loaded into the fixture's SRAM
    channel_order: tuple[int, ...]       # generator channel -> analyser channel
    serial_hints: tuple[str, ...] = FPGA_SERIAL_HINTS

    @property
    def usb(self) -> FixtureUsb:
        """The USB identity this fixture is looked up by."""
        return FixtureUsb(self.vid, self.pid, self.serial_hints)


@dataclass(frozen=True)
class ThresholdGroup:
    """Channels that need a different input threshold from the default.

    The fixture drives two logic families into one analyser: most channels are
    3.3 V, but CH20..CH25 come from bank 3, which the on-board PSRAM locks to
    1.8 V. There is one global threshold per capture, and measurement showed no
    single value serves both -- so those channels are captured a second time at a
    lower threshold and their verdict is taken from that pass.
    """

    channels: tuple[int, ...]
    voltage_threshold_v: float
    label: str


@dataclass(frozen=True)
class PlanConfig:
    """One channel-count / sample-rate combination."""

    channels: int
    samplerate_hz: int
    window_ms: float                     # capture length, chosen per mode
    max_samples: int                     # cap, so a high rate cannot explode
    # the test-station pass this config belongs to (libusb speed code, e.g. 5 =
    # 10G).  A slower link caps the rate (32U3 on 5G: 32ch@100M), so each link
    # pass has its own configs.  None -> used in every pass.
    link: int | None = None


@dataclass(frozen=True)
class PlanEntry:
    """One pattern mode and how to exercise it."""

    mode: int
    label: str
    ready: bool                          # False -> the mode FAILS with `note`
    note: str
    checks: tuple[str, ...]
    configs: tuple[PlanConfig, ...]


@dataclass(frozen=True)
class Timeouts:
    capture_s: float = 120
    mode_switch_s: float = 30            # each leg of the DFU/APP round trip


@dataclass
class Problem:
    severity: Literal["error", "warning"]
    product_id: str | None
    message: str

    def __str__(self) -> str:
        prefix = f"[{self.product_id}] " if self.product_id else ""
        return f"{self.severity.upper()}: {prefix}{self.message}"


def load_fixture_usb(path: Path = FIXTURE_CONFIG
                     ) -> tuple[FixtureUsb | None, list[Problem]]:
    """Read the fixture's USB identity from resources/fixture.toml.

    The file is optional: when it is missing, the product profiles' own vid/pid
    (or the built-in defaults) are used, so an old resources/ tree keeps
    working.  A present-but-broken file yields a Problem and falls back to the
    same defaults.
    """
    problems: list[Problem] = []
    if not path.is_file():
        return None, problems
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as e:
        problems.append(Problem("error", None,
                                t("fixture.toml parse failed: {e}").format(e=e)))
        return None, problems
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append(Problem(
            "error", None,
            t("fixture.toml schema_version should be {expected}, actual {actual!r}")
            .format(expected=SCHEMA_VERSION, actual=doc.get("schema_version"))))
        return None, problems
    try:
        raw = doc["fixture"]
        vid = int(raw["vid"])
        pid = int(raw["pid"])
        hints_raw = raw["serial_hints"]
        if not isinstance(hints_raw, list):
            raise ValueError("serial_hints must be a list of strings")
        hints = tuple(str(x).strip() for x in hints_raw if str(x).strip())
        if not hints:
            raise ValueError("serial_hints is empty")
        if not (0 <= vid <= 0xFFFF and 0 <= pid <= 0xFFFF):
            raise ValueError("vid/pid out of range")
    except (KeyError, TypeError, ValueError) as e:
        problems.append(Problem("error", None,
                                t("fixture.toml is invalid: {e}").format(e=e)))
        return None, problems
    return FixtureUsb(vid=vid, pid=pid, serial_hints=hints), problems


@dataclass(frozen=True)
class ProductProfile:
    id: str
    display_name: str
    vid: int
    app_pid: int
    dfu_pid: int | None                 # DFU-mode PID, to recognise unflashed boards
    driver: str
    num_channels: int                   # device total channels -> unitsize = ceil(n/8)
    max_bandwidth_mbps: int
    samplerates_hz: list[int]
    voltage_threshold_v: float
    signal_source: SignalSource          # the Tang Nano 9K fixture
    plan: list[PlanEntry]                # modes x configs, with their check sets
    threshold_groups: list[ThresholdGroup]   # channels needing another threshold
    product_dir: Path                   # resources/products/<id>/（固件等资源按此解析）
    timeouts: Timeouts
    # 产测工位按顺序逐轮测试的 USB 链路（libusb speed 码，[usb] links），如 32U3
    # (10G, 5G)：先在 10G 下全测通过，再换插 5G 口重测，保证用户两种链路都可用。
    # 第一轮是门槛（达不到直接 FAIL）；空 -> 不设门槛、只测一轮。
    links: tuple[int, ...] = ()
    # the board switches APP <-> DFU on a USB RECONFIG request ([usb] reconfig);
    # the test then starts with a round trip through DFU
    reconfig: bool = False

    @property
    def min_link(self) -> int | None:
        """The first (gating) link pass, or None when no link is required."""
        return self.links[0] if self.links else None

    @property
    def unitsize(self) -> int:
        return (self.num_channels + 7) // 8


def _parse_signal_source(doc: dict, product_dir: Path, num_channels: int,
                         problems: list, pid_for_log: str,
                         fixture: FixtureUsb | None) -> SignalSource:
    """Parse [signal_source] (required: every product is tested against the
    fixture).  `fixture` (resources/fixture.toml) overrides the USB identity;
    without that file the product's own vid/pid/serial_hints are the fallback."""
    raw = doc["signal_source"]
    firmware = (product_dir / str(raw["firmware"])).resolve()

    if fixture is not None:
        usb = fixture
    else:
        hints_raw = raw.get("serial_hints", FPGA_SERIAL_HINTS)
        if isinstance(hints_raw, str):
            hints_raw = [hints_raw]
        try:
            hints = tuple(str(x).strip() for x in hints_raw if str(x).strip())
        except TypeError:
            hints = ()
        if not hints:
            problems.append(Problem(
                "error", pid_for_log,
                t("signal_source.serial_hints must be a non-empty list")))
            hints = FPGA_SERIAL_HINTS
        usb = FixtureUsb(
            vid=int(raw.get("vid", FPGA_VID)),
            pid=int(raw.get("pid", FPGA_PID)),
            serial_hints=hints,
        )

    order_raw = raw.get("channel_order")
    if order_raw is None:
        order = tuple(range(num_channels))
    else:
        order = tuple(int(v) for v in order_raw)
        if len(order) != num_channels or sorted(order) != list(range(num_channels)):
            problems.append(Problem(
                "error", pid_for_log,
                t("signal_source.channel_order must be a permutation of 0..{n}").format(
                    n=num_channels - 1)))
            order = tuple(range(num_channels))

    return SignalSource(
        serial=str(raw.get("serial", "auto")),
        vid=usb.vid,
        pid=usb.pid,
        baud=int(raw.get("baud", 115200)),
        settle_ms=int(raw.get("settle_ms", 400)),
        reply_timeout_ms=int(raw.get("reply_timeout_ms", 1000)),
        firmware=firmware,
        channel_order=order,
        serial_hints=usb.serial_hints,
    )


def _parse_links(usb: dict) -> tuple[int, ...]:
    """[usb] links = ["10G", "5G"]: the ordered link passes."""
    return tuple(parse_link(x) for x in usb.get("links", []))


def _parse_plan(doc: dict, num_channels: int, samplerates_hz: list[int],
                max_bw: int, problems: list, pid_for_log: str) -> list[PlanEntry]:
    """Parse [[plan]]."""
    entries: list[PlanEntry] = []

    for i, raw in enumerate(doc.get("plan", [])):
        mode = int(raw.get("mode", i))
        configs = []
        for j, c in enumerate(raw.get("configs", [])):
            ch = int(c["channels"])
            rate = parse_rate(c["samplerate"])
            if ch > num_channels:
                problems.append(Problem(
                    "error", pid_for_log,
                    t("plan[{i}].configs[{j}].channels={ch} exceeds num_channels").format(
                        i=i, j=j, ch=ch)))
                continue
            if rate not in samplerates_hz:
                problems.append(Problem(
                    "error", pid_for_log,
                    t("plan[{i}].configs[{j}].samplerate={sr} not in the samplerates "
                      "list").format(i=i, j=j, sr=c["samplerate"])))
                continue
            if ch * rate // 8 > max_bw * 1_000_000:
                problems.append(Problem(
                    "error", pid_for_log,
                    t("test point {ch}ch@{rate} exceeds bandwidth {bw}MB/s").format(
                        ch=ch, rate=format_rate(rate), bw=max_bw)))
                continue
            configs.append(PlanConfig(
                channels=ch,
                samplerate_hz=rate,
                window_ms=float(c.get("window_ms", 20.0)),
                max_samples=int(c.get("max_samples", 8_000_000)),
                link=parse_link(c["link"]) if c.get("link") else None,
            ))
        if not configs:
            problems.append(Problem("warning", pid_for_log,
                                    t("plan[{i}] has no usable configs").format(i=i)))
        entries.append(PlanEntry(
            mode=mode,
            label=str(raw.get("label", "mode %d" % mode)),
            ready=bool(raw.get("ready", True)),
            note=str(raw.get("note", "")),
            checks=tuple(str(c) for c in raw.get("checks", ["freq"])),
            configs=tuple(configs),
        ))

    if not entries:
        problems.append(Problem("error", pid_for_log,
                                t("[[plan]] is empty; there is nothing to test")))
    return entries


def _parse_threshold_groups(doc: dict, num_channels: int, default_v: float,
                            problems: list, pid_for_log: str) -> list[ThresholdGroup]:
    """Parse [[capture.threshold_groups]]."""
    out: list[ThresholdGroup] = []
    for i, raw in enumerate(doc.get("capture", {}).get("threshold_groups", [])):
        chans = tuple(int(c) for c in raw.get("channels", []))
        if not chans or any(c < 0 or c >= num_channels for c in chans):
            problems.append(Problem(
                "error", pid_for_log,
                t("capture.threshold_groups[{i}] has an out-of-range channel").format(i=i)))
            continue
        v = float(raw.get("voltage_threshold_v", default_v))
        out.append(ThresholdGroup(channels=chans, voltage_threshold_v=v,
                                  label=str(raw.get("label",
                                                    "%.1f V group" % v))))
    return out


def _parse_profile(product_dir: Path,
                   fixture: FixtureUsb | None) -> tuple[ProductProfile | None, list[Problem]]:
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

        prod_id = str(product["id"])
        if prod_id != product_dir.name:
            return err(t("product.id ({pid}) does not match directory name ({dirname})").format(
                pid=prod_id, dirname=product_dir.name))

        # optional: only lets the link check say "still in DFU mode" instead of
        # "no board found"
        dfu_pid = usb.get("dfu_pid")

        num_channels = int(capture["num_channels"])
        samplerates_hz = sorted(parse_rate(r) for r in capture["samplerates"])
        max_bw = int(capture["max_bandwidth_mbps"])

        tmo = doc.get("timeouts", {})
        timeouts = Timeouts(
            capture_s=float(tmo.get("capture_s", 120)),
            mode_switch_s=float(tmo.get("mode_switch_s", 30)),
        )

        default_vth = float(capture.get("voltage_threshold_v", 1.6))
        signal_source = _parse_signal_source(doc, product_dir, num_channels,
                                             problems, prod_id, fixture)
        plan = _parse_plan(doc, num_channels, samplerates_hz, max_bw,
                           problems, prod_id)
        threshold_groups = _parse_threshold_groups(doc, num_channels, default_vth,
                                                   problems, prod_id)

        profile = ProductProfile(
            id=prod_id,
            display_name=str(product["display_name"]),
            vid=int(usb["vid"]),
            app_pid=int(usb["app_pid"]),
            links=_parse_links(usb),
            reconfig=bool(usb.get("reconfig", False)),
            dfu_pid=int(dfu_pid) if dfu_pid is not None else None,
            driver=str(capture["driver"]),
            num_channels=num_channels,
            max_bandwidth_mbps=max_bw,
            samplerates_hz=samplerates_hz,
            voltage_threshold_v=default_vth,
            signal_source=signal_source,
            plan=plan,
            threshold_groups=threshold_groups,
            product_dir=product_dir,
            timeouts=timeouts,
        )
        if profile.reconfig and profile.dfu_pid is None:
            problems.append(Problem("error", prod_id,
                                    t("[usb] reconfig needs dfu_pid")))
        # every link pass needs a config for every ready mode, or that pass would
        # silently test less than the others
        for link in profile.links:
            for e in plan:
                if e.ready and not any(c.link in (None, link) for c in e.configs):
                    problems.append(Problem(
                        "error", prod_id,
                        t("plan M{mode} has no config for the {link} pass").format(
                            mode=e.mode, link=link_name(link))))
        return profile, problems
    except (KeyError, ValueError, TypeError) as e:
        kind = t("missing required field") if isinstance(e, KeyError) else t("invalid field")
        return err(f"{kind}: {e!r}")


def load_profiles(products_dir: Path = PRODUCTS_DIR) -> tuple[list[ProductProfile], list[Problem]]:
    """Load resources/fixture.toml and every resources/products/<id>/product.toml.
    A broken product yields a Problem and is skipped without affecting other
    products; a broken fixture.toml yields a Problem and the products fall back
    to their own (or the built-in) fixture USB identity."""
    profiles: list[ProductProfile] = []
    problems: list[Problem] = []
    fixture, fixture_problems = load_fixture_usb()
    problems.extend(fixture_problems)
    if not products_dir.is_dir():
        return [], [Problem("error", None,
                            t("product profile directory does not exist: {d}").format(d=products_dir))]
    dirs = sorted(d for d in products_dir.iterdir()
                  if d.is_dir() and (d / "product.toml").is_file())
    if not dirs:
        return [], [Problem("error", None,
                            t("product profile directory is empty: {d}; please place <product_id>/product.toml").format(d=products_dir))]
    for d in dirs:
        profile, probs = _parse_profile(d, fixture)
        problems.extend(probs)
        if profile is not None:
            profiles.append(profile)
    # app_pid must be unique (it identifies the product).  dfu_pid is normally
    # per-product too (16U3=0x30F1, 32U3=0x30F2), but the check still tolerates
    # products sharing one dfu_pid; it only errors when an app_pid collides with
    # some product's dfu_pid.
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
    return problems


if __name__ == "__main__":
    profiles, problems = load_profiles()
    fixture, _ = load_fixture_usb()
    if fixture is not None:
        print(f"夹具 USB: {fixture.vid:#06x}:{fixture.pid:#06x}  "
              f"序列号关键字: {', '.join(fixture.serial_hints)}")
    else:
        print("夹具 USB: 未配置 fixture.toml（使用代码默认值）")
    print(f"== 加载 {len(profiles)} 个产品档案 ==")
    for p in profiles:
        dfu = f"{p.dfu_pid:#06x}" if p.dfu_pid is not None else "未配置"
        print(f"\n{p.display_name} ({p.id})")
        print(f"  USB: {p.vid:#06x} app={p.app_pid:#06x} dfu={dfu}")
        print(f"  {p.num_channels}ch, {p.max_bandwidth_mbps}MB/s, unitsize={p.unitsize}")
        print(f"  采样率: {', '.join(format_rate(r) for r in p.samplerates_hz)}")
        print(f"  夹具位流: {p.signal_source.firmware}")
        print(f"  计划: {' '.join(f'M{e.mode}' for e in p.plan)}  链路: "
              f"{', '.join(link_name(k) for k in p.links) or '-'}")
    print(f"\n== {len(problems)} 个问题 ==")
    for prob in problems:
        print(f"  {prob}")
    sys.exit(1 if any(p.severity == "error" for p in problems) else 0)
