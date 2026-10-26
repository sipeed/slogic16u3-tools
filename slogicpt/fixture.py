"""Load the Tang Nano 9K fixture bitstream (testgen.fs) into its FPGA's SRAM.

openFPGALoader does the loading, addressed by the fixture's USB bus/device
number (its BL702 reports "BL702" or "FactoryAIOT" in its serial; a Gowin USB
programmer, also 0403:6010, does not).  Bus/device is used instead of the
serial because the newer BL702 firmware's serial contains a space
("FactoryAIOT Pro") and openFPGALoader's --scan-usb output cannot delimit it.

SRAM, not flash: writing the GW1NR-9C's embedded flash with openFPGALoader ends
in "CRC check : FAIL" and the design does not boot.  SRAM is volatile, so the
bitstream is loaded on the first use in a session and again whenever the
fixture stops answering (ensure_running) -- about 1.5 s each time.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import procutil
from .fpga import FixtureUsb, is_fixture_serial
from .i18n import t


@dataclass(frozen=True)
class FtdiDevice:
    """One 0403:6010 row from `openFPGALoader --scan-usb`."""
    bus: str
    addr: str
    manufacturer: str
    serial: str
    product: str

    @property
    def busdev(self) -> str:
        """What `openFPGALoader --busdev-num` expects ("bus:device")."""
        return f"{self.bus}:{self.addr}"

    def __str__(self) -> str:
        return f"{self.busdev} {self.manufacturer} {self.serial} {self.product}"


def _openfpga_exe() -> Path | None:
    """openFPGALoader 的位置：产测资源里自带的一份，按平台挑；Linux 再退到 PATH
    （台架用发行版包）。资源目录里只有 Windows 的 .exe 时，Linux 绝不能选它——
    执行失败会被吞掉，最后只报一句误导性的"找不到设备"。"""
    import shutil
    from .profiles import RESOURCES_DIR
    bundled = RESOURCES_DIR / "bin" / "openFPGALoader"
    if procutil.IS_WINDOWS:
        cand = bundled / "openFPGALoader.exe"
        return cand if cand.is_file() else None
    cand = bundled / "openFPGALoader"
    if cand.is_file():
        return cand
    found = shutil.which("openFPGALoader")
    return Path(found) if found else None


def _ftdi_rows(exe: Path, usb: FixtureUsb) -> list[FtdiDevice] | None:
    """`openFPGALoader --scan-usb` 里所有匹配夹具 VID:PID 的行；None = 运行失败。"""
    try:
        proc = subprocess.run([str(exe), "--scan-usb"], capture_output=True,
                              timeout=20, **procutil.popen_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    # 列格式：Bus device vid:pid probe_type manufacturer serial product
    #  001 008  0x0403:0x6010 FTDI2232 SIPEED 2025030317 USB Debugger
    # serial 里可能有空格（`FactoryAIOT Pro JTAG Debugger`），不能可靠地按列
    # 切分；这里只取前四列，serial 拿到第一个 token 就够判断是不是夹具，真正
    # 定位设备用 bus:device。
    pair = rf"0x{usb.vid:04x}:0x{usb.pid:04x}"
    rows = []
    for line in (proc.stdout or "").splitlines():
        m = re.search(rf"^\s*(\d+)\s+(\d+)\s+{pair}\s+\S+\s+"
                      rf"(\S+)\s+(\S+)\s+(.+?)\s*$", line, re.IGNORECASE)
        if m:
            rows.append(FtdiDevice(*m.groups()))
    return rows


def _fixture_ftdi(log, usb: FixtureUsb) -> FtdiDevice | None:
    """夹具 BL702 在 openFPGALoader 眼里的那一行。"""
    exe = _openfpga_exe()
    if exe is None:
        return None
    rows = _ftdi_rows(exe, usb)
    if rows is None:
        log(t("openFPGALoader --scan-usb did not run: {p}").format(p=exe))
        return None
    for dev in rows:
        # serial 被空格截断时，整行仍带着完整名字；两处都认。
        if (is_fixture_serial(dev.serial, usb)
                or is_fixture_serial(str(dev), usb)):
            return dev
    log(t("Cannot find the fixture: no {vid:#06x}:{pid:#06x} device reports a "
          "fixture serial").format(vid=usb.vid, pid=usb.pid))
    log("   openFPGALoader --scan-usb: " + ("; ".join(str(r) for r in rows)
                                           or t("no matching USB device")))
    if procutil.IS_WINDOWS:
        # the serial port (channel B) answers through the FTDI driver, but
        # openFPGALoader reaches channel A through libusb, which needs WinUSB
        log(t("On Windows the fixture's FTDI channel A must use the WinUSB driver: "
              "deploy it on the Environment Setup page, then replug the fixture"))
    return None


def flash(bitstream: Path, log_cb=None, *, timeout_s: float = 120,
          cancel=None, usb: FixtureUsb | None = None) -> bool:
    """Load `bitstream` into the fixture's SRAM; True on success.

    Output goes to a temporary FILE, never through a pipe (procutil.run_to_file):
    subprocess.run with capture_output=True does not honour its timeout when
    the child leaves a grandchild holding the pipe.
    """
    def log(msg: str) -> None:
        if log_cb:
            log_cb(msg)

    usb = usb or FixtureUsb()
    if not bitstream.is_file():
        log(t("Fixture bitstream not found: {p}").format(p=bitstream))
        return False
    exe = _openfpga_exe()
    if exe is None:
        log(t("openFPGALoader not found; cannot load the fixture"))
        return False
    dev = _fixture_ftdi(log, usb)
    if dev is None:
        return False
    log(t("Loading fixture with openFPGALoader: {name} (FTDI serial {s})").format(
        name=bitstream.name, s=f"{dev.serial} @ {dev.busdev}"))
    # 用 bus:device 而不是 --ftdi-serial：新固件的序列号是 "FactoryAIOT Pro"，
    # 带空格，openFPGALoader --scan-usb 无法给出完整、可回传的字符串。
    rc, out, failure = procutil.run_to_file(
        [str(exe), "-c", "ft2232", "--busdev-num", dev.busdev, str(bitstream)],
        timeout_s, cancel)
    if failure == "timeout":
        log(t("openFPGALoader timed out after {s}s").format(s=f"{timeout_s:.0f}"))
        return False
    if failure == "cancel":
        return False
    if failure:
        log(t("Launch failed: {e}").format(e=failure.split(": ", 1)[-1]))
        return False
    for line in out.splitlines():
        log("   " + line.rstrip())
    # 只认 "CRC check: Success"，不看退出码，也不认 "Done"：openFPGALoader 每个
    # 阶段结束都打印 Done，CRC 失败（"CRC check : FAIL"）时照样有 Done。
    ok = "CRC check: Success" in out
    if not ok:
        log(t("openFPGALoader reported no success marker (rc={rc})").format(rc=rc))
    return ok


# testgen has been loaded into the fixture's SRAM by this process.  SRAM is
# volatile: when the fixture loses power the alive check fails and
# ensure_running loads it again.
session_loaded = False


def ensure_running(ss, log, cancel=None):
    """Open the fixture's serial port and return it, alive and in M0.

    testgen is loaded into SRAM on the first use in this process, and again
    whenever the fixture does not answer -- not before every board: a board
    swap does not disturb the fixture, and reloading costs ~1.5 s each time.
    `ss` is the profile's SignalSource.  Raises when it cannot be brought up.
    """
    from .fpga import SignalSource

    usb = ss.usb

    def load(why: str) -> None:
        global session_loaded
        log("[fixture] " + why)
        if not flash(ss.firmware, log, cancel=cancel, usb=usb):
            raise RuntimeError(t("could not load the signal generator"))
        session_loaded = True

    def open_alive():
        src = SignalSource(port=None if ss.serial == "auto" else ss.serial,
                           baud=ss.baud, settle_ms=ss.settle_ms,
                           reply_timeout_ms=ss.reply_timeout_ms, usb=usb)
        src.open()                 # finds the port, checks it answers, M0
        return src

    if not session_loaded:
        load(t("Loading the fixture bitstream into SRAM (first use in this session)"))
    try:
        return open_alive()
    except Exception as e:                                  # noqa: BLE001
        load(t("Fixture does not answer ({e}); loading its bitstream into SRAM "
               "again").format(e=e))
        return open_alive()


def _selftest() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    from .profiles import load_profiles
    profiles, _ = load_profiles()
    usb = profiles[0].signal_source.usb if profiles else FixtureUsb()
    exe = _openfpga_exe()
    print("openFPGALoader: %s" % exe)
    print("fixture USB: %#06x:%#06x  serial_hints=%s"
          % (usb.vid, usb.pid, ", ".join(usb.serial_hints)))
    if exe is None:
        print("RESULT: no fixture loader found")
        return 1
    rows = _ftdi_rows(exe, usb)
    print("%#06x:%#06x: %s" % (usb.vid, usb.pid, rows))
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
