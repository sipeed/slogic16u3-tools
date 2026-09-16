"""External JTAG programmer probe (read-only).

The GUI's manual scan button calls probe() to answer two questions without
ever writing the chip:
  1. is an external JTAG programmer connected to a device?  (--run 0)
  2. is the AES-key eFuse locked?                            (--keyread)

Config comes from the product's manifest [probe] (device + cable_candidates);
we invoke the Gowin Programmer AppImage directly and parse its output.  All
operations here are read-only -- no --run flash op, no --keywrite.
"""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from .profiles import RESOURCES_DIR, Probe
from .sigrok import _popen_kwargs, watchdog

EFuse = Literal["locked", "unlocked", "unknown"]


@dataclass
class ProbeResult:
    programmer_present: bool
    cable_index: int | None
    device: str | None
    efuse: EFuse
    detail: str


def find_appimage(probe_cfg: Probe) -> Path | None:
    """probe.app override, else the Gowin Programmer AppImage in resources/bin."""
    if probe_cfg.app is not None:
        return probe_cfg.app if probe_cfg.app.is_file() else None
    hits = sorted((RESOURCES_DIR / "bin").glob("Gowin-Programmer*.AppImage"))
    return hits[0] if hits else None


# --- output parsing (isolated for self-test / Gowin-version adaptation) ------

_FAIL_MARKERS = ("cable failed", "no valid jtag", "id code mismatch",
                 "error:", "unable to open", "not found")


def _device_online(out: str) -> bool:
    """True when --run 0 read a real device (IDCODE), not a cable/JTAG failure."""
    low = out.lower()
    if any(m in low for m in _FAIL_MARKERS):
        return False
    # a valid read prints "Target Device: ..." and/or a GW5A "ID 0x0001xxxx"
    return ("target device" in low) or ("id 0x0001" in low) or ("idcode" in low)


def _efuse_state(out: str) -> EFuse:
    """Locked check comes first: the locked --keyread reply carries both
    'Device Locked!' and an 'Error:' line, so the error markers must not win."""
    low = out.lower()
    if "device locked" in low:
        return "locked"
    if any(m in low for m in _FAIL_MARKERS):
        return "unknown"
    if "finished" in low or "key" in low:
        return "unlocked"
    return "unknown"


# --- subprocess -------------------------------------------------------------

def _run(app: Path, args: list[str], timeout_s: float,
         log_cb: Callable[[str], None],
         cancel: threading.Event | None) -> str:
    """Run the AppImage CLI; return combined stdout+stderr (partial on
    timeout/cancel).  Output is also streamed to log_cb."""
    argv = [str(app), "--programmer-cli", *args]
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, **_popen_kwargs())
    except OSError as e:
        log_cb(f"[probe] 启动失败: {e}")
        return ""
    lines: list[str] = []
    with watchdog(proc, timeout_s, cancel) as fate:
        assert proc.stdout is not None
        for line in proc.stdout:
            s = line.rstrip()
            if s:
                lines.append(s)
                log_cb(s)
        proc.wait()
    if fate.killed_by:
        log_cb(f"[probe] {'已取消' if fate.killed_by == 'cancel' else f'超时 ({timeout_s:.0f}s)'}")
    return "\n".join(lines)


def probe(probe_cfg: Probe, log_cb: Callable[[str], None] = print,
          cancel: threading.Event | None = None) -> ProbeResult:
    """Try each cable candidate until one reads a device, then read its eFuse
    lock state.  Never writes the chip."""
    app = find_appimage(probe_cfg)
    if app is None:
        log_cb("[probe] 未找到 Gowin Programmer AppImage（resources/bin/）")
        return ProbeResult(False, None, None, "unknown",
                           "未找到 Gowin Programmer AppImage")
    dev = probe_cfg.device
    for cable in probe_cfg.cable_candidates:
        if cancel is not None and cancel.is_set():
            return ProbeResult(False, None, None, "unknown", "已取消")
        log_cb(f"[probe] 尝试 cable-index {cable}（device={dev}）读器件码…")
        out = _run(app, ["--device", dev, "--cable-index", str(cable), "--run", "0"],
                   probe_cfg.timeout_s, log_cb, cancel)
        if not _device_online(out):
            continue
        log_cb(f"[probe] cable-index {cable} 读到器件，读 eFuse 锁定状态…")
        kout = _run(app, ["--device", dev, "--cable-index", str(cable), "--keyread"],
                    probe_cfg.timeout_s, log_cb, cancel)
        efuse = _efuse_state(kout)
        log_cb(f"[probe] 结果：外置烧录器已连接，device={dev}，"
               f"cable-index={cable}，eFuse={efuse}")
        return ProbeResult(True, cable, dev, efuse,
                           f"cable-index {cable}, device {dev}, eFuse={efuse}")
    log_cb("[probe] 结果：未在任何 cable 上读到器件——外置烧录器未连接或未上电")
    return ProbeResult(False, None, None, "unknown",
                       "未在任何 cable 上读到器件（外置烧录器未连接？）")


if __name__ == "__main__":
    # pure-parsing self-test using captured Gowin Programmer output
    run0_ok = ("op 0: Target Device: GW5AT-60B(0x0001481B); ID 0x0001481B; "
               "User Code 0x00004946; Status Code 0x7002E020; Finished.")
    run0_bad = "Error: Cable failed to open via the channel"
    kr_locked = "Key1 Sel.\nError: Device Locked!\nValue: 1"
    kr_unlocked = "Key1 Sel.\nValue: 0123456789ABCDEF0123456789ABCDEF\nFinished."
    kr_err = "Error: Cable failed to open via the channel"
    assert _device_online(run0_ok) is True
    assert _device_online(run0_bad) is False
    assert _efuse_state(kr_locked) == "locked"
    assert _efuse_state(kr_unlocked) == "unlocked"
    assert _efuse_state(kr_err) == "unknown"
    print("programmer 解析自测 PASS（online / locked / unlocked / 未连接）")
