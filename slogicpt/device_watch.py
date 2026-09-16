"""USB device detection: which product, in which mode (APP / DFU).

The (vid, pid) -> (product, mode) map is derived entirely from the loaded
product profiles -- no hardcoded IDs.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum

import usb.core
import usb.util

from .profiles import ProductProfile


class Mode(Enum):
    APP = "APP"
    DFU = "DFU"


@dataclass(frozen=True)
class DeviceStatus:
    profile: ProductProfile
    mode: Mode
    pid: int
    serial: str | None = None   # USB iSerialNumber descriptor (version marker)

    def __str__(self) -> str:
        base = f"{self.profile.display_name} ({self.mode.value})"
        return f"{base} SN:{self.serial}" if self.serial else base


def _serial_of(dev) -> str | None:
    """Read the USB iSerialNumber string descriptor, or None if the device
    has no serial / it can't be read (perms, backend, kernel driver)."""
    try:
        if not dev.iSerialNumber:
            return None
        s = usb.util.get_string(dev, dev.iSerialNumber)
        return s.strip() if s else None
    except Exception:
        return None


def read_serial(vid: int, pid: int) -> str | None:
    """SerialNumber of the (vid, pid) device currently enumerated, or None."""
    try:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        return _serial_of(dev) if dev is not None else None
    except Exception:
        return None


def scan_devices(profiles: list[ProductProfile]) -> list[DeviceStatus]:
    found: list[DeviceStatus] = []
    for p in profiles:
        candidates: list[tuple[int, Mode]] = [(p.app_pid, Mode.APP)]
        if p.dfu_pid is not None:
            candidates.append((p.dfu_pid, Mode.DFU))
        for pid, mode in candidates:
            try:
                dev = usb.core.find(idVendor=p.vid, idProduct=pid)
                if dev is not None:
                    found.append(DeviceStatus(p, mode, pid, _serial_of(dev)))
            except Exception:
                pass  # backend hiccups shouldn't kill the poll loop
    return found


def find_pid(vid: int, pid: int) -> bool:
    try:
        return usb.core.find(idVendor=vid, idProduct=pid) is not None
    except Exception:
        return False


def find_conn(vid: int, pid: int) -> str | None:
    """sigrok `conn=` spec (usb bus.address) for the device, so a capture
    targets exactly this product when several SLogic devices share one
    host (sigrok-cli refuses to capture with an ambiguous scan)."""
    try:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if dev is None or dev.bus is None or dev.address is None:
            return None
        return f"{dev.bus}.{dev.address}"
    except Exception:
        return None


def wait_for_pid(vid: int, pid: int, timeout_s: float,
                 poll_s: float = 0.5,
                 cancel: threading.Event | None = None) -> bool:
    """Poll until (vid, pid) enumerates.  False on timeout/cancel; the
    caller decides whether to prompt the operator and keep waiting."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if cancel is not None and cancel.is_set():
            return False
        if find_pid(vid, pid):
            return True
        time.sleep(poll_s)
    return False


if __name__ == "__main__":
    from .profiles import load_profiles
    profiles, _ = load_profiles()
    print("Ctrl+C 退出；插拔设备观察状态变化")
    last = None
    while True:
        found = scan_devices(profiles)
        desc = ", ".join(str(d) for d in found) if found else "无设备"
        if desc != last:
            print(time.strftime("[%H:%M:%S]"), desc)
            last = desc
        time.sleep(1)
