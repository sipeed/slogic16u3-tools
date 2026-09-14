"""USB device detection: which product, in which mode (APP / OTA).

The (vid, pid) -> (product, mode) map is derived entirely from the loaded
product profiles -- no hardcoded IDs.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum

import usb.core

from profiles import ProductProfile


class Mode(Enum):
    APP = "APP"
    OTA = "OTA"


@dataclass(frozen=True)
class DeviceStatus:
    profile: ProductProfile
    mode: Mode
    pid: int

    def __str__(self) -> str:
        return f"{self.profile.display_name} ({self.mode.value})"


def scan_devices(profiles: list[ProductProfile]) -> list[DeviceStatus]:
    found: list[DeviceStatus] = []
    for p in profiles:
        candidates: list[tuple[int, Mode]] = [(p.app_pid, Mode.APP)]
        if p.ota_pid is not None:
            candidates.append((p.ota_pid, Mode.OTA))
        for pid, mode in candidates:
            try:
                if usb.core.find(idVendor=p.vid, idProduct=pid) is not None:
                    found.append(DeviceStatus(p, mode, pid))
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
    from profiles import load_profiles
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
