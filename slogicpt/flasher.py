"""DFU firmware flashing, wrapping the slogicpt.dfu library."""
from __future__ import annotations

import contextlib
import io
import threading
from pathlib import Path
from typing import Callable

from .dfu.spi_flash import flash_firmware
from .i18n import t


class FlashError(Exception):
    pass


class _LogBridge(io.TextIOBase):
    """Redirect dfu print() progress into a line callback."""

    def __init__(self, log_cb: Callable[[str], None]):
        self._log_cb = log_cb
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log_cb(line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._log_cb(self._buf)
        self._buf = ""


def flash_app_firmware(*, vid: int, pid: int, addr: int, firmware: Path,
                       verify: bool = True,
                       log_cb: Callable[[str], None] | None = None,
                       cancel: threading.Event | None = None) -> None:
    """Flash the application firmware onto a device in DFU mode.

    Raises FlashError with a readable message on any failure.
    """
    firmware = Path(firmware)
    if not firmware.is_file():
        raise FlashError(t("Firmware file not found: {path}").format(path=firmware))
    if cancel is not None and cancel.is_set():
        raise FlashError(t("Cancelled"))
    data = firmware.read_bytes()
    if log_cb:
        log_cb(t("Firmware: {path} ({size} bytes) -> {vid:#06x}:{pid:#06x} @ {addr:#x}").format(
            path=firmware, size=len(data), vid=vid, pid=pid, addr=addr))

    out = _LogBridge(log_cb) if log_cb else io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            try:
                flash_firmware(vid, pid, addr, data, verify=verify)
            except Exception as first:
                # a stalled/stuck DFU device fails the very first bulk
                # transfer with EIO -- reset the device once and retry
                import usb.core
                if not isinstance(first, usb.core.USBError):
                    raise
                print(t("USB transfer failed ({err}), resetting device and "
                        "retrying once…").format(err=first))
                _usb_reset(vid, pid)
                flash_firmware(vid, pid, addr, data, verify=verify)
    except FlashError:
        raise
    except Exception as e:  # usb.core errors, RuntimeError, assertion...
        raise FlashError(t("DFU flash failed: {err}").format(err=e)) from e
    finally:
        out.flush()


def _usb_reset(vid: int, pid: int, settle_s: float = 2.0) -> None:
    import time
    import usb.core
    import usb.util
    dev = usb.core.find(idVendor=vid, idProduct=pid)
    if dev is None:
        raise FlashError(t("Device not found after reset"))
    try:
        dev.reset()   # 常见现象：抛 "Entity not found" 但设备已实际重枚举
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)
    time.sleep(settle_s)


if __name__ == "__main__":
    # smoke test: imports resolve (run as python -m slogicpt.flasher)
    from .dfu.spi_flash import SPIFlashDevice  # noqa: F401
    print("flasher: dfu imports OK")
    print("用法示例: flash_app_firmware(vid=0x359F, pid=0x30F1, addr=0x0, firmware=Path('app.bin'))")
